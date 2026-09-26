#!/usr/bin/env bash
set -euo pipefail
# 工单台上服(更新)脚本。三道前置闸 + 一道后置闸,任一不过就退出非零、一个字节都不动线上。
# 这几道闸是两次真事故换来的:
#   · 图片压缩挪到服务端后上服,线上 python 没装 Pillow,带图的 live 全被拦——
#     当时的上服探针只 grep 源码特征字符串,没有一条探服务端的运行环境。
#   · 上服链里 pytest 的退出码两次被 `| tail -1` 吞掉(一次被 echo 抢了 PIPESTATUS),带红上服两回。
# 三闸都能用 --check-only 单独跑(只跑闸不替换),不用拿线上当试验场。
#
# 名字全是变量(环境变量或参数),默认值是通用名:
#   INSTALL_ROOT  安装根,默认 /srv;安装目录必须是它下面的独立子目录
#   INSTALL_DIR   安装目录,默认 $INSTALL_ROOT/ticket-desk(--install-dir 可改)
#   SERVICE_NAME  systemd 服务名(不带 .service),默认 ticket-desk
#   SERVICE_USER  跑服务的系统账号,默认 ticket-desk
#   DESK_PORT     服务监听端口,默认 8443(后置闸与上服记录都用它)

SOURCE=""
INSTALL_ROOT="${INSTALL_ROOT:-/srv}"
INSTALL_DIR="${INSTALL_DIR:-$INSTALL_ROOT/ticket-desk}"
SERVICE_NAME="${SERVICE_NAME:-ticket-desk}"
SERVICE_USER="${SERVICE_USER:-ticket-desk}"
DESK_PORT="${DESK_PORT:-8443}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NODE_BIN="${NODE_BIN:-node}"
# 闸②的跳过上限:跳过条数超过它就当作没跑。
MAX_SKIPPED="${MAX_SKIPPED:-15}"
CHECK_ONLY=0
SKIP_TESTS=0
SKIP_NODE_CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --skip-node-check) SKIP_NODE_CHECK=1; shift ;;
    *) echo "拦下:不认识的更新参数：$1" >&2; exit 2 ;;
  esac
