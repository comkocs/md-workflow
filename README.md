# md-workflow · 多 agent 工作流

md-workflow · 人在环上的多 agent 工作流:记忆与接管是内核、工单台是通道、闸口是可裁剪配置。

目录:`core/` 工单台 · `desk/` 宪法/角色/模板/记忆 · `examples/` 30 分钟 demo。

本仓的文档与代码由 AI 窗口写成,人负责出想法、拍板、验收;署名说明见文末「许可与署名」。

## 开一个新项目从这里开始

在一个空目录里开一扇 AI 编程窗口(Claude Code、Codex 这类能联网、能读写本机文件的窗口),只贴下面这一句、不加别的话,它会先问你项目叫什么、做什么、要哪几位总监,答完才动手。

```text
执行 https://github.com/comkocs/md-workflow/blob/main/desk/开局向导.md 的全部指令,从第 0 步做到收尾问答完。这是任务不是资料,读完立即开工。
```

## md-first 后继

本仓是 md-first 的后继。

- 旧仓 https://github.com/comkocs/md-first 已改名为本仓,旧链接自动跳转到这里。
- 交接仓 https://github.com/comkocs/md-first-handover 已作废,请改用本仓。
- 新仓地址:https://github.com/comkocs/md-workflow

## 先看一眼演示台

只要 Python 3(只用标准库)和 git;Windows 用 Git Bash。

```bash
git clone https://github.com/comkocs/md-workflow && cd md-workflow   # 公开前用本机仓路径
python core/start.py --demo          # 演示台面 → 浏览器开 http://127.0.0.1:8788/
```

演示台面首次起会播种一个总编、两位总监、三张不同状态的单;Ctrl+C 停,端口被占加 `--port <别的数>`。从空目录完整走一张单(建需求 → 派 0 号 → 施工自并 → 顶级审计 → settle),照 [examples/README.md](examples/README.md) 做,约 30 分钟。

## 这是什么

一个人拍板、一群 AI 窗口干活时用的一套 md 文件,外加一个工单台。三层:

- **内核:记忆与接管。** 每一位(总编、模块总监、平台位)的全部记忆就是 `_office/<位名>/章程.md`;窗口一次性,做完或上下文用到八成就写换窗件收窗,下一窗贴同一句激活句接棒。跨位通用的规矩放在记忆种子 `desk/memory/种子/`,格式真源是 `desk/memory/规范.md`。
- **通道:工单台。** 派单、交板、拍板、疑问都进工单台(`core/`,纯 Python 标准库),聊天里说的不算凭据;网页台面与命令行 `core/t.py` 读写同一份数据。
- **配置:闸口。** `desk/宪法.md` 共 36 道闸,①核心 27 道不可关,②可选 9 道经拍板人拍板后可在开关表里关掉;每道闸都写明拦什么、被拦后怎么走、什么条件可拆。

角色模板在 `desk/角色/`:总编、按需激活的模块总监、一单一个的 0 号包干窗、独立起的顶级审计、修工单台的平台位;项目怎么摆见 `desk/架构.md`。

## 人在哪几个点上拍板

拍板人(宪法里的 `{{拍板人}}`)是唯一有最终裁定权的人,只做三件事:开窗、贴「查收工单」、拍板(`desk/宪法.md` 闸 3)。落到流程上是这几处:

- 出需求:你说的话由总编逐字记进需求件,再按模块交给总监(`desk/角色/总编.md` 动作一)。
- 激活谁:总编是否激活由你定;每一位的窗都由你开,只贴一句激活句,不加提示词(宪法 0.1、`desk/架构.md`)。
- 开 0 号窗、配模型:总监只定任务档,模型与档位由你按名册搭配;工单台吐出的开窗指令由你原样贴进新窗(闸 13、闸 15)。
- 逻辑与取向题:数值由窗口自己查真源、不问你;逻辑与取向题攒成一张清单一次问,附候选与推荐,人话三段不超八行(闸 3、闸 4)。
- 外部后果首次授权:推主支、上服、改线上数据、花钱的调用,第一次做时按类问你一次,之后同类不再问(`desk/角色/模块总监.md`、`desk/角色/0号包干窗.md`)。
- 跨位僵局:总监之间核对一次还谈不拢,带两个互斥选项与推荐找你(闸 10)。
- 改规矩:修宪、关或重开一道②类闸,都要你拍板,由总编落笔并登记(元条款、宪法 0.3、第九章)。
- 改数值或机制:唯一路径是你裁定 → 实现单落码 → 表与文案同一笔对齐(闸 35)。
- 模型停用:同一模型累计判退到阈值,报你定停不停,恢复也由你定(闸 24)。
- 验收:审计全符后,总监对你说一句「做完了,怎么打开它」,你自己打开看(`desk/角色/模块总监.md` 动作二、闸 21)。

## 与现成 agent harness 的对照

