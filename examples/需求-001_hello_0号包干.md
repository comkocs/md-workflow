**本单遵守 `@DEMO@/code/desk/模板/0号任务书模板.md`,偏离项见 §0.9。**

# 需求-001 hello_mdwf · 0 号包干任务书(演示样例)

> 30 分钟 demo 的样例任务书。第 5 步把文中 `@DEMO@` 换成 demo 目录,落进 `_office/后端/任务书/`,由后端总监落笔。

## §0 开窗第一段

照模板 §1.1 按本窗平台抄一版。demo 里 0 号的命令由读者替跑,本段从略。

## §0.9 偏离项

- 演示单:只走「施工 → 自测 → 并线 → 顶级审计 → settle」,不出进度件与收口报告。

## 需求原文(逐字抄需求件)

代码仓加一个 `core/hello_mdwf.py`:跑 `python core/hello_mdwf.py`,一行打出 `core/desk_config.json`「位表」里全部位名,用「 / 」隔开;文件自带一条测试。

1. origin/main 上有 `core/hello_mdwf.py`,不超过 12 行。
2. `python core/hello_mdwf.py` 打出的位名与 `core/desk_config.json`「位表」各行的「名字」逐个一致、顺序一致。
3. 在代码仓根跑 `python -m pytest core/hello_mdwf.py -q -p no:cacheprovider`,结果是 `1 passed`。
4. 与并线前的 origin/main 相比,只多了 `core/hello_mdwf.py` 这一个文件。

## 本单不碰

除 `core/hello_mdwf.py` 以外的一切文件。

## 产出路径

- `core/hello_mdwf.py`(新增)

别单在跑的目录:无。

## 真源指针

- 需求件:`@DEMO@/_office/后端/需求件/需求-001_hello.md`
- 位表:`@DEMO@/code/core/desk_config.json` 的「位表」

## 怎么验收

- 上面四条判据逐条可核。
- 并线前在 `core/` 下跑一次全回归,不新增红。
- 本单置「免判卷模块」:0 号自并,并线后派一道顶级审计(`@DEMO@/code/desk/角色/顶级审计子代理提示词.md`),收口用 `settle`,不走 `judge`。
