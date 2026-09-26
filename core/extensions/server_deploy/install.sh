#!/usr/bin/env bash
set -euo pipefail
# 工单台首次安装到一台 Linux 服务器(systemd)。只做一次;之后每一次上服走 update.sh(它有三道前置闸)。
#
# 目录布局故意把**代码**与**状态**分开:
#   $INSTALL_DIR/app      代码,每次上服整个替换
#   $INSTALL_DIR/db       SQLite 库与服务令牌  ┐
#   $INSTALL_DIR/img      入单的图片            ├ 上服脚本一个字都不碰
#   $INSTALL_DIR/certs    证书                  ┘
#   $INSTALL_DIR/backups  backup.sh 的落点
#
# 名字全是变量,默认值是通用名:
#   INSTALL_ROOT  安装根,默认 /srv;server.json 里的「安装目录」必须是它下面的独立子目录
#   SERVICE_NAME  systemd 服务名(不带 .service),默认 ticket-desk
#   SERVICE_USER  跑服务的系统账号,默认 ticket-desk(不存在就建一个系统账号)

CONFIG=""
SOURCE=""
DATA_ROOT=""
REOPEN_SETUP=0
ROTATE_TOKEN=0
INSTALL_ROOT="${INSTALL_ROOT:-/srv}"
SERVICE_NAME="${SERVICE_NAME:-ticket-desk}"
SERVICE_USER="${SERVICE_USER:-ticket-desk}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --data-root) DATA_ROOT="$2"; shift 2 ;;
    --reopen-setup) REOPEN_SETUP=1; shift ;;
    --rotate-token) ROTATE_TOKEN=1; shift ;;
    *) echo "拦下:不认识的安装参数：$1" >&2; exit 2 ;;
  esac
done

[[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "拦下:必须用 --config 指向 server.json。" >&2; exit 2; }
[[ -n "$SOURCE" && -d "$SOURCE/tools/tickets" && -d "$SOURCE/tools/browser" ]] || { echo "拦下:必须用 --source 指向工单台源码根目录。" >&2; exit 2; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("拦下:工单台要求 Python 3.10 或更高版本。")
PY

readarray -t SETTINGS < <(python3 - "$CONFIG" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8-sig"))
keys = ("域名", "端口", "安装目录", "管理员用户名")
missing = [key for key in keys if str(value.get(key, "")).strip() == ""]
if missing:
    raise SystemExit("拦下:server.json 缺少：" + "、".join(missing))
for key in keys:
    print(value[key])
PY
)
DOMAIN="${SETTINGS[0]}"
PORT="${SETTINGS[1]}"
INSTALL_DIR="${SETTINGS[2]}"
ADMIN_USER="${SETTINGS[3]}"
[[ "$INSTALL_DIR" == "$INSTALL_ROOT"/* && "$INSTALL_DIR" != "$INSTALL_ROOT" ]] || { echo "拦下:安装目录必须是 $INSTALL_ROOT 下的独立目录。" >&2; exit 2; }

APP="$INSTALL_DIR/app"
DB_DIR="$INSTALL_DIR/db"
IMG_DIR="$INSTALL_DIR/img"
LOG_DIR="$INSTALL_DIR/log"
CERT_DIR="$INSTALL_DIR/certs"
DB="$DB_DIR/tickets.sqlite"
TOKEN_FILE="$DB_DIR/service.token"
CERT="$CERT_DIR/server.crt"
KEY="$CERT_DIR/server.key"

id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP/tools" "$DB_DIR" "$IMG_DIR" "$LOG_DIR" "$CERT_DIR" "$INSTALL_DIR/backups"
rm -rf "$APP/tools/tickets" "$APP/tools/browser" "$APP/extensions"
cp -a "$SOURCE/tools/tickets" "$APP/tools/tickets"
cp -a "$SOURCE/tools/browser" "$APP/tools/browser"
if [[ -d "$SOURCE/extensions" ]]; then cp -a "$SOURCE/extensions" "$APP/extensions"; fi
if [[ -f "$SOURCE/desk_config.json" ]]; then cp -a "$SOURCE/desk_config.json" "$APP/desk_config.json"; fi

if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes -subj "/CN=$DOMAIN" -keyout "$KEY" -out "$CERT"
  chmod 600 "$KEY"
fi
if [[ ! -f "$TOKEN_FILE" || "$ROTATE_TOKEN" -eq 1 ]]; then
  python3 "$APP/tools/tickets/ticket.py" account rotate-service-token --token-file "$TOKEN_FILE"
fi
if [[ -n "$DATA_ROOT" && -d "$DATA_ROOT/items" ]]; then
  python3 "$APP/tools/tickets/ticket.py" migrate --from "$DATA_ROOT" --to "$DB"
fi
ACCOUNT_ARGS=(account init --db "$DB" --username "$ADMIN_USER")
if [[ "$REOPEN_SETUP" -eq 1 ]]; then ACCOUNT_ARGS+=(--reopen-setup); fi
python3 "$APP/tools/tickets/ticket.py" "${ACCOUNT_ARGS[@]}"

cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Ticket Desk ($SERVICE_NAME)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP
ExecStart=/usr/bin/python3 $APP/tools/tickets/ticket.py serve --host 0.0.0.0 --port $PORT --db $DB --tls-cert $CERT --tls-key $KEY --token-file $TOKEN_FILE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
ufw allow "$PORT/tcp"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
systemctl restart "$SERVICE_NAME.service"
echo "安装完成。首次设密页开放 30 分钟：https://$DOMAIN:$PORT/setup"