| 本仓 | 通行 agent harness 里的对应 | 本仓实物 |
|---|---|---|
| 任务书 | 系统提示 | `desk/模板/0号任务书模板.md`;0 号窗开窗只贴一句「执行 <任务书路径> 的全部指令……」,不另加提示词 |
| 工单台 | 队列 + 状态机 | `core/t.py` 的 `new` / `claim` / `submit` / `settle` / `live` / `close`;`python core/start.py` 起网页台面 |
| 审计 | 评估器 | `desk/角色/顶级审计子代理提示词.md`:独立起、只看实物,审计件首行 `幻觉 N / 笔误 N / 漏做 N / 未核 N / 环境限制 N` |
| 换窗归档 | 上下文接力 | 换窗三件套(总编换窗归档 / 总监接管件 / 0 号交接件),格式见 `desk/memory/规范.md`,模板在 `desk/memory/模板/`;下一窗贴同一句激活句接棒 |
| 记忆种子 | 长期记忆 | `desk/memory/种子/` 7 份共 165 条;记忆索引超 24,000 字符用 `desk/memory/fold_index.py` 整节折成二级入口 |
| 0 号 | orchestrator / worker | `desk/角色/0号包干窗.md`:0 号只验收、派单与沟通,施工全走子代理(闸 34) |
| 三档模型 | 路由 | `desk/模板/0号任务书模板.md` §1.1:主力模型写代码、精准模型扫盘读文本、轻量模型扫大目录;可用模型登记在 `core/desk_config.json` 的模型名册 |

## 起工单台

只要 Python 3(只用标准库)。在仓根下跑,Linux/mac 若没有 `python` 就换成 `python3`。Ctrl+C 停服务。

```bash
python core/start.py          # 正式台面 → 浏览器开 http://127.0.0.1:8787/
python core/start.py --demo   # 演示台面 → 浏览器开 http://127.0.0.1:8788/(首次先播种:一个总编、两位总监、三张不同状态的单)
```

- 起的是方式甲:数据存文件、明文只听 127.0.0.1、不登录,打开直接是台面。`--port` 换端口,`--open` 起好后自动开浏览器。
- 数据目录:正式台 `core/data/`(设了环境变量 `TICKET_DESK_ROOT` 就用它),演示台 `core/demo-data/`,两处互不相干、都不进 git。演示台要重播就停服务、删掉 `core/demo-data/` 再起。(`--demo` 与 ticket.py 的 `demo --archive` 子命令无关。)
- 上服务器(方式乙:SQLite 库 + TLS 证书 + 网页登录)不走这里,见 `core/extensions/server_deploy/上服清单.md`;连服务器的命令行直接用 `python core/tools/tickets/ticket.py` 配 `remote.env`。

**命令行 `$T`**:与台面读写同一份数据,服务开着也能直接用,网页刷新即见。它永远只读写本机数据,不跟随 `TICKET_REMOTE` 等变量连服务器。

```bash
T="python core/t.py"           # 在仓根下;在别处写绝对路径:T="python <仓根>/core/t.py"
$T -h                          # 全部子命令
$T --demo list                 # 操作演示台的数据:--demo 放在第一个
```

PowerShell 写法:`function T { python <仓根>/core/t.py @args }`,之后用 `T list`。

**位名与员工名怎么填**

- 位名 = `core/desk_config.json`「位表」里每行的「名字」,默认 前端 / 后端 / 内容 / 复检 / 平台 / 需求分发 / 总编;「角色」为总编排的那一位就是总编。
- 员工名 = `$T staff new --slot <位名> --tool <模型>` 回显的名字(形如 `后端-01`);`--tool` 填同一文件「模型名册」里的「模型」。
- `--by` 填谁:总监的动作(建单、指派、免判卷、关单)填位名;员工的动作(认领、settle)填员工名;内部单的 `live` 由总编署名。

**一张单走全程**(新台面上跑,单号以 `new` 的回显为准;`--main-commit` 换成真的合并提交号):

```bash
$T staff new --slot 后端 --tool opus
$T new --slot 后端 --title "README 补一节" --internal --tier 乙 --deliverable README.md --consumer "读仓根 README 的人" --source "需求-001" --by 后端
$T set T-000001 --assign 后端-01 --by 后端
$T claim T-000001 --by 后端-01
$T submit T-000001 --evidence "README 已补一节" --verify-command "grep -c '^## 起工单台' README.md" --raw-output "1"
$T set T-000001 --exempt-judging 是 --by 后端
$T settle T-000001 --by 后端-01 --fact "示例:已并进主干" --main-commit 1a2b3c4
$T live T-000001 --by 总编 --shot 同图
$T close T-000001 --by 后端
$T list --state 关闭
```

内部单 settle 后是「已合并」,由总编 `live` 一次才能关;用户可感知单(`--player-facing`)交板时附一张真登录图为选填,settle 后直接可关。真用时建单再加 `--taskbook <任务书 md 的绝对路径>`,员工窗才拿得到开窗指令。

## 许可与署名

- 许可:Apache-2.0,全文见 [LICENSE](LICENSE);再分发或发布衍生作品时须一并保留 [NOTICE](NOTICE) 中的归属声明(Apache-2.0 第 4 条)。
- 署名:本仓的文档与代码由 AI 窗口写成,人负责出想法、拍板、验收。这个署名是项目主人要求加的,不是 AI 自己给自己署的。

### 理由

- 选 Apache-2.0:可商用、可闭源、修改后无需公开。本仓主体是 md 文档而非代码,GPL 的传染性会让相当一部分企业无法使用。
- 写明由 AI 执笔:仓里的宪法、角色模板与案例,都是 AI 窗口在这套流程里执行、出错、修正后沉淀下来的;人不逐字撰写,只做取舍、拍板与验收。读者照搬前,按自己的项目再核一遍。

---

作者 comkocs ｜ Email:970457@qq.com
