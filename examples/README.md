# 30 分钟 demo:从空目录走完一张 0 号单

八步:空项目 → 建容器 → 起工单台 → 总编窗建一张需求 → 总监窗派 0 号 → 0 号做一件 10 行代码的小事并自并 → 顶级审计 → settle。

真项目里第 4~8 步各由一个 AI 窗来做;demo 里第 7 步要你真开一个 AI 窗,其余各步由你替那个窗跑它会跑的那一条命令。

要准备:Windows + Git Bash、Python 3(带 pytest)、git;第 7 步要一个能读写本机文件的 AI 编程窗(如 Claude Code)。全程在同一个 Git Bash 窗里,从上往下一步贴一条。

本目录的文件:

| 文件 | 用在哪一步 |
|---|---|
| `README.md` | 本页 |
| `需求-001_hello.md` | 第 4 步:样例需求件 |
| `需求-001_hello_0号包干.md` | 第 5 步:样例 0 号任务书 |
| `hello_mdwf.py` | 第 6 步:0 号要交的 10 行代码 |

## 开头:设变量

```bash
REPO=https://github.com/comkocs/md-workflow   # 公开前用本机仓路径,如 REPO=D:/code/md-workflow
DEMO=~/mdwf-demo; T="python -X utf8 $DEMO/code/core/t.py"; unset TICKET_ENV TICKET_REMOTE TICKET_DESK_ROOT
```

- `DEMO` 是 demo 用到的唯一目录,要放别处只改这里(路径里不要有空格);这个目录必须还不存在。
- `T` 固定指向 `$DEMO/code` 那一份 `core/t.py`:工单数据跟着脚本所在的仓走,不跟当前目录走,所以全程只用这一份。
- `unset` 三个环境变量,防止命令被本机别的项目配置带去读写别处的库。
- 中途关了 Git Bash,重开后先把这两行再贴一遍。

## 第 1 步 · 空项目

建一个空目录当项目,放进代码仓,再建一个本地裸仓 `origin.git` 当远端,demo 里的推送只进它、不碰真仓。

```bash
mkdir "$DEMO" && git clone -q --bare "$REPO" "$DEMO/origin.git" && git clone -q "$DEMO/origin.git" "$DEMO/code" && ls "$DEMO"
```

看到:`code  origin.git`。

## 第 2 步 · 建容器

照 `desk/架构.md`「开局五步」把容器搭齐:`_office/` 是独立 git 仓放章程与需求件,`_work/` 放一单一棵的工作树,`.claude/settings.json` 管容器里所有 AI 窗的权限。

```bash
mkdir -p "$DEMO"/_office/{总编,后端/{需求件,任务书,回执,资料}} "$DEMO/_work" "$DEMO/.claude" && git -C "$DEMO/_office" init -q -b main && printf '{\n  "permissions": {\n    "allow": ["Bash(git *)", "Bash(python *)", "Bash(ls *)", "Bash(mkdir *)"],\n    "defaultMode": "bypassPermissions"\n  }\n}\n' > "$DEMO/.claude/settings.json" && ls -A "$DEMO"
```

看到:`.claude  _office  _work  code  origin.git`。真项目这一步还要按 `desk/占位符表.md` 填宪法、按 `desk/角色/亲测步骤_填章程.md` 填总编与总监章程,demo 跳过;「后端」这一位默认已在 `core/desk_config.json` 的位表里。

## 第 3 步 · 起工单台

先确认命令行读写的是 demo 里的库,再在后台起工单台(文件存储、只听本机、不登录),然后浏览器打开 http://127.0.0.1:18787/ 。

```bash
cd "$DEMO/code" && $T env && { python -X utf8 core/start.py --port 18787 > "$DEMO/_work/desk.log" 2>&1 & } && DESK_PID=$! && curl --noproxy '*' -s --retry 10 --retry-delay 1 --retry-connrefused -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:18787/
```

看到:`env` 那行的「本机库」落在 `mdwf-demo/code/core/data`,末行 `HTTP 200`。「本机库」不在 `$DEMO` 下就停手,别往下跑。

- 端口用 18787:本机常有真工单台占着 8787,撞上会被拦「本机端口 8787 上已经有服务在听」。
- 这里起的是正式台面(`core/data/`),后面写的单刷新网页即见;想看播种好的演示台面另用 `start.py --demo`,之前别先跑 `$T --demo …`,否则会建出空的 `core/demo-data/`,它就不再播种。

## 第 4 步 · 总编窗建一张需求

总编把你的话写成一份需求件(一份 md,不是工单),落进后端那一格并提交 `_office`,再在后端对话线说一句「需求-001 已落」。

