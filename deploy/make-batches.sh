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
# 安全策略：先把所有目标批次名算出来，确认没有一个跟已有目录撞名，再开始搬。
#           不会出现「搬到一半发现撞名、留下半个批次」的情况。
#
# 其它开关：
#   --prefix NAME        批次名前缀，默认 batch
#   --start N            起始批次号，默认 1（撞名时用它往上跳，别覆盖旧批次）
#   --force              守护进程正在跑也照拆（它可能读到半个批次，慎用）
#   --include-no-image   连「目录里没有图」的一级目录也拆进去（默认跳过，它们不可能是商品）
#   --allow-container    确实要把容器目录当成一个商品时用（默认会拦下来）
#   --help               看这段说明
#
set -euo pipefail

SRC=""
INBOX="/data/ocr/inbox"
SIZE=500
PREFIX="batch"
START=1
DRY_RUN=0
FORCE=0
INCLUDE_NO_IMAGE=0
ALLOW_CONTAINER=0

usage() {
    # 把文件开头的注释整块当说明打出来，改注释不用同步改行号
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

die() { echo "错误：$*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --src)     SRC="${2:-}";     shift 2 ;;
        --inbox)   INBOX="${2:-}";   shift 2 ;;
        --size)    SIZE="${2:-}";    shift 2 ;;
        --prefix)  PREFIX="${2:-}";  shift 2 ;;
        --start)   START="${2:-}";   shift 2 ;;
        --dry-run) DRY_RUN=1;        shift ;;
        --force)   FORCE=1;          shift ;;
        --include-no-image) INCLUDE_NO_IMAGE=1; shift ;;
        --allow-container)  ALLOW_CONTAINER=1;   shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（用 --help 看用法）" ;;
    esac
done

