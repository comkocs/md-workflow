"""用临时产品、办公目录与会话记录复验两道只读钩子。"""

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch


脚本目录 = Path(__file__).resolve().parents[1] / "hooks"
防回潮 = "防回潮检查.py"
回灌 = "压缩后回灌.py"
标题 = ("一、本位接管件 §一", "二、当日口径", "三、本窗在跑子代理清单")


class HookTests(unittest.TestCase):
    def setUp(self):
        临时 = tempfile.TemporaryDirectory(prefix="hooks-")
        self.addCleanup(临时.cleanup)
        self.root = Path(临时.name)
        self.repo = self.root / "product"
        self.office = self.root / "办公"
        self.office.mkdir()
        for 名 in (防回潮, 回灌):
            目标 = self.repo / "core/tools/hooks" / 名
            目标.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(脚本目录 / 名, 目标)
        self.write(self.repo / "core/desk_config.json", json.dumps({"办公目录": str(self.office)}, ensure_ascii=False))
        self.write(self.repo / "desk/作废词表.txt", "旧流程词\n旧归档词\n")
        self.write(self.repo / "desk/预算表.md", "| 件 | 量法 | 预算 |\n|---|---|---|\n")
        self.transcript = self.root / "sessions/main.jsonl"

    def write(self, 路径, 内容):
        路径.parent.mkdir(parents=True, exist_ok=True)
        路径.write_bytes(内容.encode("utf-8"))
        return 路径

    def rows(self, 路径, 记录):
        return self.write(路径, "".join(json.dumps(项, ensure_ascii=False) + "\n" for 项 in 记录))

    def snapshot(self):
        return {路径.relative_to(self.root).as_posix(): (路径.read_bytes(), 路径.stat().st_mtime_ns)
                for 路径 in self.root.rglob("*") if 路径.is_file()}

    def run_hook(self, 名, 事件):
        前 = self.snapshot()
        输入 = 事件 if isinstance(事件, str) else json.dumps(事件, ensure_ascii=False)
        结果 = subprocess.run([sys.executable, "-X", "utf8", "-I", str(self.repo / "core/tools/hooks" / 名)],
                                input=输入.encode("utf-8"), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=self.root, timeout=10)
        self.assertEqual(结果.returncode, 0, 结果.stderr.decode("utf-8", "replace"))
        self.assertEqual(结果.stderr, b"")
        self.assertEqual(前, self.snapshot(), "钩子不得新增或改写文件")
        return 结果.stdout.decode("utf-8")

    def check(self, 相对, 内容, **事件):
        self.write(self.office / 相对, 内容)
        return self.run_hook(防回潮, dict(cwd=str(self.office), tool_input={"file_path": 相对}, **事件))

    def hits(self, 输出):
        self.assertEqual(len(输出.splitlines()), 1)
        结果 = json.loads(输出)["hookSpecificOutput"]
        self.assertEqual(结果["hookEventName"], "PostToolUse")
        return [行 for 行 in 结果["additionalContext"].splitlines() if 行.startswith("- ")]

    def session(self, 首句=None, 其他=()):
        if 首句 is None:
            首句 = "请阅读 " + (self.office / "编辑/章程.md").as_posix()
        self.rows(self.transcript, [{"type": "user", "message": {"content": 首句}}, *其他])
        return {"cwd": str(self.root), "transcript_path": str(self.transcript)}

    def contents(self, 输出):
        self.assertEqual(输出.splitlines()[0], "【压缩后回灌】")
        段 = {}
        for 序号, 名 in enumerate(标题):
            前缀 = "\n【" + 名 + "】\n"
            if 前缀 not in 输出:
                continue
            文 = 输出.split(前缀, 1)[1]
            if 序号 + 1 < len(标题):
                文 = 文.split("\n【" + 标题[序号 + 1] + "】\n", 1)[0]
            段[序号] = 文.removesuffix("\n")
        return 段

    def child(self, 编号, 年龄=0, 结束=False, 元=True, 尾=True, 描述="编写材料"):
        目录 = self.transcript.with_suffix("") / "subagents"
        路径列表 = []
        if 元:
            路径列表.append(self.write(目录 / ("agent-" + 编号 + ".meta.json"), json.dumps({
                "requestShape": "background", "toolUseId": "call-" + 编号,
                "description": 描述, "agentType": "worker", "resolvedModel": "model"}, ensure_ascii=False)))
        if 尾:
            路径列表.append(self.rows(目录 / ("agent-" + 编号 + ".jsonl"), [
                {"type": "assistant", "message": {"model": "model", "stop_reason": "end_turn" if 结束 else "tool_use"}}]))
        for 路径 in 路径列表:
            os.utime(路径, (time.time() - 年龄, time.time() - 年龄))

    def test_作废词恰一条且退出零(self):
        命中 = self.hits(self.check("资料.md", "旧流程词\n", tool_name="Write"))
        self.assertEqual(len(命中), 1)
        self.assertIn("作废词", 命中[0])

    def test_版本号恰一条且覆盖所有件类(self):
        for 名 in ("desk/子代理/定义.md", "desk/模板/短书.md", "desk/角色/编辑.md", ".claude/agents/worker.md", "其他/章程.md"):
            with self.subTest(名=名):
                骨架 = "\n".join("## " + 节 for 节 in ("本位管什么", "真源表", "开窗步骤", "规矩"))
                命中 = self.hits(self.check(名, "遵循 v12.34\n" + 骨架))
                self.assertEqual(len(命中), 1)
                self.assertIn("写死版本号", 命中[0])

    def test_宪法附录与普通件不查版本(self):
        for 名 in ("desk/模板/宪法.md", "desk/模板/宪法_历史附录.md", "资料.md"):
            with self.subTest(名=名):
                self.assertEqual(self.check(名, "版本 v3.14\n"), "")

    def test_字节预算恰一条(self):
        self.write(self.repo / "desk/预算表.md", "| 预算 | 件 | 量法 |\n|---|---|---|\n| ≤ 5 | `说明.md` | 字节 |\n")
        命中 = self.hits(self.check("说明.md", "中文"))
        self.assertEqual(len(命中), 1)
        self.assertIn("6 字节 / 预算 5 字节", 命中[0])

    def test_字符预算与不限量(self):
        self.write(self.repo / "desk/预算表.md", "| 件 | 量法 | 预算 |\n|---|---|---|\n"
                   "| 记忆索引 `MEMORY.md` | 字符 | ≤ 2 |\n| `其他.md` | 字节 | 不设上限 |\n")
        self.assertEqual(self.check("MEMORY.md", "中文"), "")
        命中 = self.hits(self.check("MEMORY.md", "中文字"))
        self.assertEqual(len(命中), 1)
        self.assertIn("3 字符 / 预算 2 字符", 命中[0])
        self.assertEqual(self.check("其他.md", "字" * 100), "")

    def test_预算通配路径与千位数(self):
        self.write(self.repo / "desk/预算表.md", "| 件 | 量法 | 预算 |\n|---|---|---|\n"
                   "| `desk/角色/*.md` 章程骨架 | 字节 | ≤ 1,000 |\n")
        self.assertEqual(len(self.hits(self.check("desk/角色/编辑.md", "字" * 334))), 1)
        self.assertEqual(self.check("别处/编辑.md", "字" * 334), "")

    def test_章程缺节恰一条且完整骨架不报(self):
        完整 = "\n".join("## " + 节 for 节 in ("一、本位管什么", "二、真源表", "三、开窗步骤", "四、规矩只引号"))
        self.assertEqual(self.check("编辑/章程.md", 完整), "")
        命中 = self.hits(self.check("编辑/章程.md", 完整.replace("## 三、开窗步骤", "正文提到开窗步骤")))
        self.assertEqual(len(命中), 1)
        self.assertIn("缺节：开窗步骤", 命中[0])

    def test_十二处作废词只列十条(self):
        self.assertEqual(len(self.hits(self.check("资料.md", "旧流程词\n" * 12))), 10)
        self.assertEqual(len(self.hits(self.check("资料.md", "旧流程词 " * 12))), 10)

    def test_不命中为空且退出零(self):
        self.assertEqual(self.check("资料.md", "新的内容", tool_name="Edit"), "")

    def test_非md为空且退出零(self):
        self.assertEqual(self.check("资料.txt", "旧流程词", tool_name="MultiEdit"), "")

    def test_坏json为空且退出零(self):
        for 输入 in ("{", "[]", "null", '{"tool_input":null}'):
            with self.subTest(输入=输入):
                self.assertEqual(self.run_hook(防回潮, 输入), "")

    def test_缺文件及配置错误静默(self):
        self.assertEqual(self.run_hook(防回潮, {"cwd": str(self.office), "tool_input": {"file_path": "无.md"}}), "")
        self.write(self.repo / "core/desk_config.json", "{")
        self.assertEqual(self.check("资料.md", "旧流程词"), "")

    def test_相对与反斜杠路径按cwd补全(self):
        self.write(self.office / "子目录/资料.md", "旧流程词")
        输出 = self.run_hook(防回潮, {"cwd": str(self.office), "tool_input": {"file_path": "子目录\\资料.md"}})
        self.assertEqual(len(self.hits(输出)), 1)

    def test_办公目录相对产品根读取(self):
        self.write(self.repo / "core/desk_config.json", '{"办公目录":"办公"}')
        self.write(self.repo / "办公/编辑/章程.md", "# 空章程\n")
        输出 = self.run_hook(防回潮, {"tool_input": {"file_path": str(self.repo / "办公/编辑/章程.md")}})
        self.assertIn("缺节", self.hits(输出)[0])

    def test_回灌三段齐且只取现状一节(self):
        事件 = self.session()
        self.write(self.office / "编辑/接管_现状.md", "# 现状\n## 一、现状\n正在写材料\n## 二、指针\n不应回灌的指针\n")
        self.write(self.office / "当日口径.md", "今日口径内容")
        self.child("recent")
        输出 = self.run_hook(回灌, 事件)
        段 = self.contents(输出)
        self.assertEqual(set(段), {0, 1, 2})
        self.assertIn("正在写材料", 段[0])
        self.assertNotIn("不应回灌的指针", 输出)
        self.assertEqual(段[1], "今日口径内容")
        self.assertIn("编号：recent", 段[2])

    def test_产品模板节一字面也整段回灌(self):
        事件 = self.session()
        self.write(self.office / "编辑/接管_现状.md",
                   "# 编辑 · 接管_现状\n\n## §一 现状(覆盖式,收窗整段重写)\n正在写材料\n\n## §二 指针(只列路径与命令)\n不应回灌的指针\n")
        输出 = self.run_hook(回灌, 事件)
        段 = self.contents(输出)
        self.assertIn("## §一 现状(覆盖式,收窗整段重写)", 段[0])
        self.assertIn("正在写材料", 段[0])
        self.assertNotIn("不应回灌的指针", 输出)
        self.assertNotIn("本位现状件无 §一", 输出)

    def test_缺现状不回退旧件(self):
        事件 = self.session()
        self.write(self.office / "编辑/接管_旧续段.md", "绝不回灌旧件标记")
        输出 = self.run_hook(回灌, 事件)
        self.assertEqual(self.contents(输出)[0], "本位无现状件")
        self.assertNotIn("绝不回灌旧件标记", 输出)

    def test_缺当日口径有精确提示(self):
        self.assertEqual(self.contents(self.run_hook(回灌, self.session()))[1], "无当日口径")

    def test_判不出位只回灌当日口径(self):
        事件 = self.session("普通开场，不含所属位")
        self.write(self.office / "当日口径.md", "仅此口径")
        self.child("recent")
        self.assertEqual(self.contents(self.run_hook(回灌, 事件)), {1: "仅此口径"})

    def test_只认首条用户消息且不按cwd猜位(self):
        事件 = self.session("普通开场", [{"type": "user", "message": {"content": str(self.office / "编辑/章程.md")}}])
        事件["cwd"] = str(self.office / "编辑")
        self.assertEqual(set(self.contents(self.run_hook(回灌, 事件))), {1})

    def test_120秒内列在跑超过不列(self):
        事件 = self.session()
        self.child("recent", 年龄=20)
        self.child("stale", 年龄=180)
        self.child("finished", 结束=True)
        输出 = self.run_hook(回灌, 事件)
        self.assertIn("编号：recent", 输出)
        self.assertNotIn("编号：stale", 输出)
        self.assertNotIn("编号：finished", 输出)

    def test_主会话Agent调用合并元数据与完成结果(self):
        启动 = dt.datetime.now(dt.timezone.utc).isoformat()
        调用 = {"type": "assistant", "timestamp": 启动, "message": {"content": [
            {"type": "tool_use", "id": "call-recent", "name": "Agent", "input": {
                "description": "主窗描述", "subagent_type": "builder", "run_in_background": True}}]}}
        结果 = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call-recent", "content": "后台已启动"}]},
                "toolUseResult": {"agentId": "recent", "isAsync": True}}
        事件 = self.session(其他=[调用, 结果])
        self.child("recent")
        输出 = self.run_hook(回灌, 事件)
        self.assertEqual(输出.count("编号：recent"), 1)
        self.assertIn("主窗描述", 输出)
        结果["toolUseResult"]["status"] = "completed"
        事件 = self.session(其他=[调用, 结果])
        self.assertNotIn("编号：recent", self.run_hook(回灌, 事件))

    def test_近期主会话调用无子文件也列出(self):
        其他 = [{"type": "assistant", "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "message": {"content": [
            {"type": "tool_use", "id": "running-call", "name": "Agent", "input": {"run_in_background": True}}]}}]
        self.assertIn("编号：running-call", self.run_hook(回灌, self.session(其他=其他)))

    def test_元数据单独与尾部单独均参与清单(self):
        事件 = self.session()
        self.child("meta-only", 尾=False)
        self.child("tail-only", 元=False)
        输出 = self.run_hook(回灌, 事件)
        self.assertIn("编号：meta-only", 输出)
        self.assertIn("编号：tail-only", 输出)

    def test_总长与三段各自上限且只读(self):
        事件 = self.session()
        self.write(self.office / "编辑/接管_现状.md", "## 一、现状\n" + "甲" * 16000)
        self.write(self.office / "当日口径.md", "乙" * 16000)
        for 序号 in range(40):
            self.child("long-" + str(序号), 描述="丙" * 400)
        输出 = self.run_hook(回灌, 事件)
        段 = self.contents(输出)
        self.assertEqual(set(段), {0, 1, 2})
        self.assertLessEqual(len(输出), 30000)
        self.assertEqual([len(段[序号]) for 序号 in range(3)], [12000, 10000, 7000])
        self.assertTrue(all("[本段已截断]" in 段[序号] for 序号 in range(3)))

    def test_判不出位的口径也受段限(self):
        self.write(self.office / "当日口径.md", "甲" * 15000)
        段 = self.contents(self.run_hook(回灌, self.session("普通开场")))
        self.assertEqual(set(段), {1})
        self.assertEqual(len(段[1]), 10000)

    def test_按session_id找会话且只读(self):
        路径 = self.root / "home/.claude/projects/project/session-one.jsonl"
        self.write(路径, "{}\n")
        模块 = types.ModuleType("compact_hook")
        模块.__file__ = str(self.repo / "core/tools/hooks" / 回灌)
        exec(compile((脚本目录 / 回灌).read_bytes(), 模块.__file__, "exec"), 模块.__dict__)
        前 = self.snapshot()
        with patch.object(模块.Path, "home", return_value=self.root / "home"):
            self.assertEqual(模块.会话路径({"session_id": "session-one"}), 路径)
            self.assertIsNone(模块.会话路径({"session_id": "../bad"}))
        self.assertEqual(前, self.snapshot())

    def test_相对会话路径及首句文本块(self):
        首句 = str(self.office / "编辑/接管_现状.md").replace("/", "\\")
        self.rows(self.transcript, [{"type": "user", "isMeta": True, "message": {"content": "元消息"}},
                                   {"type": "user", "message": {"content": [{"type": "text", "text": 首句}]}}])
        输出 = self.run_hook(回灌, {"cwd": str(self.root), "transcript_path": "sessions/main.jsonl"})
        self.assertEqual(set(self.contents(输出)), {0, 1, 2})

    def test_缺会话和坏输入不报错(self):
        for 事件 in ({}, "{", {"transcript_path": "无.jsonl"}):
            with self.subTest(事件=事件):
                self.assertEqual(self.contents(self.run_hook(回灌, 事件)), {1: "无当日口径"})


if __name__ == "__main__":
    unittest.main()
