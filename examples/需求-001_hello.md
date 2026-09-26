# 需求-001 · hello_mdwf:一条命令打出位表(演示样例)

> 30 分钟 demo 的样例需求件。第 4 步原样拷进 `_office/后端/需求件/`,由总编落笔。需求件是一份 md,不是工单。

## 背景

新人想一眼看到工单台里有哪些位,不想去翻 `core/desk_config.json`。

## 要什么效果

代码仓加一个 `core/hello_mdwf.py`:跑 `python core/hello_mdwf.py`,一行打出 `core/desk_config.json`「位表」里全部位名,用「 / 」隔开;文件自带一条测试。

## 判据

1. origin/main 上有 `core/hello_mdwf.py`,不超过 12 行。
2. `python core/hello_mdwf.py` 打出的位名与 `core/desk_config.json`「位表」各行的「名字」逐个一致、顺序一致。
3. 在代码仓根跑 `python -m pytest core/hello_mdwf.py -q -p no:cacheprovider`,结果是 `1 passed`。
4. 与并线前的 origin/main 相比,只多了 `core/hello_mdwf.py` 这一个文件。

## 不碰什么

除 `core/hello_mdwf.py` 以外的一切文件。

## 拆窗

一张单、一个 0 号窗,不拆。
