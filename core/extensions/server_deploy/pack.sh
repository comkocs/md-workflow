#!/usr/bin/env bash
set -euo pipefail
# 上服包打包器。
#
# ★为什么要有这个脚本,而不是让人手敲 git archive:
#   `git archive` 打出来的包**没有 .git 目录**,于是 update.sh 里那句 `git rev-parse HEAD`
#   永远拿不到部署头,「后置闸通过之后自动建上服记录」这条路**每次都走兜底、从不生效**。
#   根治办法只有一个:**打包那一刻就把提交号放进包里**。
#
# 用法:
#   bash extensions/server_deploy/pack.sh [输出路径] [提交号]
#   默认输出 /tmp/desk.tar,默认打当前 HEAD。在哪个目录下敲都行:按脚本自己的位置找到包根(core/)。
#
# 包根是 core/ 目录:包里是 tools/tickets、tools/browser、extensions、desk_config.json 四样。
# core/ 可以是仓根,也可以是某个仓里的子目录——用「<提交>:<子目录>」这种 tree-ish 让它当包根,
# 两种布局打出来的包完全一样。
#
# 打完会把 sha256 打在最后一行——两端核指纹用它,别再另跑一次 sha256sum。

OUT="${1:-/tmp/desk.tar}"
REF="${2:-HEAD}"
case "$OUT" in /*|[A-Za-z]:*) ;; *) OUT="$PWD/$OUT" ;; esac

command -v git >/dev/null 2>&1 || { echo "拦下:找不到 git。" >&2; exit 2; }
CORE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$CORE_DIR"
git rev-parse --git-dir >/dev/null 2>&1 || { echo "拦下:$CORE_DIR 不在 git 仓里。" >&2; exit 2; }

HEAD_SHORT="$(git rev-parse --short=9 "$REF")"
PACK_PATHS=(tools/tickets tools/browser extensions desk_config.json)
# ★工作树脏就拦:打包打的是 $REF 那个**提交**,工作区里未提交的改动一个字都不会进包。
#   不拦的话,人改完没提交就打包上服,线上跑的是旧代码而他以为是新的——
#   这种「上服了但没生效」最难查,因为哪一步都没报错。
if [[ -n "$(git status --porcelain -- "${PACK_PATHS[@]}")" ]]; then
  echo "拦下:${PACK_PATHS[*]} 里有未提交的改动。" >&2
  echo "  打包打的是提交 $HEAD_SHORT,工作区里没提交的东西进不了包。" >&2
  echo "  请先提交(或 git stash),再重跑本脚本。" >&2
  git status --short -- "${PACK_PATHS[@]}" >&2
  exit 2
fi

TOPLEVEL="$(git rev-parse --show-toplevel)"
PREFIX="$(git rev-parse --show-prefix)"
TREEISH="$REF"
if [[ -n "$PREFIX" ]]; then TREEISH="$REF:${PREFIX%/}"; fi
# --add-virtual-file=<路径>:<内容>（git 2.38+）：不落临时文件，直接把提交号写进包内固定位置。
( cd "$TOPLEVEL" && git archive --format=tar \
    --add-virtual-file="extensions/server_deploy/DEPLOY_HEAD:$HEAD_SHORT" \
    "$TREEISH" "${PACK_PATHS[@]}" ) > "$OUT"

# ★★打完必须**回验**,不许只打印一句「已写进包内」就完事。
#   这个脚本的第一版就是这么错的:用了一个根本不存在的 git 选项,错误被 2>/dev/null 吞掉,
#   而脚本照样打印「部署头已写进包内」——**包里其实什么都没有**。报成功、没做成、没人发现。
PACKED="$(tar xOf "$OUT" extensions/server_deploy/DEPLOY_HEAD 2>/dev/null | tr -d '[:space:]' || true)"
if [[ "$PACKED" != "$HEAD_SHORT" ]]; then
  echo "拦下:部署头没能写进包里(读回「${PACKED:-空}」,期望「$HEAD_SHORT」)。" >&2
  echo "  多半是这台机器的 git 不支持 --add-virtual-file(需要 2.38+):$(git --version)" >&2
  echo "  升级 git,或上服时显式带上环境变量:DEPLOY_HEAD=$HEAD_SHORT sudo bash …/update.sh …" >&2
  rm -f "$OUT"   # 半成品不留在盘上,免得有人拿它去上服
  exit 3
fi

echo "包已打好:$OUT"
echo "  来自提交:$HEAD_SHORT($REF)"
echo "  部署头已写进包内并回验通过 ⇒ update.sh 会读它自动建上服记录(需服务端启用扩展 deploy_record)。"
sha256sum "$OUT"
