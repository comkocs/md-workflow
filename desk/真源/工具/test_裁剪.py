"""自带合成夹具；不读取随包的真实条目表。"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

import 裁剪


def 测试条目() -> list[dict]:
    """每个模块一条合成说明，仅用于验证工具行为。"""
    条目 = []
    中型模块 = {"分工与权责", "任务书与派遣", "跨窗协作", "负载与性能"}
    大型模块 = {"审查席", "真源库"}
    特征要求 = {
        "审计与验收": ["代码"], "代码与仓库": ["代码"],
        "发布与上线": ["代码", "上线"], "素材与命名": ["素材"],
        "负载与性能": ["代码"], "审查席": ["审查岗"],
        "真源库": ["数值", "多模型", "规矩库"],
    }
    for 次, 模块 in enumerate(裁剪.模块表, 1):
        最小 = "大" if 模块 in 大型模块 else "中" if 模块 in 中型模块 else "小"
        特征 = 特征要求.get(模块, [])
        条目.append({
            "编号": f"G-{次:04d}", "模块": 模块,
            "层": 裁剪.层表[(次 - 1) % len(裁剪.层表)],
            "短题": f"合成说明{次}",
            "条文": "{{决策者}}为{{项目名}}核对本项合成说明。",
            "为什么": "这是测试夹具，验证文字与解释一同输出。",
            "最小规模": 最小, "最大规模": "大", "需要特征": 特征,
            "依赖": [] if 次 == 1 else ["G-0001"],
            "加回条件": "同时在跑的窗口达到 4 个，或相应工作开始 1 次时重新核对。" if 最小 != "小" or 特征 else "",
            "源条数": 1,
        })
    条目[7]["条文"] = "本项合成说明的当前说法记录在{{口径件}}。"
    return 条目


def 测试卡(规模="小", 特征=()) -> dict:
    return {
        "项目": {"名": "合成项目", "一句": "检查合成资料的裁剪。", "受众": "读者", "成品": "测试稿"},
        "称呼": dict(裁剪.占位缺省, 项目名="合成项目", 受众="读者", 成品="测试稿"),
        "规模": 规模, "数字": {"同时窗数": 1, "分工位数": 1, "仓数": 0},
        "特征": {名: 名 in 特征 for 名 in 裁剪.特征表},
        "手调": {"加": [], "减": []}, "sha256": "a" * 64,
    }


class 裁剪测试(unittest.TestCase):
    def setUp(self):
        self.暂存 = tempfile.TemporaryDirectory(prefix="裁剪测试_夹具_")
        self.addCleanup(self.暂存.cleanup)
        self.目录 = Path(self.暂存.name)
        self.条目 = 测试条目()
        self.卡 = 测试卡()

    def 写表(self, 条目=None):
        路径 = self.目录 / "合成条目.jsonl"
        路径.write_text("".join(json.dumps(条, ensure_ascii=False) + "\n" for 条 in (self.条目 if 条目 is None else 条目)), encoding="utf-8")
        return 路径

    def 写卡(self, 档="", 窗=1, 位=1, 仓=0, 上线=False, 追加=""):
        路径 = self.目录 / "需求卡.toml"
        路径.write_text(
            '["项目"]\n"名" = "合成项目"\n"一句" = "检查合成资料。"\n"受众" = "读者"\n"成品" = "测试稿"\n'
            '["规模"]\n"档" = "' + 档 + f'"\n"同时窗数" = {窗}\n"分工位数" = {位}\n"仓数" = {仓}\n'
            '["特征"]\n"上线" = ' + str(上线).lower() + '\n' + 追加,
            encoding="utf-8",
        )
        return 路径

    def 调命令(self, 参数):
        输出, 错误 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(输出), contextlib.redirect_stderr(错误):
            返回码 = 裁剪.主程(参数)
        return 返回码, 输出.getvalue(), 错误.getvalue()

    def test_夹具本身校验通过(self):
        self.assertEqual(裁剪.校验条目(self.条目), [])

    def test_规模下限含边界(self):
        条 = copy.deepcopy(self.条目[0])
        条.update(最小规模="中", 加回条件="窗口达到 4 个时加回。")
        self.assertEqual(len(裁剪.裁选([条], 测试卡("小"))[0]), 0)
        self.assertEqual(len(裁剪.裁选([条], 测试卡("中"))[0]), 1)
        self.assertEqual(len(裁剪.裁选([条], 测试卡("大"))[0]), 1)

    def test_规模上限含边界(self):
        条 = copy.deepcopy(self.条目[0])
        条["最大规模"] = "中"
        self.assertEqual(len(裁剪.裁选([条], 测试卡("小"))[0]), 1)
        self.assertEqual(len(裁剪.裁选([条], 测试卡("中"))[0]), 1)
        self.assertEqual(len(裁剪.裁选([条], 测试卡("大"))[0]), 0)

    def test_特征必须全部打开(self):
        条 = copy.deepcopy(self.条目[0])
        条["需要特征"] = ["代码", "上线"]
        for 特征, 应有 in (((), 0), (("代码",), 0), (("代码", "上线"), 1)):
            with self.subTest(特征=特征):
                self.assertEqual(len(裁剪.裁选([条], 测试卡(特征=特征))[0]), 应有)

    def test_两新特征缺省拆开启留(self):
        for 模块, 特征 in (("审查席", "审查岗"), ("真源库", "规矩库")):
            with self.subTest(模块=模块, 特征=特征):
                已开 = tuple(名 for 名 in 裁剪.特征表 if 名 not in ("审查岗", "规矩库"))
                卡 = 测试卡("大", 已开)
                保留, 拆掉, _ = 裁剪.裁选(self.条目, 卡)
                self.assertNotIn(模块, [条["模块"] for 条 in 保留])
                self.assertIn(模块, [条["模块"] for 条 in 拆掉])
                卡["特征"][特征] = True
                保留, 拆掉, _ = 裁剪.裁选(self.条目, 卡)
                self.assertIn(模块, [条["模块"] for 条 in 保留])
                self.assertNotIn(模块, [条["模块"] for 条 in 拆掉])
                另一模块 = "真源库" if 模块 == "审查席" else "审查席"
                self.assertNotIn(另一模块, [条["模块"] for 条 in 保留])

    def test_预设缺两新键按false(self):
        for 路径 in sorted((裁剪.包目录 / "档案").glob("*.toml")):
            with self.subTest(预设=路径.name):
                文本 = 路径.read_text(encoding="utf-8")
                文本 = "\n".join(行 for 行 in 文本.splitlines()
                               if not 行.startswith(('"审查岗" =', '"规矩库" ='))) + "\n"
                缺键卡 = self.目录 / 路径.name
                缺键卡.write_text(文本, encoding="utf-8")
                卡 = 裁剪.读卡(缺键卡)
                self.assertIs(卡["特征"]["审查岗"], False)
                self.assertIs(卡["特征"]["规矩库"], False)
                self.assertEqual(len(卡["特征"]), 7)
                各件 = 裁剪.生成各件(self.条目, 卡, "b" * 64)
                记录 = json.loads(各件["裁剪记录.json"])
                self.assertFalse({"审查席", "真源库"}.intersection(记录["保留模块"]))

    def test_五份预设两新特征仅大型开启(self):
        for 路径 in sorted((裁剪.包目录 / "档案").glob("*.toml")):
            with self.subTest(预设=路径.name):
                卡 = 裁剪.读卡(路径)
                for 特征 in ("审查岗", "规矩库"):
                    self.assertIs(卡["特征"][特征], 路径.stem == "大型研发")

    def test_手调加可越过规模特征(self):
        self.卡["手调"]["加"] = [{"编号": "G-0016", "理由": "需要单独试用。"}]
        保留, _, _ = 裁剪.裁选(self.条目, self.卡)
        self.assertIn("G-0016", [条["编号"] for 条 in 保留])

    def test_手调减保留理由(self):
        self.卡["手调"]["减"] = [{"编号": "G-0002", "理由": "这次不需要。"}]
        保留, _, 因 = 裁剪.裁选(self.条目, self.卡)
        self.assertNotIn("G-0002", [条["编号"] for 条 in 保留])
        self.assertIn("这次不需要。", 因["G-0002"])

    def test_同号先加后减(self):
        项 = {"编号": "G-0002", "理由": "检查顺序。"}
        self.卡["手调"] = {"加": [项], "减": [项]}
        self.assertNotIn("G-0002", [条["编号"] for 条 in 裁剪.裁选(self.条目, self.卡)[0]])

    def test_手调缺理由报错(self):
        for 动作 in ("加", "减"):
            for 理由 in (None, "", "   "):
                with self.subTest(动作=动作, 理由=理由):
                    项 = {"编号": "G-0002"}
                    if 理由 is not None:
                        项["理由"] = 理由
                    追加 = '["手调"]\n' + json.dumps(动作, ensure_ascii=False) + " = " + self.转内联表(项) + "\n"
                    with self.assertRaisesRegex(裁剪.输入错, "理由"):
                        裁剪.读卡(self.写卡(追加=追加))

    @staticmethod
    def 转内联表(项):
        return "[{" + ", ".join(f"{json.dumps(键, ensure_ascii=False)} = {json.dumps(值, ensure_ascii=False)}" for 键, 值 in 项.items()) + "}]"

    def test_手调不存在编号报错(self):
        self.卡["手调"]["加"] = [{"编号": "G-9999", "理由": "测试缺编号。"}]
        with self.assertRaisesRegex(裁剪.输入错, "编号不存在"):
            裁剪.裁选(self.条目, self.卡)

    def test_依赖被拆报错不自动补(self):
        self.卡["手调"]["减"] = [{"编号": "G-0001", "理由": "检查依赖。"}]
        with self.assertRaisesRegex(裁剪.输入错, "依赖被拆.*G-0001"):
            裁剪.裁选(self.条目, self.卡)
        self.assertEqual(self.卡["手调"]["加"], [])

    def test_依赖报错时不写输出(self):
        表 = self.写表()
        卡 = self.写卡(追加='["手调"]\n"减" = [{"编号" = "G-0001", "理由" = "检查报错。"}]\n')
        出 = self.目录 / "输出"
        码, _, 错 = self.调命令(["出", "--卡", str(卡), "--条目", str(表), "--出", str(出)])
        self.assertEqual(码, 1)
        self.assertIn("依赖被拆", 错)
        self.assertFalse(出.exists())

    def test_占位全部替换(self):
        文 = "、".join("{{" + 名 + "}}" for 名 in 裁剪.占位缺省)
        结果 = 裁剪.替换(文, self.卡["称呼"])
        self.assertNotIn("{{", 结果)
        self.assertIn("合成项目", 结果)
        self.assertIn("读者", 结果)

    def test_未知占位报错(self):
        with self.assertRaisesRegex(裁剪.输入错, "未知占位"):
            裁剪.替换("{{不在定表}}", self.卡["称呼"])
        self.条目[0]["条文"] += "{{不在定表}}"
        self.assertTrue(any("未知占位" in 错 for 错 in 裁剪.校验条目(self.条目)))

    def test_校验必填逐项(self):
        for 字段 in ("编号", "模块", "层", "短题", "条文", "为什么", "最小规模", "需要特征", "依赖", "源条数"):
            with self.subTest(字段=字段):
                条目 = copy.deepcopy(self.条目)
                del 条目[0][字段]
                self.assertTrue(any("缺必填 " + 字段 in 错 for 错 in 裁剪.校验条目(条目)))

    def test_校验枚举逐项(self):
        for 字段 in ("模块", "层", "最小规模", "最大规模", "需要特征"):
            with self.subTest(字段=字段):
                条目 = copy.deepcopy(self.条目)
                条目[0][字段] = ["未知"] if 字段 == "需要特征" else "未知"
                self.assertTrue(裁剪.校验条目(条目))

    def test_校验编号格式与唯一(self):
        for 编号 in ("G-1", "G-0000", "G-１２３４", "G-0001\n", "X-0001", 1):
            with self.subTest(编号=编号):
                条目 = copy.deepcopy(self.条目)
                条目[0]["编号"] = 编号
                self.assertTrue(any("编号须为" in 错 for 错 in 裁剪.校验条目(条目)))
        self.条目[1]["编号"] = "G-0001"
        self.assertTrue(any("编号重复" in 错 for 错 in 裁剪.校验条目(self.条目)))

    def test_校验依赖存在与自依赖(self):
        for 依赖, 提示 in ((["G-9999"], "依赖不存在"), (["G-0001"], "不得自依赖"), (["错误"], "依赖编号格式"), ("G-0002", "依赖须为")):
            with self.subTest(依赖=依赖):
                条目 = copy.deepcopy(self.条目)
                条目[0]["依赖"] = 依赖
                self.assertTrue(any(提示 in 错 for 错 in 裁剪.校验条目(条目)))

    def test_校验规模倒置与缺省上限(self):
        del self.条目[0]["最大规模"]
        self.assertEqual(裁剪.校验条目(self.条目), [])
        self.条目[0].update(最小规模="大", 最大规模="小", 加回条件="窗口达到 7 个时核对。")
        self.assertTrue(any("最小规模大于最大规模" in 错 for 错 in 裁剪.校验条目(self.条目)))

    def test_校验需要加回条件不空(self):
        for 修改 in ({"最小规模": "中"}, {"需要特征": ["素材"]}):
            with self.subTest(修改=修改):
                条目 = copy.deepcopy(self.条目)
                条目[0].update(修改)
                条目[0]["加回条件"] = "  "
                self.assertTrue(any("加回条件" in 错 for 错 in 裁剪.校验条目(条目)))

    def test_校验文字长度与数目(self):
        for 字段, 值 in (("短题", "题" * 21), ("为什么", "因" * 121), ("条文", ""), ("源条数", 0), ("源条数", True)):
            with self.subTest(字段=字段, 值=值):
                条目 = copy.deepcopy(self.条目)
                条目[0][字段] = 值
                self.assertTrue(裁剪.校验条目(条目))

    def test_校验错误类型不崩溃(self):
        self.assertTrue(裁剪.校验条目([None]))
        for 字段 in ("编号", "模块", "最小规模", "需要特征", "依赖", "加回条件"):
            with self.subTest(字段=字段):
                条目 = copy.deepcopy(self.条目)
                条目[0][字段] = {"错误": True}
                self.assertTrue(裁剪.校验条目(条目))

    def test_数字推算所有边界(self):
        for 窗, 位, 仓, 上线, 应有 in (
            (1, 0, 0, False, "小"), (2, 1, 1, True, "小"),
            (3, 1, 1, False, "小"), (3, 0, 0, False, "小"),
            (4, 1, 0, False, "中"), (6, 5, 1, False, "中"),
            (1, 2, 0, False, "中"), (3, 2, 0, False, "中"),
            (7, 1, 0, False, "大"), (4, 6, 0, False, "大"),
            (1, 6, 0, False, "大"), (1, 1, 2, True, "中"),
            (2, 4, 2, True, "大"), (8, 1, 0, False, "大"),
            (1, 0, 2, True, "中"), (3, 3, 2, True, "中"),
            (2, 4, 2, False, "中"), (2, 4, 1, True, "中"),
            (6, 5, 2, True, "大"), (1, 1, 2, False, "小"),
        ):
            with self.subTest(窗=窗, 位=位, 仓=仓, 上线=上线):
                self.assertEqual(裁剪.推算规模(窗, 位, 仓, 上线), 应有)
                self.assertEqual(裁剪.读卡(self.写卡(窗=窗, 位=位, 仓=仓, 上线=上线))["规模"], 应有)

    def test_档与数字不一致提醒并用档(self):
        错误 = io.StringIO()
        with contextlib.redirect_stderr(错误):
            卡 = 裁剪.读卡(self.写卡(档="小", 窗=7))
        self.assertEqual(卡["规模"], "小")
        self.assertEqual(len(错误.getvalue().splitlines()), 1)
        self.assertIn("提醒", 错误.getvalue())

    def test_称呼缺省与自填(self):
        卡 = 裁剪.读卡(self.写卡(追加='["称呼"]\n"决策者" = "主理人"\n"真源件" = ""\n'))
        self.assertEqual(卡["称呼"]["决策者"], "主理人")
        self.assertEqual(卡["称呼"]["真源件"], 裁剪.占位缺省["真源件"])
        self.assertEqual(卡["称呼"]["受众"], "读者")
        self.assertEqual(卡["称呼"]["项目名"], "合成项目")

    def test_需求卡非法数字特征称呼报错(self):
        for 追加 in ('"代码" = "是"\n', '"不存在" = true\n', '["称呼"]\n"未知" = "称呼"\n', '["称呼"]\n"真源件" = "{{真源件}}"\n'):
            with self.subTest(追加=追加):
                with self.assertRaises(裁剪.输入错):
                    裁剪.读卡(self.写卡(追加=追加))
        for 窗, 位, 仓 in ((0, 1, 0), (1, -1, 0), (1, 1, -1)):
            with self.assertRaises(裁剪.输入错):
                裁剪.读卡(self.写卡(窗=窗, 位=位, 仓=仓))

    def test_开窗必读不超过3072字节(self):
        for 档 in 裁剪.规模表:
            with self.subTest(档=档):
                各件 = 裁剪.生成各件(self.条目, 测试卡(档, 裁剪.特征表), "b" * 64)
                self.assertLessEqual(len(各件["开窗必读.md"].encode("utf-8")), 3072)

    def test_开窗过长报错不截原意(self):
        self.卡["项目"]["一句"] = "长" * 1500
        with self.assertRaisesRegex(裁剪.输入错, "超过 3,072 B"):
            裁剪.生成各件(self.条目, self.卡, "b" * 64)

    def test_口径仅按保留条文判断(self):
        self.assertIn("当日口径.md", 裁剪.生成各件(self.条目, self.卡, "b" * 64))
        self.卡["手调"]["减"] = [{"编号": "G-0008", "理由": "这次不用。"}]
        self.assertNotIn("当日口径.md", 裁剪.生成各件(self.条目, self.卡, "b" * 64))
        self.条目[0]["为什么"] = "测试{{口径件}}不在条文时不会开启。"
        self.assertNotIn("当日口径.md", 裁剪.生成各件(self.条目, self.卡, "b" * 64))

    def test_输出格式模块顺序与件头(self):
        各件 = 裁剪.生成各件(list(reversed(self.条目)), 测试卡("大", 裁剪.特征表), "b" * 64)
        self.assertEqual(len(各件), 7)
        for 文件名, 文本 in 各件.items():
            if 文件名.endswith(".md"):
                self.assertIn(裁剪.声明, 文本)
                self.assertIn("a" * 64, 文本)
                self.assertIn("b" * 64, 文本)
                self.assertIn("规模：大", 文本)
        文本 = 各件["宪法.md"]
        self.assertIn("**合成说明1**〔G-0001〕", 文本)
        self.assertIn("\n> 为什么:", 文本)
        self.assertLess(文本.index("## 底线"), 文本.index("## 分工与权责"))
        self.assertNotIn("{{", 文本)

    def test_拆掉清单按拆因分组且详条件随条列出(self):
        各件 = 裁剪.生成各件(self.条目, self.卡, "b" * 64)
        记录 = json.loads(各件["裁剪记录.json"])
        文本 = 各件["拆掉清单.md"]
        self.assertIn("各组节首写加回门槛；条后仅括注与组门槛不同的加回条件。", 文本)
        self.assertIn("## 项目长到中档时加回(4 条)", 文本)
        self.assertIn("门槛：同时窗 ≥4 或分工位 ≥2 或 仓 ≥2 且上线。", 文本)
        self.assertIn("## 长到大档时加回(2 条)", 文本)
        self.assertIn("门槛：同时窗 ≥7 或分工位 ≥6 或（仓 ≥2 且上线 且分工位 ≥4）。", 文本)
        中节 = 文本.split("## 项目长到中档时加回", 1)[1].split("## ", 1)[0]
        大节 = 文本.split("## 长到大档时加回", 1)[1].split("## ", 1)[0]
        self.assertIn("负载与性能:合成说明14〔G-0014〕（另需 代码）", 中节)
        self.assertIn("审查席:合成说明15〔G-0015〕（另需 审查岗）", 大节)
        self.assertIn("真源库:合成说明16〔G-0016〕（另需 数值、多模型、规矩库）", 大节)
        self.assertNotIn("负载与性能:", 大节)
        self.assertNotIn("本次拆因", 文本)
        self.assertTrue(all(记录["加回条件"].values()))
        for 编号 in 记录["拆掉"]:
            self.assertIn(f"（加回条件：{记录['加回条件'][编号]}）", 文本)
            self.assertTrue(记录["拆因"][编号])

    def test_拆掉清单整模块一行列全编号和条件(self):
        条目 = [copy.deepcopy(self.条目[0])]
        for 次, 条件 in enumerate(("最先出现但仅一次。", "{{项目名}}需要多人协作时。", "{{项目名}}需要多人协作时。"), 2):
            条 = copy.deepcopy(self.条目[3])
            条.update(编号=f"G-{次:04d}", 短题=f"待拆说明{次}", 加回条件=条件)
            条目.append(条)
        各件 = 裁剪.生成各件(list(reversed(条目)), self.卡, "b" * 64)
        文本 = 各件["拆掉清单.md"]
        内容行 = [行 for 行 in 文本.splitlines() if 行.startswith("分工与权责:")]
        self.assertEqual(内容行, [
            "分工与权责:待拆说明2〔G-0002〕（加回条件：最先出现但仅一次。）"
            "、待拆说明3〔G-0003〕（加回条件：合成项目需要多人协作时。）"
            "、待拆说明4〔G-0004〕（加回条件：合成项目需要多人协作时。）",
        ])
        self.assertNotIn("全拆", 文本)
        self.assertNotIn("{{", 文本)
        记录 = json.loads(各件["裁剪记录.json"])
        self.assertCountEqual(re.findall(r"G-[0-9]{4}", 文本), 记录["拆掉"])
        self.assertEqual(len(记录["拆掉"]), 3)
        self.assertEqual(记录["加回条件"]["G-0002"], "最先出现但仅一次。")

    def test_拆掉清单部分模块一行列短题(self):
        条目 = copy.deepcopy(self.条目)
        for 次, 题 in ((100, "稍后再用"), (101, "{{项目名}}补充")):
            条 = copy.deepcopy(self.条目[0])
            条.update(编号=f"G-{次:04d}", 短题=题, 最小规模="中", 加回条件="{{项目名}}分工达到 2 位时。")
            条目.append(条)
        各件 = 裁剪.生成各件(list(reversed(条目)), self.卡, "b" * 64)
        文本 = 各件["拆掉清单.md"]
        内容行 = [行 for 行 in 文本.splitlines() if 行.startswith("底线:")]
        self.assertEqual(内容行, [
            "底线:稍后再用〔G-0100〕（加回条件：合成项目分工达到 2 位时。）"
            "、合成项目补充〔G-0101〕（加回条件：合成项目分工达到 2 位时。）",
        ])
        self.assertNotIn("## 底线", 文本)
        self.assertNotIn("本次拆因", 文本)
        self.assertNotIn("- 加回条件", 文本)
        记录 = json.loads(各件["裁剪记录.json"])
        self.assertEqual(记录["拆因"]["G-0100"], "规模低于中")
        self.assertEqual(记录["加回条件"]["G-0100"], "合成项目分工达到 2 位时。")

    def test_拆掉清单七种特征归节且多特征只列一次(self):
        条目 = [copy.deepcopy(self.条目[0])]
        题表 = (
            ("代码", "开始写程序(代码)时加回"), ("上线", "开始部署上线时加回"),
            ("素材", "开始批量做素材时加回"), ("数值", "出现须统一维护的规则或数值时加回"),
            ("多模型", "开始派多家模型时加回"),
            ("审查岗", "决策者要设审查岗时加回"),
            ("规矩库", "决策者要建规矩库时加回"),
        )
        for 次, (特征, _) in enumerate(题表, 100):
            条 = copy.deepcopy(self.条目[0])
            条.update(编号=f"G-{次:04d}", 短题=f"等待{特征}", 需要特征=[特征], 加回条件="该项特征开始时核对。")
            条目.append(条)
        条 = copy.deepcopy(条目[1])
        条.update(编号="G-0200", 短题="等待两项", 需要特征=["上线", "代码"])
        条目.append(条)
        文本 = 裁剪.生成各件(条目, self.卡, "b" * 64)["拆掉清单.md"]
        for 次, (特征, 题) in enumerate(题表, 100):
            with self.subTest(特征=特征):
                节 = 文本.split(f"## {题}\n\n", 1)[1].split("## ", 1)[0]
                self.assertIn(f"等待{特征}〔G-{次:04d}〕", 节)
        代码节 = 文本.split("## 开始写程序(代码)时加回\n\n", 1)[1].split("## ", 1)[0]
        self.assertIn("门槛：在需求卡中开启代码特征，再重裁。", 代码节)
        self.assertIn("等待两项〔G-0200〕（另需 上线）（加回条件：该项特征开始时核对。）", 代码节)
        self.assertEqual(文本.count("〔G-0200〕"), 1)
        self.assertEqual(文本.count("（加回条件：该项特征开始时核对。）"), 8)

    def test_拆掉清单规模与特征双缺归规模且仅标未开特征(self):
        条目 = [copy.deepcopy(self.条目[0])]
        for 次, 档 in ((100, "中"), (101, "大")):
            条 = copy.deepcopy(self.条目[0])
            条.update(编号=f"G-{次:04d}", 短题=f"等待{档}档", 最小规模=档, 需要特征=["上线", "代码"], 加回条件="规模与两项特征均满足时。")
            条目.append(条)
        for 特征, 应标 in (((), "代码、上线"), (("代码",), "上线")):
            with self.subTest(特征=特征):
                文本 = 裁剪.生成各件(条目, 测试卡(特征=特征), "b" * 64)["拆掉清单.md"]
                for 次, 题 in ((100, "项目长到中档时加回"), (101, "长到大档时加回")):
                    节 = 文本.split(f"## {题}(1 条)\n\n", 1)[1].split("## ", 1)[0]
                    self.assertIn(f"〔G-{次:04d}〕（另需 {应标}）（加回条件：规模与两项特征均满足时。）", 节)
                    self.assertEqual(文本.count(f"〔G-{次:04d}〕"), 1)
                self.assertNotIn("## 开始写程序", 文本)
                self.assertNotIn("## 开始部署上线", 文本)

    def test_拆掉清单多特征已开一项后按剩余项归节(self):
        条 = copy.deepcopy(self.条目[0])
        条.update(编号="G-0100", 短题="等待程序上线", 需要特征=["上线", "代码"], 加回条件="程序上线时核对。")
        for 已开, 应在, 不在 in ((("代码",), "开始部署上线时加回", "开始写程序(代码)时加回"),
                                  (("上线",), "开始写程序(代码)时加回", "开始部署上线时加回")):
            with self.subTest(已开=已开):
                文本 = 裁剪.生成各件([self.条目[0], 条], 测试卡(特征=已开), "b" * 64)["拆掉清单.md"]
                未开 = "上线" if 已开[0] == "代码" else "代码"
                self.assertIn(f"## {应在}\n\n门槛：在需求卡中开启{未开}特征，再重裁。\n\n", 文本)
                self.assertIn("底线:等待程序上线〔G-0100〕（加回条件：程序上线时核对。）", 文本)
                self.assertNotIn(f"## {不在}", 文本)
                self.assertNotIn("另需", 文本)

    def test_拆掉清单整模块跨节不混拆因(self):
        条目 = [copy.deepcopy(self.条目[0])]
        for 次, 档, 最大, 特征 in ((100, "大", "大", ["代码"]), (101, "大", "大", ["代码"]),
                                  (102, "大", "大", ["素材"]), (103, "小", "大", ["素材"]),
                                  (104, "小", "小", [])):
            条 = copy.deepcopy(self.条目[3])
            条.update(编号=f"G-{次:04d}", 短题=f"跨节说明{次}", 最小规模=档, 最大规模=最大, 需要特征=特征)
            条目.append(条)
        文本 = 裁剪.生成各件(条目, 测试卡("中"), "b" * 64)["拆掉清单.md"]
        条件 = "（加回条件：" + self.条目[3]["加回条件"] + "）"
        for 题, 应有 in (
            ("长到大档时加回(3 条)",
             f"分工与权责:跨节说明100〔G-0100〕（另需 代码）{条件}"
             f"、跨节说明101〔G-0101〕（另需 代码）{条件}、跨节说明102〔G-0102〕（另需 素材）{条件}"),
            ("开始批量做素材时加回", f"分工与权责:跨节说明103〔G-0103〕{条件}"),
            ("本档已有完整版、不用的简化条", f"分工与权责:跨节说明104〔G-0104〕{条件}"),
        ):
            with self.subTest(题=题):
                节 = 文本.split(f"## {题}\n\n", 1)[1].split("## ", 1)[0]
                self.assertEqual([行 for 行 in 节.splitlines() if 行.startswith("分工与权责:")], [应有])
        self.assertEqual(文本.count("分工与权责:"), 3)
        self.assertCountEqual(re.findall(r"G-[0-9]{4}", 文本), [f"G-{次:04d}" for 次 in range(100, 105)])

    def test_拆掉清单简化条单列且不误归特征(self):
        条目 = [copy.deepcopy(self.条目[0])]
        for 次, 特征 in ((100, []), (101, ["代码"])):
            条 = copy.deepcopy(self.条目[0])
            条.update(编号=f"G-{次:04d}", 短题=f"简化说明{次}", 最大规模="小", 需要特征=特征, 加回条件="小项目使用此简化说明。")
            条目.append(条)
        文本 = 裁剪.生成各件(条目, 测试卡("中"), "b" * 64)["拆掉清单.md"]
        节 = 文本.split("## 本档已有完整版、不用的简化条\n\n", 1)[1].strip().splitlines()
        self.assertEqual(节, [
            "门槛：项目规模回到条目适用范围时，改需求卡重裁。", "",
            "底线:简化说明100〔G-0100〕（加回条件：小项目使用此简化说明。）"
            "、简化说明101〔G-0101〕（加回条件：小项目使用此简化说明。）",
        ])
        self.assertNotIn("## 开始写程序", 文本)
        self.assertNotIn("另需", 文本)

    def test_拆掉清单手调减单列且理由保留记录(self):
        条 = copy.deepcopy(self.条目[0])
        条.update(编号="G-0100", 短题="手动暂缓", 最小规模="中", 需要特征=["代码"], 加回条件="中档开始写程序时。")
        self.卡["手调"]["减"] = [{"编号": "G-0100", "理由": "此次有意暂缓。"}]
        各件 = 裁剪.生成各件([self.条目[0], 条], self.卡, "b" * 64)
        文本 = 各件["拆掉清单.md"]
        self.assertIn("## 手调拆掉的条目", 文本)
        self.assertIn("底线:手动暂缓〔G-0100〕", 文本)
        self.assertNotIn("## 项目长到中档", 文本)
        self.assertNotIn("## 开始写程序", 文本)
        记录 = json.loads(各件["裁剪记录.json"])
        self.assertEqual(记录["拆因"]["G-0100"], "手调减：此次有意暂缓。")

    def test_拆掉清单条件不同才括注且各组有门槛(self):
        组表 = (
            ("中", {"最小规模": "中"}, "同时窗 ≥4 或分工位 ≥2 或 仓 ≥2 且上线。"),
            ("大", {"最小规模": "大"}, "同时窗 ≥7 或分工位 ≥6 或（仓 ≥2 且上线 且分工位 ≥4）。"),
            ("代码", {"需要特征": ["代码"]}, "在需求卡中开启代码特征，再重裁。"),
            ("上线", {"需要特征": ["上线"]}, "在需求卡中开启上线特征，再重裁。"),
            ("素材", {"需要特征": ["素材"]}, "在需求卡中开启素材特征，再重裁。"),
            ("数值", {"需要特征": ["数值"]}, "在需求卡中开启数值特征，再重裁。"),
            ("多模型", {"需要特征": ["多模型"]}, "在需求卡中开启多模型特征，再重裁。"),
            ("审查岗", {"需要特征": ["审查岗"]}, "决策者要设审查岗时加回"),
            ("规矩库", {"需要特征": ["规矩库"]}, "决策者要建规矩库时加回"),
            ("简化", {"最大规模": "小"}, "项目规模回到条目适用范围时，改需求卡重裁。"),
            ("手调", {}, "重新需要时在需求卡中调整手调减，再重裁。"),
        )
        for 组, 修改, 门槛 in 组表:
            with self.subTest(组=组):
                卡 = 测试卡("中" if 组 == "简化" else "小")
                卡["称呼"]["留痕处"] = 门槛
                条目 = [copy.deepcopy(self.条目[0])]
                for 次, 条件 in ((100, "{{留痕处}}"), (101, "{{项目名}}另有 1 项明确需求时。")):
                    条 = copy.deepcopy(self.条目[0])
                    条.update(修改)
                    条.update(编号=f"G-{次:04d}", 短题=f"条件说明{次}", 加回条件=条件)
                    条目.append(条)
                if 组 == "手调":
                    卡["手调"]["减"] = [{"编号": 条["编号"], "理由": "检查加回门槛。"} for 条 in 条目[1:]]
                文本 = 裁剪.生成各件(条目, 卡, "b" * 64)["拆掉清单.md"]
                self.assertIn("门槛：" + 门槛 + "\n\n", 文本)
                self.assertEqual(文本.split("门槛：", 1)[1].count(门槛), 1)
                self.assertIn("底线:条件说明100〔G-0100〕、条件说明101〔G-0101〕", 文本)
                self.assertIn("条件说明101〔G-0101〕（加回条件：合成项目另有 1 项明确需求时。）", 文本)
                self.assertEqual(文本.count("（加回条件："), 1)
                self.assertNotIn("{{", 文本)

    def test_拆掉清单缺省加回说明也随条列出(self):
        for 组, 修改, 条件 in (
            ("简化", {"最大规模": "小"}, "项目规模回到小至小时，改需求卡重裁。"),
            ("手调", {}, "重新需要这条时，在需求卡 [手调] 加中写编号与理由，再重裁。"),
        ):
            with self.subTest(组=组):
                条 = copy.deepcopy(self.条目[0])
                条.update(修改)
                条.update(编号="G-0100", 短题="缺省说明")
                del 条["加回条件"]
                卡 = 测试卡("中")
                if 组 == "手调":
                    卡["手调"]["减"] = [{"编号": 条["编号"], "理由": "检查缺省加回说明。"}]
                文本 = 裁剪.生成各件([self.条目[0], 条], 卡, "b" * 64)["拆掉清单.md"]
                self.assertIn(f"缺省说明〔G-0100〕（加回条件：{条件}）", 文本)

    def test_拆掉清单每个被拆编号恰好出现一次(self):
        条目 = copy.deepcopy(self.条目)
        条 = copy.deepcopy(self.条目[0])
        条.update(编号="G-0100", 短题="小档简化", 最大规模="小")
        条目.append(条)
        for 档 in 裁剪.规模表:
            for 特征 in ((), ("代码",), 裁剪.特征表):
                with self.subTest(档=档, 特征=特征):
                    卡 = 测试卡(档, 特征)
                    卡["手调"]["减"] = [{"编号": "G-0002", "理由": "检查手调条仅列一次。"}]
                    各件 = 裁剪.生成各件(条目, 卡, "b" * 64)
                    记录 = json.loads(各件["裁剪记录.json"])
                    编号表 = re.findall(r"G-[0-9]{4}", 各件["拆掉清单.md"])
                    self.assertCountEqual(编号表, 记录["拆掉"])
                    self.assertEqual(len(编号表), len(set(编号表)))

    def test_拆掉清单无拆条时不输出空节(self):
        文本 = 裁剪.生成各件(self.条目, 测试卡("大", 裁剪.特征表), "b" * 64)["拆掉清单.md"]
        self.assertIn("本次没有拆掉条目。", 文本)
        self.assertNotIn("## ", 文本)

    def test_拆掉清单不受条目输入顺序影响(self):
        正序 = 裁剪.生成各件(self.条目, self.卡, "b" * 64)["拆掉清单.md"]
        倒序 = 裁剪.生成各件(list(reversed(self.条目)), self.卡, "b" * 64)["拆掉清单.md"]
        self.assertEqual(正序, 倒序)

    def test_表包含各字段并按模块(self):
        文本 = 裁剪.生成表(list(reversed(self.条目)), "b" * 64)
        for 词 in ("规模：", "特征：", "依赖：", "加回条件：", "> 为什么:", "源条数："):
            self.assertIn(词, 文本)
        self.assertLess(文本.index("## 底线"), 文本.index("## 真源库"))
        表 = self.写表()
        码, _, _ = self.调命令(["表", "--条目", str(表)])
        self.assertEqual(码, 0)
        self.assertTrue(表.with_name("条目表.md").is_file())

    def test_校验命令通过与非零(self):
        表 = self.写表()
        self.assertEqual(self.调命令(["校验", "--条目", str(表)])[0], 0)
        self.条目[0]["模块"] = "未知"
        self.assertEqual(self.调命令(["校验", "--条目", str(self.写表())])[0], 1)

    def test_损坏JSON与TOML报错(self):
        表 = self.目录 / "坏.jsonl"
        表.write_text("{\n", encoding="utf-8")
        self.assertEqual(self.调命令(["校验", "--条目", str(表)])[0], 1)
        卡 = self.目录 / "坏.toml"
        卡.write_text("[项目\n", encoding="utf-8")
        with self.assertRaises(裁剪.输入错):
            裁剪.读卡(卡)

    def test_摘要按输入原字节计算(self):
        表 = self.写表()
        卡路径 = self.写卡()
        _, 摘要 = 裁剪.读条目(表)
        卡 = 裁剪.读卡(卡路径)
        self.assertEqual(摘要, hashlib.sha256(表.read_bytes()).hexdigest())
        self.assertEqual(卡["sha256"], hashlib.sha256(卡路径.read_bytes()).hexdigest())

    def test_五份预设各裁夹具(self):
        预设 = ("大型研发", "中型软件", "小型软件", "小说写作", "短视频")
        禁模块 = {"代码与仓库", "发布与上线", "负载与性能", "审查席", "真源库"}
        for 名 in 预设:
            with self.subTest(预设=名):
                卡 = 裁剪.读卡(裁剪.包目录 / "档案" / f"{名}.toml")
                各件 = 裁剪.生成各件(self.条目, 卡, "b" * 64)
                记录 = json.loads(各件["裁剪记录.json"])
                self.assertLessEqual(len(各件["开窗必读.md"].encode("utf-8")), 3072)
                if 名 == "大型研发":
                    self.assertEqual(记录["拆掉"], [])
                if 名 in ("小说写作", "短视频"):
                    self.assertFalse(禁模块.intersection(记录["保留模块"]))
                    self.assertTrue({"底线", "决策与原话", "真源与完成"}.issubset(记录["保留模块"]))
                    self.assertLessEqual(sum(len(各件[件].encode("utf-8")) for 件 in ("宪法.md", "作业规矩.md")), 12288)
                if 名 == "中型软件":
                    self.assertLessEqual(sum(len(各件[件].encode("utf-8")) for 件 in ("宪法.md", "作业规矩.md")), 30720)

    def test_五份预设数字与档一致(self):
        for 名, 应有 in (("大型研发", "大"), ("中型软件", "中"), ("小型软件", "小"), ("小说写作", "小"), ("短视频", "小")):
            with self.subTest(预设=名):
                错误 = io.StringIO()
                with contextlib.redirect_stderr(错误):
                    卡 = 裁剪.读卡(裁剪.包目录 / "档案" / f"{名}.toml")
                self.assertEqual(错误.getvalue(), "")
                self.assertEqual(卡["规模"], 应有)
                self.assertEqual(裁剪.推算规模(**卡["数字"], 上线=卡["特征"]["上线"]), 应有)
                if 名 in ("小说写作", "短视频"):
                    self.assertEqual(卡["数字"], {"同时窗数": 2 if 名 == "小说写作" else 3, "分工位数": 1, "仓数": 0})

    def test_档案命令列五份(self):
        码, 输出, 错误 = self.调命令(["档案"])
        self.assertEqual(码, 0)
        self.assertEqual(错误, "")
        self.assertEqual(len(输出.splitlines()), 5)
        self.assertIn("小说写作.toml：规模=小；特征=无", 输出)

    def test_自定优先声明与开窗顺序(self):
        各件 = 裁剪.生成各件(self.条目, self.卡, "b" * 64)
        声明 = "本项目独有的要求见 `本项目自定.md`，与本件冲突以它为准。"
        self.assertIn(声明, 各件["宪法.md"])
        self.assertLess(各件["宪法.md"].index(声明), 各件["宪法.md"].index("## 底线"))
        文本 = 各件["开窗必读.md"]
        self.assertLess(文本.index("本项目自定.md"), 文本.index("宪法.md"))
        self.assertLess(文本.index("宪法.md"), 文本.index("作业规矩.md"))

    def test_无本项目自定时只出样板(self):
        目录 = self.目录 / "输出"
        码, 输出, 错误 = self.调命令([
            "出", "--卡", str(self.写卡()), "--条目", str(self.写表()), "--出", str(目录),
        ])
        self.assertEqual(码, 0)
        self.assertEqual(错误, "")
        self.assertIn("输出 8 件", 输出)
        self.assertFalse((目录 / "本项目自定.md").exists())
        self.assertEqual((目录 / "本项目自定_样板.md").read_text(encoding="utf-8"),
                         "# 本项目自定\n\n"
                         "每条一行：要求 —— 出处:资料/决策者原话.md 第 N 条\n"
                         "与通用条冲突时以本件为准，并在该条后写「压 G-xxxx」。\n")

    def test_已有本项目自定时不写不挪(self):
        目录 = self.目录 / "输出"
        目录.mkdir()
        自定 = 目录 / "本项目自定.md"
        原文 = "# 本项目自定\r\n\r\n设定不得前后矛盾 —— 出处:资料/决策者原话.md 第 1 条\r\n".encode("utf-8")
        自定.write_bytes(原文)
        os.utime(自定, ns=(1_000_000_000, 1_000_000_000))
        原状态 = 自定.stat()
        码, 输出, 错误 = self.调命令([
            "出", "--卡", str(self.写卡()), "--条目", str(self.写表()), "--出", str(目录),
        ])
        self.assertEqual(码, 0)
        self.assertEqual(错误, "")
        self.assertIn("输出 7 件", 输出)
        self.assertEqual(自定.read_bytes(), 原文)
        self.assertEqual(自定.stat().st_mtime_ns, 原状态.st_mtime_ns)
        self.assertEqual(自定.stat().st_ino, 原状态.st_ino)
        self.assertFalse((目录 / "本项目自定_样板.md").exists())
        self.assertFalse((目录 / "_旧版").exists())

    def test_有本件时已有样板也保持原样(self):
        自定 = self.目录 / "本项目自定.md"
        样板 = self.目录 / "本项目自定_样板.md"
        自定.write_text("手写要求\n", encoding="utf-8")
        样板.write_text("原有样板\n", encoding="utf-8")
        os.utime(样板, ns=(1_000_000_000, 1_000_000_000))
        原状态 = 样板.stat()
        self.assertEqual(裁剪.写各件(self.目录, {"宪法.md": "新宪法\n"}), 1)
        self.assertEqual(自定.read_text(encoding="utf-8"), "手写要求\n")
        self.assertEqual(样板.read_text(encoding="utf-8"), "原有样板\n")
        self.assertEqual(样板.stat().st_mtime_ns, 原状态.st_mtime_ns)
        self.assertEqual(样板.stat().st_ino, 原状态.st_ino)
        self.assertFalse((self.目录 / "_旧版").exists())

    def test_重裁不碰本项目自定(self):
        目录 = self.目录 / "输出"
        参数 = ["出", "--卡", str(self.写卡()), "--条目", str(self.写表()), "--出", str(目录)]
        self.assertEqual(self.调命令(参数)[0], 0)
        旧宪法 = (目录 / "宪法.md").read_bytes()
        自定 = 目录 / "本项目自定.md"
        原文 = "发布前决策者过目 —— 出处:资料/决策者原话.md 第 2 条；压 G-0002\r\n".encode("utf-8")
        自定.write_bytes(原文)
        os.utime(自定, ns=(1_000_000_000, 1_000_000_000))
        原状态 = 自定.stat()
        样板 = 目录 / "本项目自定_样板.md"
        旧样板 = 样板.read_bytes()
        样板状态 = 样板.stat()
        for 次 in (1, 2):
            with self.subTest(次=次):
                码, 输出, 错误 = self.调命令(参数)
                self.assertEqual(码, 0)
                self.assertEqual(错误, "")
                self.assertIn("输出 7 件", 输出)
                self.assertEqual(自定.read_bytes(), 原文)
                self.assertEqual(自定.stat().st_mtime_ns, 原状态.st_mtime_ns)
                self.assertEqual(自定.stat().st_ino, 原状态.st_ino)
                self.assertEqual(样板.read_bytes(), 旧样板)
                self.assertEqual(样板.stat().st_mtime_ns, 样板状态.st_mtime_ns)
                self.assertEqual(样板.stat().st_ino, 样板状态.st_ino)
                旧目录 = 目录 / "_旧版" / f"第{次}次"
                self.assertEqual((旧目录 / "宪法.md").read_bytes(), 旧宪法)
                self.assertEqual(list(旧目录.rglob("本项目自定.md")), [])
                self.assertEqual(list(旧目录.rglob("本项目自定_样板.md")), [])

    def test_无本件重裁只挪旧样板(self):
        裁剪.写各件(self.目录, {"宪法.md": "第一版\n"})
        样板 = self.目录 / "本项目自定_样板.md"
        样板.write_text("旧样板\n", encoding="utf-8")
        裁剪.写各件(self.目录, {"宪法.md": "第二版\n"})
        self.assertEqual(样板.read_text(encoding="utf-8"), 裁剪.自定样板)
        旧样板 = self.目录 / "_旧版" / "第1次" / "本项目自定_样板.md"
        self.assertEqual(旧样板.read_text(encoding="utf-8"), "旧样板\n")
        self.assertFalse((self.目录 / "本项目自定.md").exists())

    def test_写各件不生成或覆盖本项目自定(self):
        各件 = {"宪法.md": "测试宪法\n", "本项目自定.md": "不得写入\n"}
        self.assertEqual(裁剪.写各件(self.目录, 各件), 2)
        自定 = self.目录 / "本项目自定.md"
        self.assertFalse(自定.exists())
        自定.write_text("手写原文\n", encoding="utf-8")
        self.assertEqual(裁剪.写各件(self.目录, 各件), 1)
        self.assertEqual(自定.read_text(encoding="utf-8"), "手写原文\n")
        self.assertEqual(list((self.目录 / "_旧版").rglob("本项目自定.md")), [])
        self.assertIn("本项目自定.md", 各件)

    def test_重复裁剪旧件挪走不删(self):
        目录 = self.目录 / "输出"
        裁剪.写各件(目录, {"宪法.md": "第一版\n", "当日口径.md": "旧口径\n"})
        (目录 / "手写资料.md").write_text("保留\n", encoding="utf-8")
        裁剪.写各件(目录, {"宪法.md": "第二版\n"})
        self.assertEqual((目录 / "宪法.md").read_text(encoding="utf-8"), "第二版\n")
        self.assertFalse((目录 / "当日口径.md").exists())
        self.assertEqual((目录 / "_旧版" / "第1次" / "当日口径.md").read_text(encoding="utf-8"), "旧口径\n")
        self.assertTrue((目录 / "手写资料.md").exists())
        裁剪.写各件(目录, {"宪法.md": "第三版\n"})
        self.assertTrue((目录 / "_旧版" / "第2次" / "宪法.md").exists())

    def test_生成文件无BOM(self):
        各件 = 裁剪.生成各件(self.条目, self.卡, "b" * 64)
        裁剪.写各件(self.目录 / "输出", 各件)
        for 文件 in (self.目录 / "输出").iterdir():
            self.assertFalse(文件.read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
