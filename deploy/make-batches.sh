#!/usr/bin/env bash
#
# make-batches.sh —— 把一大堆商品目录拆成收件目录下的若干「批次目录」。
#
# 为什么需要这个脚本：
#   守护进程的批次发现规则是「收件目录下的一级子目录，且它自己还包含商品子目录」。
#   也就是必须长这样：
#
#       /data/ocr/inbox/
#         batch-001/            <- 一级目录 == 一个批次
#           100003043680/       <- 商品目录（里面放图）
#             a.jpg
#           100003043681/
#             b.jpg
#
#   如果直接把一个个商品目录平铺丢进 inbox（inbox/100003043680/a.jpg），
#   守护进程扫描时会认为「这个一级目录里没有商品子目录」，于是**一个都不跑**。
#   一万个商品全平铺进 inbox，最坏情况是跑完一轮什么都没发生。
#
#   另外，把一万个商品塞进同一个批次也不合适：批次汇总邮件只在整批跑完时发一封，
#   中途没有任何进度感知；拆成每批 500 个，等于每跑完 500 个就有一封结果邮件。
#
# 用法：
#   # 先看看会怎么拆（不移动任何文件）
#   sudo bash deploy/make-batches.sh --src /data/ocr/staging --dry-run
#
#   # 确认无误后真拆
#   sudo bash deploy/make-batches.sh --src /data/ocr/staging --size 500
#
#   # 商品已经平铺在 inbox 里，就地整理（src 和 inbox 同一个目录）
#   sudo bash deploy/make-batches.sh --src /data/ocr/inbox --inbox /data/ocr/inbox --size 500
#
# 注意：src 和 inbox 在同一个文件系统时，mv 只是改目录项，上万个目录也是秒级完成；
#       跨文件系统（比如 staging 在 /，inbox 在 /data）会变成真实拷贝，很慢。
#
set -euo pipefail

SRC=""
INBOX="/data/ocr/inbox"
SIZE=500
PREFIX="batch"
DRY_RUN=0
FORCE=0

usage() {
    sed -n '3,40p' "$0" | sed 's/^#\{0,1\} \{0,1\}//'
}

die() { echo "错误：$*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --src)     SRC="${2:-}";     shift 2 ;;
        --inbox)   INBOX="${2:-}";   shift 2 ;;
        --size)    SIZE="${2:-}";    shift 2 ;;
        --prefix)  PREFIX="${2:-}";  shift 2 ;;
        --dry-run) DRY_RUN=1;        shift ;;
        --force)   FORCE=1;          shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（用 --help 看用法）" ;;
    esac
done

[[ -n "$SRC" ]] || { usage; exit 2; }
[[ "$SIZE" =~ ^[1-9][0-9]*$ ]] || die "--size 必须是正整数，当前是 '$SIZE'"
[[ -d "$SRC" ]]   || die "找不到源目录：$SRC"
[[ -d "$INBOX" ]] || die "找不到收件目录：$INBOX"

SRC="$(cd "$SRC" && pwd -P)"
INBOX="$(cd "$INBOX" && pwd -P)"

# 守护进程正在跑的时候搬目录，会让它读到半个批次。除非明确 --force，否则停下。
if [[ "$FORCE" != "1" ]] && command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet ocr-v7-daemon 2>/dev/null; then
        die "ocr-v7-daemon 正在运行。先 sudo systemctl stop ocr-v7-daemon 再拆批（或加 --force 自负风险）。"
    fi
fi

if [[ "$SRC" == "$INBOX" ]]; then
    echo "源目录与收件目录相同，做「就地整理」。"
else
    src_dev="$(stat -c '%d' "$SRC")"
    dst_dev="$(stat -c '%d' "$INBOX")"
    if [[ "$src_dev" == "$dst_dev" ]]; then
        echo "源目录与收件目录在同一文件系统，移动是瞬时的（只改目录项）。"
    else
        echo "警告：源目录与收件目录不在同一文件系统，移动会变成真实拷贝，一万个商品可能要很久。"
    fi
fi