```bash
cp "$DEMO/code/examples/需求-001_hello.md" "$DEMO/_office/后端/需求件/" && git -C "$DEMO/_office" add -- 后端/需求件/需求-001_hello.md && git -C "$DEMO/_office" -c user.name=总编 -c user.email=demo@mdwf.local commit -q -m "总编: 需求-001 落后端" && $T say --slot 后端 --by 总编 "需求-001 已落"
```

看到:`已写入 后端 对话线 · <时间>`。

## 第 5 步 · 总监窗派 0 号

后端总监落任务书、建员工位,建单时一次带齐 `--taskbook` 与 `--assign` 拿到开窗指令,再单独一条把单置成「免判卷模块」。

```bash
W=$(cd "$DEMO" && pwd -W) && sed "s#@DEMO@#$W#g" "$DEMO/code/examples/需求-001_hello_0号包干.md" > "$DEMO/_office/后端/任务书/需求-001_hello_0号包干.md" && git -C "$DEMO/_office" add -- 后端/任务书/需求-001_hello_0号包干.md && git -C "$DEMO/_office" -c user.name=后端 -c user.email=demo@mdwf.local commit -q -m "后端: 需求-001 任务书" && $T staff new --slot 后端 --tool opus && $T new --slot 后端 --by 后端 --tier 甲 --internal --window claude --title "加 core/hello_mdwf.py 打印位表" --taskbook "$W/_office/后端/任务书/需求-001_hello_0号包干.md" --deliverable core/hello_mdwf.py --consumer "想一眼看位表的新人:python core/hello_mdwf.py" --source "$W/_office/后端/需求件/需求-001_hello.md" --assign 后端-01 && $T set T-000001 --exempt-judging 是 --by 后端
```

看到:`后端-01`;一段「以下交付项现在还不存在」的提醒(正常,第 6 步才做出来);`T-000001` 与三行开窗指令(认领一句 / 「执行 … 的全部指令」一句 / 只给你的操作提示一句);末行 `已改 免判卷模块：是`。git 若提示 LF 与 CRLF 互换,可忽略。

- 开窗指令只在建单这一刻吐出,事后补 `--assign` 不会补吐,所以两项必须和 `new` 写在同一条。
- `--exempt-judging` 必须单独一条 `set`,和别的参数写在一起会被拦;不置的话第 8 步 `settle` 走不通。
- 真项目里你开一个新 AI 窗贴第二行开窗指令;第一行认领用的是 `ticket.py` 全路径、不带 `--local`,本机若配过远程通道会连去别处,所以 demo 一律用 `$T`。

## 第 6 步 · 0 号做一件 10 行代码的小事并自并

0 号认领,开一棵本单工作树,放进 10 行的 `core/hello_mdwf.py`,先跑它自带的测试和全回归(约 3 分钟),全绿再提交、推主支。

```bash
$T claim T-000001 --by 后端-01 && git -C "$DEMO/code" fetch -q origin && git -C "$DEMO/code" worktree add -q "$DEMO/_work/wt-后端-01-000001" -b feat/T-000001-hello origin/main && cd "$DEMO/_work/wt-后端-01-000001" && cp examples/hello_mdwf.py core/hello_mdwf.py && python -X utf8 core/hello_mdwf.py && python -m pytest core/hello_mdwf.py -q -p no:cacheprovider && (cd core && python -m pytest tools/tickets/tests extensions -q -p no:cacheprovider) && git add -- core/hello_mdwf.py && git -c user.name=后端-01 -c user.email=demo@mdwf.local commit -q -m "加 core/hello_mdwf.py:打印位表" && git push -q origin HEAD:main && git log --oneline -1
```

看到:一行位名 `前端 / 后端 / …`、`1 passed`、全回归末行 `… passed, 2 skipped …`,最后一行是刚推上主支的提交。

- 真项目里这 10 行由 0 号派出的写码子代理来写;demo 直接拷仓里备好的 `examples/hello_mdwf.py`。
- 全回归必须先 `cd core` 再跑,在仓根跑会报 `No module named 'tools'`。
- 新克隆的仓可能没有 git 身份,所以提交用 `-c user.name=… -c user.email=…` 就地给。

## 第 7 步 · 顶级审计

这一步是人工的:下面这条把 `desk/角色/顶级审计子代理提示词.md` 里的代码块取出、填好本单字段,存成一份可以直接贴的提示词。