[[ -n "$SRC" ]] || { usage; exit 2; }
[[ "$SIZE"  =~ ^[1-9][0-9]*$ ]] || die "--size 必须是正整数，当前是 '$SIZE'"
[[ "$START" =~ ^[1-9][0-9]*$ ]] || die "--start 必须是正整数，当前是 '$START'"
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
# 1. 先摸清楚上级目录里都有什么
#    一次 find 扫完所有图片，比每个目录各起一个 find 快得多（一万个目录能差几十秒）。
# --------------------------------------------------------------------------- #
declare -A HAS_IMAGE=()
declare -A HAS_DIRECT_IMAGE=()   # 图片就在这一层（item/xxx.jpg）—— 正常的商品目录长这样
declare -A HAS_NESTED_IMAGE=()   # 图片在更深一层（item/sub/xxx.jpg）—— 可能是个容器
while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    item="${rel%%/*}"
    rest="${rel#*/}"
    HAS_IMAGE["$item"]=1
    if [[ "$rest" == */* ]]; then
        HAS_NESTED_IMAGE["$item"]=1
    else
        HAS_DIRECT_IMAGE["$item"]=1
    fi
done < <(
    find "$SRC" -mindepth 2 -type f \
        \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \
           -o -iname '*.bmp' -o -iname '*.tif' -o -iname '*.tiff' \) \
        -printf '%P\n'
)

# --------------------------------------------------------------------------- #
# 2. 收集待拆分的一级子目录（按自然序：xx-2 会排在 xx-10 前面）
#    已存在的 batch-NNN 要排除掉，否则第二次运行会把上次切好的批次再切一遍。
#    默认只收「里面有图」的目录 —— 没有图的目录无论如何都不会被当成商品
#    （守护进程的 product_candidates 要求目录里必须有支持的图片），
#    留着只会白占一个名额。
# --------------------------------------------------------------------------- #
declare -A SEEN=()
ALL_ITEMS=()
while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    [[ "$name" =~ ^${PREFIX}-[0-9]+$ ]] && continue
    [[ -n "${SEEN[$name]:-}" ]] && continue
    SEEN["$name"]=1
    ALL_ITEMS+=("$name")
done < <(
    find "$SRC" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
        | sort -V
)

ITEMS=()
NO_IMAGE=0
for item in "${ALL_ITEMS[@]}"; do
    if [[ -n "${HAS_IMAGE[$item]:-}" ]]; then
        ITEMS+=("$item")
    else
        NO_IMAGE=$((NO_IMAGE + 1))
        [[ "$INCLUDE_NO_IMAGE" == "1" ]] && ITEMS+=("$item")
    fi
done

TOTAL=${#ALL_ITEMS[@]}
COUNT=${#ITEMS[@]}

echo
echo "源目录        : $SRC"
echo "收件目录      : $INBOX"
echo "一级子目录    : $TOTAL 个"
echo "其中含图片    : $((TOTAL - NO_IMAGE)) 个"
echo "本次要拆      : $COUNT 个"
echo "每批商品数    : $SIZE"
echo "起始批次号    : $START"

if [[ "$TOTAL" -eq 0 ]]; then
    echo
    echo "源目录下没有任何一级子目录，没什么可拆的。"
    exit 0
fi

if [[ "$COUNT" -eq 0 ]]; then
    echo
    echo "！！ 一级子目录里一张图都没找到。"
    if [[ "$SRC" == "$INBOX" ]]; then
        echo "   收件目录目前只有 $TOTAL 个一级子目录且不含任何图片，通常是两种原因："
        echo "   1. 上传工具（Xftp 等）还在传，图片文件还没到齐 —— 等传完再跑本脚本；"
        echo "   2. 商品目录被包在一个容器目录里（如 inbox/jd_image/商品ID/图.jpg）"
        echo "      —— 传完后用 ls $INBOX 确认名字，再执行："
        echo "      sudo bash $0 --src $INBOX/<容器目录名> --inbox $INBOX --size $SIZE"
    else
        echo "   说明图片还在更深一层，也就是说 $SRC 的第一层本身就已经是「商品组」，"
        echo "   守护进程会把整个 $SRC 当成一个批次。"
        echo "   如果这本就是你要的，直接把 $SRC 移进 $INBOX 就行，不用拆。"
    fi
    exit 1
fi

if [[ "$NO_IMAGE" -gt 0 ]]; then
    echo
    if [[ "$INCLUDE_NO_IMAGE" == "1" ]]; then
        echo "注意：勾选了 --include-no-image，这 $NO_IMAGE 个没图的目录也会被拆进去，"
        echo "      守护进程会忽略它们（不会成为商品），但会占掉本批的名额。"
    else
        echo "已跳过 $NO_IMAGE 个没有图片的目录（它们不可能成为商品）："
        shown=0
        for item in "${ALL_ITEMS[@]}"; do
            [[ -n "${HAS_IMAGE[$item]:-}" ]] && continue
            echo "    - $item"
            shown=$((shown + 1))
            [[ "$shown" -ge 10 ]] && { echo "    ... 其余省略"; break; }
        done
    fi
fi

CONTAINERS=()
for item in "${ITEMS[@]}"; do
    [[ -n "${HAS_NESTED_IMAGE[$item]:-}" ]] || continue
    [[ -n "${HAS_DIRECT_IMAGE[$item]:-}" ]] && continue
    CONTAINERS+=("$item")
done

if [[ "${#CONTAINERS[@]}" -gt 0 ]]; then
    echo
    echo "！！ 有几个目录看着是「容器」，不像商品。"
    echo "   它们的图不在自己这一层，而在更深一层，说明它们本身就是装商品目录的文件夹"
    echo "   （比如你直接把 jd_image 这个总目录整包上传了）："
    for item in "${CONTAINERS[@]}"; do
        echo "    - $item"
    done
    echo
    echo "   一个商品目录长这样：  100003043680/  <- 图直接放在这里面"
    echo "   一个容器长这样：      jd_image/100003043680/  <- 还会再套一层"
    echo
    echo "   如果不加处理就拆，上面这个容器会占掉一个商品名额，它下面的图会全部算成"
    echo "   「同一个商品」的图片 —— 白跑很久，还出一份废数据。"
    if [[ "$ALLOW_CONTAINER" != "1" ]]; then
        echo
        echo "   一个文件都还没动。正确做法是把 --src 指到更深一层，也就是商品目录所在的那一层："
        echo "        sudo bash $0 --src $SRC/<容器名> --inbox $INBOX --size $SIZE --dry-run"
        echo "   确认 $SRC/<容器名> 打开后每个子目录里直接就是图，去掉 --dry-run 再跑一次。"
        echo "   （确实想把容器当商品用，再加 --allow-container。）"
        exit 4
    fi
    echo
    echo "   --allow-container 已指定，继续按容器处理。"
fi

BATCHES=$(( (COUNT + SIZE - 1) / SIZE ))
echo "预计拆成      : $BATCHES 个批次"
echo

# --------------------------------------------------------------------------- #
# 3. 预检：把所有目标名先算出来，确认一个都没撞名
# --------------------------------------------------------------------------- #
declare -A TARGET_OF=()
batch_no=0
index=0
for item in "${ITEMS[@]}"; do
    if (( index % SIZE == 0 )); then
        batch_no=$((batch_no + 1))
        TARGET_OF["$batch_no"]=$(printf '%s-%03d' "$PREFIX" "$((START + batch_no - 1))")
    fi
    index=$((index + 1))
done

CLASH=0
for ((n = 1; n <= batch_no; n++)); do
    target="$INBOX/${TARGET_OF[$n]}"
    if [[ -e "$target" ]]; then
        echo "撞名：$target 已经存在"
        CLASH=1
    fi
done
if [[ "$CLASH" == "1" ]]; then
    echo
    echo "一个文件都还没动。换个批次号或前缀重试，例如："
    echo "    ... --start $((START + batch_no))          # 从更大的号往下编"
    echo "    ... --prefix run2                          # 换一眼前缀"
    echo "    ... --dry-run                              # 先看看会拆成什么样"
    exit 3
fi
echo "预检通过：$batch_no 个目标批次名都可用。"

if [[ "$DRY_RUN" == "1" ]]; then
    echo
    echo "（--dry-run：只列计划，不移动任何文件）"
fi

# --------------------------------------------------------------------------- #
# 4. 真正搬运
# --------------------------------------------------------------------------- #
batch_no=0
index=0
for item in "${ITEMS[@]}"; do
    if (( index % SIZE == 0 )); then
        batch_no=$((batch_no + 1))
        name="${TARGET_OF[$batch_no]}"
        target="$INBOX/$name"
        echo "[$name]"
        if [[ "$DRY_RUN" != "1" ]]; then
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
