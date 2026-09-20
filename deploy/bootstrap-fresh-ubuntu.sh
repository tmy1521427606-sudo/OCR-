#!/usr/bin/env bash
# OCR v7 —— 全新 Ubuntu 服务器引导脚本
#
# 适用于「刚装好系统、还没装任何依赖」的 Ubuntu 22.04 / 24.04（含 minimized 精简版）。
# 它只做三件事，做完把控制权交给 deploy/install-linux.sh：
#   1. 装系统依赖：python3 / python3-venv / python3-pip / git / ca-certificates / curl；
#   2. 连通性预检：内网 OCR、PostgreSQL、GitHub、DashScope、163 SMTP —— 只报告，不阻断；
#   3. 调用 install-linux.sh 建 venv、装 Python 依赖、建目录、写 env、注册 systemd。
#
# 用法（在仓库根目录执行，需要 root 或 sudo）：
#   sudo bash deploy/bootstrap-fresh-ubuntu.sh
#   sudo bash deploy/bootstrap-fresh-ubuntu.sh --app-dir /opt/ocr-v7 --user ubuntu
#   sudo bash deploy/bootstrap-fresh-ubuntu.sh --skip-deps        # 依赖已装过，跳过 apt
#   sudo bash deploy/bootstrap-fresh-ubuntu.sh --check-only       # 只做连通性预检
#
# 其余参数原样透传给 install-linux.sh（--input-root / --no-service 等）。

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SCRIPT="$SELF_DIR/install-linux.sh"

SKIP_DEPS=0
CHECK_ONLY=0
PASSTHRU=()

# 预检默认值（若 /etc/ocr-v7/ocr-daemon.env 已存在，则以其为准）
DEFAULT_OCR_URL="http://192.168.1.115:8870/v1/ocr"
DEFAULT_DB_HOST="192.168.0.23"
DEFAULT_DB_PORT="5432"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-deps)  SKIP_DEPS=1; shift ;;
        --check-only) CHECK_ONLY=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) PASSTHRU+=("$1"); shift ;;
    esac
done

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[  ok  ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ warn ]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[ fail ]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "这个脚本只能在 Linux 上运行。"
[[ "$(id -u)" == "0" ]] || die "请用 root 运行：sudo bash deploy/bootstrap-fresh-ubuntu.sh"

# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------
if [[ "$SKIP_DEPS" == "0" && "$CHECK_ONLY" == "0" ]]; then
    log "apt-get update"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq

    MISSING=()
    command -v git     >/dev/null 2>&1 || MISSING+=(git)
    command -v curl    >/dev/null 2>&1 || MISSING+=(curl)
    command -v python3 >/dev/null 2>&1 || MISSING+=(python3)
    # python3 在但 venv 模块缺失，是 Ubuntu minimized 镜像最常见的情况。
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import venv' >/dev/null 2>&1 || MISSING+=(python3-venv)
        python3 -c 'import ensurepip' >/dev/null 2>&1 || MISSING+=(python3-pip)
    fi

    if (( ${#MISSING[@]} )); then
        log "安装系统依赖：${MISSING[*]}"
        apt-get install -y -qq ca-certificates "${MISSING[@]}"
    else
        ok "系统依赖已齐全"
    fi

    # 校验 Python 版本满足 install-linux.sh 的要求（>=3.10）
    PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "Python 版本过低（$PYV），install-linux.sh 需要 3.10 以上。"
    ok "python3 = $PYV，python3 -m venv / ensurepip 可用"
fi

# ---------------------------------------------------------------------------
# 连通性预检（只报告，不阻断）
# ---------------------------------------------------------------------------
env_value() {  # key default
    local key="$1" default="$2" file="/etc/ocr-v7/ocr-daemon.env" line=""
    if [[ -f "$file" ]]; then
        line="$(grep -E "^${key}=" "$file" | tail -1 || true)"
        [[ -n "$line" ]] && { printf '%s' "${line#*=}"; return; }
    fi
    printf '%s' "$default"
}

url_host() { printf '%s' "${1#*://}" | cut -d/ -f1 | cut -d: -f1; }
url_port() {
    local rest port
    rest="$(printf '%s' "${1#*://}" | cut -d/ -f1)"
    port="$(printf '%s' "$rest" | cut -s -d: -f2)"
    [[ -n "$port" ]] && printf '%s' "$port" || printf '%s' "${2:-80}"
}

check_tcp() {  # host port label
    local host="$1" port="$2" label="$3"
    if timeout 5 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
        ok "$label  $host:$port  可达"
        return 0
    fi
    warn "$label  $host:$port  **不可达**"
    return 1
}

log "连通性预检（只报告，不阻断安装）"
FAILED=0
OCR_URL="$(env_value PADDLE_OCR_API_URL "$DEFAULT_OCR_URL")"
DB_HOST="$(env_value POSTGRES_HOST "$DEFAULT_DB_HOST")"
DB_PORT="$(env_value POSTGRES_PORT "$DEFAULT_DB_PORT")"
SMTP_HOST="$(env_value ALERT_SMTP_HOST "smtp.163.com")"
SMTP_PORT="$(env_value ALERT_SMTP_PORT "465")"

check_tcp "$(url_host "$OCR_URL")" "$(url_port "$OCR_URL" 8870)" "内网 OCR   " || FAILED=1
check_tcp "$DB_HOST" "$DB_PORT" "PostgreSQL " || FAILED=1
check_tcp github.com 443 "GitHub     " || FAILED=1
check_tcp dashscope.aliyuncs.com 443 "DashScope  " || FAILED=1
check_tcp "$SMTP_HOST" "$SMTP_PORT" "163 SMTP   " || FAILED=1

if [[ "$FAILED" == "1" ]]; then
    warn "上面有不可达的地址。安装可以继续，但跑批次前必须解决："
    warn "  · 内网 OCR / PostgreSQL 不通 → 检查是否同一网段、防火墙、或者这台机器需要跳板路由"
    warn "  · GitHub 不通 → 改用 scp 传代码，或在能上网的机器上打好 tar 包再传"
    warn "  · DashScope / SMTP 不通 → 检查出网策略；SMTP 不通会导致收不到报警邮件"
else
    ok "所有外部依赖都通"
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
    log "只做预检（--check-only），退出。"
    exit 0
fi

# ---------------------------------------------------------------------------
# 交给 install-linux.sh
# ---------------------------------------------------------------------------
[[ -f "$INSTALL_SCRIPT" ]] || die "找不到 $INSTALL_SCRIPT，请在仓库根目录执行本脚本。"
log "调用 install-linux.sh ${PASSTHRU[*]:-}"
bash "$INSTALL_SCRIPT" ${PASSTHRU[@]+"${PASSTHRU[@]}"}

HINT_USER="${SUDO_USER:-root}"
HINT_INPUT="$(env_value OCR_INPUT_ROOT "/data/ocr/inbox")"
# 从透传参数里取 --app-dir，保证提示里的路径和实际装的位置一致
HINT_APP="/opt/ocr-v7"
for ((i = 0; i < ${#PASSTHRU[@]}; i++)); do
    if [[ "${PASSTHRU[$i]}" == "--app-dir" && $((i + 1)) -lt ${#PASSTHRU[@]} ]]; then
        HINT_APP="${PASSTHRU[$((i + 1))]}"
    fi
done

echo
log "下一步（必须手工做的只剩填密钥）"
printf '  1) sudo vi /etc/ocr-v7/ocr-daemon.env\n'
printf '       必填：DASHSCOPE_API_KEY / POSTGRES_PASSWORD / ALERT_SMTP_USER / ALERT_SMTP_PASSWORD\n'
printf '       确认：OCR_DEMO_KEYS_ROTATED=YES\n'
printf '  2) 测邮件：sudo %s/.venv/bin/python %s/email_alert.py --self-test\n' "$HINT_APP" "$HINT_APP"
printf '  3) 试跑一轮：sudo -u %s %s/.venv/bin/python %s/ocr_daemon.py --input-root %s --platform jd --once\n' "$HINT_USER" "$HINT_APP" "$HINT_APP" "$HINT_INPUT"
printf '  4) 常驻：sudo systemctl enable --now ocr-v7-daemon && sudo journalctl -u ocr-v7 -f\n'
printf '\n批次目录结构：%s/<批次名>/<product_id>/*.jpg\n' "$HINT_INPUT"