# --------------------------------------------------------------------------- #
# 1. 找出「哪些一级子目录里面真的有图」
#    一次 find 扫完，比每个目录各起一个 find 快得多。
# --------------------------------------------------------------------------- #
declare -A HAS_IMAGE=()
while IFS= read -r rel; do
    [[ -n "$rel" ]] && HAS_IMAGE["${rel%%/*}"]=1
done < <(
    find "$SRC" -mindepth 2 -type f \
        \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \
           -o -iname '*.bmp' -o -iname '*.tif' -o -iname '*.tiff' \) \
        -printf '%P\n'
)

# --------------------------------------------------------------------------- #
# 2. 收集待拆分的一级子目录（按自然序：会处理 xx-2 / xx-10 这类编号）
#    已存在的 batch-NNN 目录要排除掉，否则第二次运行会把自己刚建出来的批次再拆一遍。
# --------------------------------------------------------------------------- #
ITEMS=()
while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    [[ "$name" =~ ^${PREFIX}-[0-9]+$ ]] && continue
    ITEMS+=("$name")
done < <(
    find "$SRC" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
        | sort -V
)

TOTAL=${#ITEMS[@]}
WITH_IMAGE=0
for item in "${ITEMS[@]}"; do
    [[ -n "${HAS_IMAGE[$item]:-}" ]] && WITH_IMAGE=$((WITH_IMAGE + 1))
done

echo
echo "源目录      : $SRC"
echo "收件目录    : $INBOX"
echo "一级子目录  : $TOTAL 个"
echo "其中含图片  : $WITH_IMAGE 个"
echo "每批商品数  : $SIZE"
echo "批次命名    : ${PREFIX}-001, ${PREFIX}-002, ..."

if [[ "$TOTAL" -eq 0 ]]; then
    echo
    echo "源目录下没有任何一级子目录，没什么可拆的。"
    exit 0
fi

if [[ "$WITH_IMAGE" -eq 0 ]]; then
    echo
    echo "！！ 一级子目录里一张图都没找到。"
    echo "   说明图片还在更深一层，也就是说 $SRC 下面第一层就已经是商品组了，"
    echo "   守护进程会把整个 $SRC 当成「一个批次」处理。"
    echo "   如果这本就是你要的，直接把 $SRC 移进 $INBOX 即可，不用拆。"
    exit 1
fi

if [[ "$WITH_IMAGE" -lt "$TOTAL" ]]; then
    echo
    echo "提示：有 $((TOTAL - WITH_IMAGE)) 个一级子目录里没有图片，会被当成商品目录照常拆进去。"
    echo "      如果它们是 noise（临时文件、日志目录），建议先清理再跑。"
fi

BATCHES=$(( (WITH_IMAGE + SIZE - 1) / SIZE ))
echo "预计拆成    : $BATCHES 个批次"
echo

if [[ "$DRY_RUN" == "1" ]]; then
    echo "（--dry-run：只列计划，不移动任何文件）"
fi

# --------------------------------------------------------------------------- #
# 3. 分批搬运
# --------------------------------------------------------------------------- #
declare -A CREATED=()
batch_no=0
index=0
for item in "${ITEMS[@]}"; do
    if (( index % SIZE == 0 )); then
        batch_no=$((batch_no + 1))
        name=$(printf '%s-%03d' "$PREFIX" "$batch_no")
        target="$INBOX/$name"
        CREATED[$name]=1
        echo "[$name]"
        if [[ "$DRY_RUN" != "1" ]]; then
            [[ -e "$target" ]] && die "目标已存在：$target（先改名或删掉，别覆盖）"
            mkdir -p "$target"
        fi
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "    - $item"
    else
        mv -- "$SRC/$item" "$target/"
    fi
    index=$((index + 1))
done

echo
if [[ "$DRY_RUN" == "1" ]]; then
    echo "演练结束，没有动任何文件。去掉 --dry-run 即可真正执行。"
else
    echo "完成：$index 个商品目录，拆成 $batch_no 个批次，已放进 $INBOX。"
    echo
    echo "接下来："
    echo "    sudo systemctl start ocr-v7-daemon      # 启动守护进程"
    echo "    sudo journalctl -u ocr-v7-daemon -f     # 看日志"
    echo
    echo "提醒：一个批次的汇总邮件只在整批跑完时才发。想早点看到结果，就把 --size 调小。"
fi
