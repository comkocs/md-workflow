#!/usr/bin/env bash
# 每日备份。装进 cron,例如:
#   0 3 * * * <安装目录>/app/extensions/server_deploy/backup.sh --install-dir <安装目录>
# 备份走 `ticket.py dump`,不是直接拷 .sqlite 文件:服务开着 WAL,直接拷到的可能是一份缺最后几笔的库。
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/srv}"
INSTALL_DIR="${INSTALL_DIR:-$INSTALL_ROOT/ticket-desk}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    *) echo "拦下:不认识的备份参数：$1" >&2; exit 2 ;;
  esac
done
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("拦下:工单台要求 Python 3.10 或更高版本。")
PY
[[ "$INSTALL_DIR" == "$INSTALL_ROOT"/* && "$INSTALL_DIR" != "$INSTALL_ROOT" ]] || { echo "拦下:安装目录必须是 $INSTALL_ROOT 下的独立目录。" >&2; exit 2; }
BACKUPS="$INSTALL_DIR/backups"
TARGET="$BACKUPS/$(date +%F)"
install -d "$TARGET"
python3 "$INSTALL_DIR/app/tools/tickets/ticket.py" dump --db "$INSTALL_DIR/db/tickets.sqlite" --to "$TARGET"
find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d -mtime +13 -exec rm -rf -- {} +
echo "备份完成：$TARGET；仅保留最近 14 天。"