```bash
W=$(cd "$DEMO" && pwd -W) && git -C "$DEMO/code" fetch -q origin && mkdir -p "$DEMO/_work/audit-T-000001" && sed -n '/^````text$/,/^````$/p' "$DEMO/code/desk/角色/顶级审计子代理提示词.md" | sed -e '/^````/d' -e 's#<位名>#后端#g' -e 's#<单号>#T-000001#g' -e "s#<需求件绝对路径>#$W/_office/后端/需求件/需求-001_hello.md#g" -e "s#<任务书绝对路径>#$W/_office/后端/任务书/需求-001_hello_0号包干.md#g" -e "s#<提交号>#$(git -C "$DEMO/code" rev-parse origin/main)#g" -e "s#<日期>#$(date +%F)#g" -e "s#{{代码仓路径}}#$W/code#g" -e 's#{{远端主支}}#origin/main#g' -e 's#{{代码托管远端}}#origin#g' -e "s#{{章程目录}}#$W/_office#g" -e "s#{{工作树根目录}}#$W/_work#g" > "$DEMO/_work/audit-T-000001/审计提示词.txt" && ! grep -n '{{\|<位名>\|<单号>\|<提交号>\|<日期>\|<需求件\|<任务书' "$DEMO/_work/audit-T-000001/审计提示词.txt" && echo "已生成 $W/_work/audit-T-000001/审计提示词.txt"
```

看到:`已生成 …/审计提示词.txt`(字段有没填上的会先打出那几行,且不打这一句)。然后:

1. 在 `$DEMO` 目录开一个新的 AI 编程窗,不带任何别的上下文,审计要「独立起」;用 Claude Code 的话就在本窗跑 `cd "$DEMO" && claude`,审计完退出,回到本窗接第 8 步。
2. 用编辑器打开这份 `审计提示词.txt`,全文复制,贴进新窗,别的什么都不说。
3. 它只读需求件、任务书与 `origin/main` 上的实物,自己跑命令取证,交出 `$DEMO/_office/后端/回执/T-000001_顶级审计_<日期>.md`,回报三行。
4. 算过:审计件第 1 行 `幻觉 N / 笔误 N / 漏做 N / 未核 N / 环境限制 N` 里前四项都是 0(环境限制不计),「四、不符项清单」写「无」。不是就按不符项开返工单,demo 不演返工。

## 第 8 步 · settle

审计全符后记账收口:0 号在本单工作树里交板并 `settle`,总监留一行「顶级审计全符」,总编 `live` 一次,总监关单,最后收掉工作树。

```bash
cd "$DEMO/_work/wt-后端-01-000001" && $T submit T-000001 --evidence "core/hello_mdwf.py 已并进 main" --verify-command "python core/hello_mdwf.py" --raw-output "$(python -X utf8 core/hello_mdwf.py)" && $T settle T-000001 --by 后端-01 --fact "feat/T-000001-hello 已并进 main" --main-commit "$(git rev-parse origin/main)" && $T say --slot 后端 --by 后端 --ref T-000001 "顶级审计全符 · 审计件在 _office/后端/回执/" && $T live T-000001 --by 总编 --shot 同图 && $T close T-000001 --by 后端 && cd "$DEMO" && git -C "$DEMO/code" worktree remove "$DEMO/_work/wt-后端-01-000001" && $T list --state 关闭
```

看到:单依次落「待判」「已合并」「实机复验过」「关闭」,`settle` 回显「名册已自动收窗：后端-01」,末行 `list` 里 T-000001 是「关闭」。刷新网页台面能看到同一张单。

- `submit` 必须在本单工作树里跑:交付项按当前目录找文件,主检出里没有这个新文件,会被拦「交付项找不到对应文件」。
- 免判卷单不走 `judge`:`settle` 一步落「已合并」;内部单还要总编 `live` 一次才能关,`--shot` 必填,内部单用「同图」(「免独图」只给已实机复验过、只欠一张独图的单用)。

## 收尾

看完网页台面,停掉第 3 步起的服务;demo 目录用完可整个删掉。

```bash
kill $DESK_PID
```

## 计时表

「机器实跑」是命令本身的耗时(在一台 Windows 机上全新目录实跑);「预计用时」含读说明、贴命令、看回显。

| 步 | 内容 | 预计用时 | 机器实跑 |
|---|---|---|---|
| 开头 | 读本页开头、设变量 | 2 分钟 | 0 秒 |
| 1 | 空项目:克隆代码仓与本地远端 | 1 分钟 | 1 秒 |
| 2 | 建容器 | 1 分钟 | 1 秒 |
| 3 | 起工单台、开网页 | 2 分钟 | 4 秒 |
| 4 | 总编建需求 | 2 分钟 | 1 秒 |
| 5 | 总监派 0 号 | 3 分钟 | 1 秒 |
| 6 | 0 号施工、全回归、自并 | 5 分钟 | 3 分 4 秒(全回归 3 分 3 秒) |
| 7 | 顶级审计:生成提示词、开新窗、等审计件 | 10 分钟 | 1 秒(不含 AI 窗审计) |
| 8 | settle 收口、看网页 | 2 分钟 | 1 秒 |
| | **合计** | **28 分钟** | |