done
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("拦下:工单台要求 Python 3.10 或更高版本。")
PY
[[ -n "$SOURCE" && -d "$SOURCE/tools/tickets" && -d "$SOURCE/tools/browser" ]] || { echo "拦下:必须用 --source 指向工单台源码根目录。" >&2; exit 2; }
[[ "$INSTALL_DIR" == "$INSTALL_ROOT"/* && "$INSTALL_DIR" != "$INSTALL_ROOT" ]] || { echo "拦下:安装目录必须是 $INSTALL_ROOT 下的独立目录。" >&2; exit 2; }

# ── 前置闸 ① 服务端运行环境 ────────────────────────────────────────
# 探的是「服务器上真跑得起来吗」,不是「源码里有没有那几个字」。
if ! "$PYTHON_BIN" -c "from PIL import Image, ImageOps" >/dev/null 2>&1; then
  echo "前置闸 ① PIL … 拦" >&2
  echo "  $PYTHON_BIN 里 import 不到 Pillow。系统 python 受 PEP 668 管控,用 apt install python3-pil,不要 pip。" >&2
  exit 2
fi
PIL_NOTE="$PYTHON_BIN 可 import"
if command -v sudo >/dev/null 2>&1 && id "$SERVICE_USER" >/dev/null 2>&1; then
  # 真正跑服务的是服务账号,不是现在这个 shell 的用户;只探当前用户会漏。
  if ! sudo -n -u "$SERVICE_USER" "$PYTHON_BIN" -c "from PIL import Image, ImageOps" >/dev/null 2>&1; then
    echo "前置闸 ① PIL … 拦" >&2
    echo "  当前用户能 import,但服务账号 $SERVICE_USER 不能——线上跑服务的是它。" >&2
    exit 2
  fi
  PIL_NOTE="$PIL_NOTE;服务账号 $SERVICE_USER 也可 import"
else
  PIL_NOTE="$PIL_NOTE;本机没有 sudo 或没有 $SERVICE_USER 账号,服务账号那一次没跑"
fi
echo "前置闸 ① PIL … 过（$PIL_NOTE）"

# ── 前置闸 ② 源码自检 ──────────────────────────────────────────────
# ★退出码必须是 pytest 自己的:输出重定向进文件,`$?` 直接接在那条命令后面,全程不接管道。
#   `| tail -1` 之所以能把红吞成绿,是因为管道的退出码默认只取最后一节(tail 永远是 0),
#   pytest 的非零码在那一步就丢了。开头的 `set -euo pipefail` 已经把 pipefail 打开了,
#   但那只是第二重保险——第一重是这里根本不建管道,连让人再写错一次的机会都不留。
# 包里带着扩展目录时,扩展自己的用例一起跑(它们在用例里显式打开扩展)。
TEST_PATHS=(tools/tickets/tests)
if [[ -d "$SOURCE/extensions" ]]; then TEST_PATHS+=(extensions); fi
if [[ "$SKIP_TESTS" -eq 1 ]]; then
  echo "前置闸 ② pytest … 跳过（--skip-tests;这是人明写的,不是静默跳过）"
elif ! "$PYTHON_BIN" -c "import pytest" >/dev/null 2>&1; then
  echo "前置闸 ② pytest … 拦" >&2
  echo "  服务器没装 pytest,--skip-tests 才能跳过。" >&2
  exit 2
else
  TEST_LOG="$(mktemp)"
  # `-p no:cacheprovider`:缓存目录一律不写进包目录。用 sudo 跑过一次之后,包里会留下 root 属主的
  #   .pytest_cache,下一个普通账号写不进去,pytest 只打一条 PytestCacheWarning 就过去了——
  #   闸看着还在跑,其实已经被自己上一趟的产物半瞎了。
  # `-rs`:把跳过的清单打出来,下面的跳过上限要拿它当证据。
  set +e
  ( cd "$SOURCE" && "$PYTHON_BIN" -m pytest "${TEST_PATHS[@]}" -q -rs -p no:cacheprovider ) >"$TEST_LOG" 2>&1
  TEST_RC=$?
  set -e
  if [[ "$TEST_RC" -ne 0 ]]; then
    echo "前置闸 ② pytest … 拦（pytest 退出码 $TEST_RC）" >&2
    tail -n 30 "$TEST_LOG" >&2
    rm -f "$TEST_LOG"
    exit 2
  fi
  # 跳过也要有上限。有几条用例只能在完整仓树上跑(要读 tools/ 以外的文件),上服包里干净跳过;
  # 但「跳过」本身就是下一个静默漏洞的形状——「闸看起来在,其实从没真跑过」。
  # 跳过条数一旦超过上限,这一趟就按没跑算,照拦处理,并把跳过清单打出来给人看。
  TEST_SUMMARY="$(tail -n 1 "$TEST_LOG")"
  TEST_SKIPPED="$(printf '%s' "$TEST_SUMMARY" | grep -oE '[0-9]+ skipped' | grep -oE '^[0-9]+' || true)"
  TEST_SKIPPED="${TEST_SKIPPED:-0}"
  if [[ "$TEST_SKIPPED" -gt "$MAX_SKIPPED" ]]; then
    echo "前置闸 ② pytest … 拦（跳过 $TEST_SKIPPED 条,超过上限 $MAX_SKIPPED;跳这么多等于没跑）" >&2
    echo "  跳过清单:" >&2
    grep -E '^SKIPPED' "$TEST_LOG" >&2 || true
    rm -f "$TEST_LOG"
    exit 2
  fi
  echo "前置闸 ② pytest … 过（$TEST_SUMMARY;跳过 $TEST_SKIPPED 条,上限 $MAX_SKIPPED）"
  rm -f "$TEST_LOG"
fi

# ── 前置闸 ③ 前端语法 ──────────────────────────────────────────────
# tickets.js 是浏览器直接读的,坏了没人替它编译,只有点开页面的人才发现。
if [[ "$SKIP_NODE_CHECK" -eq 1 ]]; then
  echo "前置闸 ③ node … 跳过（--skip-node-check;这是人明写的,不是静默跳过）"
elif ! command -v "$NODE_BIN" >/dev/null 2>&1; then
  echo "前置闸 ③ node … 拦" >&2
  echo "  服务器没装 node,--skip-node-check 才能跳过。" >&2
  exit 2
elif ! "$NODE_BIN" --check "$SOURCE/tools/browser/tickets.js"; then
  echo "前置闸 ③ node … 拦（tickets.js 语法不过）" >&2
  exit 2
else
  echo "前置闸 ③ node … 过"
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "三闸全过；--check-only:到此为止,不替换、不重启。"
  exit 0
fi
echo "三闸全过,开始替换。"

APP="$INSTALL_DIR/app"
rm -rf "$APP/tools/tickets" "$APP/tools/browser" "$APP/extensions"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP/tools"
cp -a "$SOURCE/tools/tickets" "$APP/tools/tickets"
cp -a "$SOURCE/tools/browser" "$APP/tools/browser"
if [[ -d "$SOURCE/extensions" ]]; then cp -a "$SOURCE/extensions" "$APP/extensions"; fi
if [[ -f "$SOURCE/desk_config.json" ]]; then cp -a "$SOURCE/desk_config.json" "$APP/desk_config.json"; fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP"
systemctl restart "$SERVICE_NAME.service"

# ── 后置闸:服务端口真在听 ────────────────────────────────────────
# 日志里的 READY 行是纸糊的闸:journal 是追加的,上一次的旧 READY 照样在那儿被 grep 到。
# 真判据只有一个——端口现在在听。
LISTENING=0
for _ in $(seq 1 10); do
  if ss -ltn 2>/dev/null | grep -q ":$DESK_PORT "; then LISTENING=1; break; fi
  sleep 1
done
if [[ "$LISTENING" -ne 1 ]]; then
  echo "后置检查:重启后等了 10 秒,$DESK_PORT 仍然没在听。线上代码已替换,请照下面的日志排查。" >&2
  journalctl -u "$SERVICE_NAME.service" -n 20 --no-pager >&2 || true
  exit 3
fi
echo "后置检查:$DESK_PORT 已在听。"
echo "工单台代码已更新；数据库、图片和证书未改动。"

# ── 上服记录:后置闸通过之后自动建一张,免员工窗、免判 ────────────────
# 服务端要在配置里启用扩展 deploy_record,这一段才建得成;没启用时只打一行提示,不影响上服。
# ★必须在**后置闸之后**:这张单的意思是「线上真跑起来了」,不是「脚本走到这一行了」。
#   放在闸前面就成了纸糊的记录——前面那个 LISTENING 检查失败会 exit 3,走不到这里。
# ★建单失败**不能**让整条上服失败:代码已经替换、服务已经起来,这时候 exit 非零
#   会让人以为上服没成、回头再跑一遍。所以这一段全程 || true,失败只打印一行。
# 部署头三条来源,按可靠性排:
#   ① 环境变量(人明写的最优先);
#   ② 包里的 DEPLOY_HEAD 文件 —— pack.sh 在打包那一刻写进去的,**这是常态路径**;
#   ③ $SOURCE 下的 git(只有拿仓树当 --source 时才有)。
# ★只有 ③ 时永远拿不到头——`git archive` 打的包没有 .git,于是这段自动建单每次都走兜底。
if [[ -z "${DEPLOY_HEAD:-}" ]]; then
  for stamp in "$SOURCE/extensions/server_deploy/DEPLOY_HEAD" "$SOURCE/DEPLOY_HEAD"; do
    if [[ -f "$stamp" ]]; then
      DEPLOY_HEAD="$(tr -d '[:space:]' < "$stamp")"
      break
    fi
  done
fi
DEPLOY_HEAD="${DEPLOY_HEAD:-$(cd "$SOURCE" 2>/dev/null && git rev-parse --short=9 HEAD 2>/dev/null || true)}"
if [[ -z "$DEPLOY_HEAD" ]]; then
  echo "上服记录:拿不到部署头(包里既没有 DEPLOY_HEAD 文件也没有 .git),跳过自动建单。" \
       "★正常路径是用 extensions/server_deploy/pack.sh 打包,它会把提交号写进包里;" \
       "这一次请手工补:ticket.py deploy-record --head <提交号> --probes \"<探针输出>\""
else
  PROBES="服务 active;$DESK_PORT 在听(后置闸实测);源 $SOURCE"
  RECORD_JSON=$(printf '{"op":"deploy-record","by":"部署脚本","head":"%s","repo":"server","probes":"%s"}' \
                "$DEPLOY_HEAD" "$PROBES")
  # 令牌自己找,不靠调用者记路径。
  # ★★路径**从 systemd unit 里的 --token-file 现读**,不写死。两个理由:
  #   ① 服务用哪个令牌,这里就用哪个——配置改了脚本自动跟上,不会哪天悄悄读到一份废令牌;
  #   ② 本脚本**不许出现指向数据库或图片目录的字面路径**:上服只替换 app,
  #      绝不碰数据库/图片/证书,扩展自带的用例有一条闸按文本守着这件事
  #      (它连注释里的字面也拦——粗,但方向是对的)。
  #      为了读个令牌把那道闸放宽是本末倒置:它防的是「上服脚本误删数据库」。
  if [[ -z "${TICKET_TOKEN:-}" ]]; then
    token_file="$(systemctl cat "$SERVICE_NAME.service" 2>/dev/null \
      | grep -oE -- '--token-file[= ][^[:space:]]+' | head -1 | sed 's/^--token-file[= ]//')"
    if [[ -n "$token_file" && -r "$token_file" ]]; then
      TICKET_TOKEN="$(tr -d '[:space:]' < "$token_file")"
    fi
  fi
  if [[ -n "${TICKET_TOKEN:-}" ]]; then
    if curl -sk --max-time 20 -X POST "https://127.0.0.1:$DESK_PORT/api/action" \
         -H "Content-Type: application/json" -H "X-Ticket-Token: ${TICKET_TOKEN}" \
         -d "$RECORD_JSON" -o /tmp/desk-deploy-record.json 2>/dev/null; then
      echo "上服记录已建(头 $DEPLOY_HEAD);部署头已写进当前值面。取证请另开取证单——取不到图不挡上服。"
    else
      echo "上服记录没建成(不影响本次上服,线上已是 $DEPLOY_HEAD)。" \
           "请补:ticket.py deploy-record --head $DEPLOY_HEAD --probes \"$PROBES\"" >&2
    fi
  else
    echo "上服记录:没有 TICKET_TOKEN,跳过自动建单(不影响本次上服)。" \
         "请补:ticket.py deploy-record --head $DEPLOY_HEAD --probes \"$PROBES\""
  fi
fi
