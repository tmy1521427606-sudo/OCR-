#!/usr/bin/env bash
# OCR v7 Linux 服务器部署脚本
#
# 做四件事：
#   1. 建 venv 并装依赖（psycopg / openpyxl / Pillow，都不需要编译）；
#      psycopg[binary] 装不上时自动退回纯 Python 版 + 系统 libpq5；
#   2. 建好收件目录、状态目录、输出目录；
#   3. 生成 /etc/ocr-v7/ocr-daemon.env（已存在则不动，避免覆盖密钥）；
#   4. 注册并启动 systemd 服务。
#
# 依赖安装刻意不用 --quiet：装失败时 pip 打印的「The conflict is caused by:」
# 才是真正的原因，加 --quiet 会把它一起吞掉，只剩一句没头没尾的 ERROR。
#
# 用法（在仓库根目录，用 root 或有 sudo 的账号跑）：
#   sudo bash deploy/install-linux.sh
#   sudo bash deploy/install-linux.sh --app-dir /opt/ocr-v7 --user ocr
#   sudo bash deploy/install-linux.sh --no-service     # 只装依赖，不注册 systemd

set -euo pipefail

APP_SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/ocr-v7"
SERVICE_USER="${SUDO_USER:-ocr}"
INPUT_ROOT="/data/ocr/inbox"
SETUP_SERVICE=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app-dir) APP_DIR="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        --input-root) INPUT_ROOT="$2"; shift 2 ;;
        --no-service) SETUP_SERVICE=0; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "这个脚本只能在 Linux 上运行。"

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done
[[ -n "$PYTHON_BIN" ]] || die "找不到 python3，请先安装 Python 3.10 以上版本。"
log "使用解释器：$PYTHON_BIN（$("$PYTHON_BIN" -V 2>&1)）"

if [[ "$APP_DIR" != "$APP_SOURCE_DIR" ]]; then
    log "复制代码到 $APP_DIR"
    mkdir -p "$APP_DIR"
    # 不覆盖已有的 .venv / .state / runs，避免把线上状态冲掉。
    tar -C "$APP_SOURCE_DIR" \
        --exclude='.venv' --exclude='.state' --exclude='runs' --exclude='.git' \
        --exclude='__pycache__' -cf - . | tar -C "$APP_DIR" -xf -
fi

log "创建 Python 虚拟环境：$APP_DIR/.venv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi

VENV_PY="$APP_DIR/.venv/bin/python"

# 先把自己是谁打出来。装依赖失败时，这几行往往就是答案
# （典型：aarch64 + 只有 x86_64 wheel 的镜像源 → psycopg[binary] 解析不出候选版本）。
log "环境信息"
"$VENV_PY" - <<'PY'
import platform, sys, sysconfig
print(f"  python   : {sys.version.split()[0]} ({platform.machine()})")
print(f"  platform : {sysconfig.get_platform()}")
PY
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
    warn "检测到 PIP_INDEX_URL（走镜像源）：$(printf '%s' "$PIP_INDEX_URL" | sed -E 's#//[^@/]*@#//***@#')"
    warn "镜像源缺 wheel 时会报「conflicting dependencies」，用官方源试一次往往就好了。"
fi

log "升级 pip"
"$VENV_PY" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/pip" -V

# 分开装：哪个包出问题一眼就能看出来，别让 --quiet 把原因一起吞掉。
log "安装 openpyxl / Pillow"
"$APP_DIR/.venv/bin/pip" install 'openpyxl>=3.1,<4' 'Pillow>=10,<13'

log "安装 PostgreSQL 驱动 psycopg"
# 优先预编译 wheel（自带 libpq，开箱即用）；某些平台/镜像源没有对应 wheel，
# 这时退回纯 Python 实现 + 系统 libpq5，功能完全一样，只是解析稍慢。
if ! "$APP_DIR/.venv/bin/pip" install 'psycopg[binary]>=3.2,<4'; then
    warn "psycopg[binary] 装不上，多半是当前平台没有对应的预编译 wheel。"
    warn "退回纯 Python 版 psycopg + 系统 libpq5（功能一致，性能略低）。"
    if command -v apt-get >/dev/null 2>&1; then
        # 这两个变量缺一不可：
        #   DEBIAN_FRONTEND=noninteractive 管 debconf 的对话框；
        #   NEEDRESTART_SUSPEND=1          管 Ubuntu 的「哪些服务需要重启」交互提示
        #   —— 少了后者，无人值守时会一直卡在
        #      "Which services should be restarted? []" 上等输入。
        export DEBIAN_FRONTEND=noninteractive
        export NEEDRESTART_SUSPEND=1
        apt-get update -qq || warn "apt-get update 失败，继续尝试"
        apt-get install -y libpq5 || warn "libpq5 安装失败，继续尝试"
    fi
    "$APP_DIR/.venv/bin/pip" install 'psycopg>=3.2,<4' \
        || die "psycopg 装不上。请检查这台机器到 PyPI（或镜像源）的网络，以及 uname -m 是否被镜像源支持。"
fi

log "校验依赖可导入"
"$VENV_PY" - <<'PY' || die "依赖校验失败，上面的报错就是原因。"
import openpyxl
import PIL
from psycopg import pq
print(f"  openpyxl {openpyxl.__version__} / Pillow {PIL.__version__}")
try:
    print(f"  psycopg 后端={pq.__impl__} libpq={pq.version()}")
except Exception as exc:  # libpq 缺失时这里会炸，必须让脚本停下来
    raise SystemExit(f"psycopg 已安装但无法加载 libpq：{exc}")
PY

log "创建数据目录"
mkdir -p "$INPUT_ROOT" /var/lib/ocr-v7/state /var/lib/ocr-v7/runs /etc/ocr-v7

if [[ ! -f /etc/ocr-v7/ocr-daemon.env ]]; then
    log "生成 /etc/ocr-v7/ocr-daemon.env（模板，密钥需要手工填写）"
    cp "$APP_DIR/deploy/ocr-daemon.env.example" /etc/ocr-v7/ocr-daemon.env
    sed -i "s#^OCR_INPUT_ROOT=.*#OCR_INPUT_ROOT=$INPUT_ROOT#" /etc/ocr-v7/ocr-daemon.env
else
    warn "/etc/ocr-v7/ocr-daemon.env 已存在，保留不动。"
fi
chmod 600 /etc/ocr-v7/ocr-daemon.env

if [[ "$SETUP_SERVICE" == "1" ]]; then
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        log "创建运行账号 $SERVICE_USER"
        useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    chown -R "$SERVICE_USER":"$SERVICE_USER" /var/lib/ocr-v7 "$INPUT_ROOT" "$APP_DIR"
    log "注册 systemd 服务 /etc/systemd/system/ocr-v7-daemon.service"
    sed -e "s#^User=.*#User=$SERVICE_USER#" \
        -e "s#^Group=.*#Group=$SERVICE_USER#" \
        -e "s#/opt/ocr-v7#$APP_DIR#g" \
        "$APP_DIR/deploy/ocr-v7-daemon.service" > /etc/systemd/system/ocr-v7-daemon.service
    systemctl daemon-reload
    log "服务已注册。请先填好 /etc/ocr-v7/ocr-daemon.env，再执行："
    echo "    sudo /opt/ocr-v7/.venv/bin/python $APP_DIR/email_alert.py --self-test   # 测邮件"
    echo "    sudo systemctl enable --now ocr-v7-daemon"
else
    log "已跳过 systemd 注册（--no-service）"
fi

log "安装完成。"
