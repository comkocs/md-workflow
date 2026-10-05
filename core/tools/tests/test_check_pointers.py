"""自足夹具:tmp 里建迷你私库/办公仓/台面库,不依赖真仓真库。"""

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_pointers.py"
SPEC = importlib.util.spec_from_file_location("check_pointers", SCRIPT)
check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)

CONSTITUTION = "\n".join([
    "# 迷你宪法 v1.0",
    "",
    "### 闸 1 · 甲闸 【①核心】",
    "- 拦:①类事故演示",
    "- 回口:演示",
    "- 拆除:不拆",
    "",
    "### 闸 2 · 乙闸 【②可选】",
    "- 拦:②类事故演示",
    "- 回口:演示",
    "- 拆除:不拆",
    "",
])

TEST_MD = "\n".join([
    "# 指针演练",
    "",
    "- 悬空闸号:闸 9",
    "- 正常闸号:闸 1",
    "- 悬空版本:宪法 v9.9",
    "- 正常版本:宪法 v1.0",
    "- 正常模板:任务书模板_v2",
    "- 悬空路径:desk/没有.md",
    "- 正常路径:desk/朗文.md",
    "- 悬空需求:需求-777",
    "- 正常需求:需求-001",
    "- 正常单号:T-000001",
    "- 悬空文件行:朗文不存在.md:100",
    "- 正常文件行:朗文.md:003「第三行」",
    "",
])

LONG_MD = "\n".join(["第一行", "第二行", "第三行", "第四行", "第五行"])

LABELS = {"①闸号", "②版本号", "③路径", "④需求号单号", "⑤文件:行"}


class CheckPointersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="check-pointers-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / "repo"
        self.office = base / "office"
        self.data = base / "data"
        self.out1 = base / "out1"
        self.out2 = base / "out2"
        self.out3 = base / "out3"
        self.write(self.repo / "desk" / "宪法.md", CONSTITUTION)
        self.write(self.repo / "desk" / "测试.md", TEST_MD)
        self.write(self.repo / "desk" / "朗文.md", LONG_MD)
        self.write(self.office / "需求-001_迷你需求.md", "# 迷你需求\n")
        self.write(self.office / "0号任务书模板_v2_迷你.md", "# 模板占位\n")
        self.write(self.data / "items" / "T-000001.json",
                   '{"编号": "T-000001"}\n')

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def read(self, path):
        return path.read_text(encoding="utf-8")

    def run_cli(self, out, baseline=None):
        command = [sys.executable, "-X", "utf8", str(SCRIPT),
                   "--repo", str(self.repo), "--office", str(self.office),
                   "--tickets-data", str(self.data), "--out", str(out)]
        if baseline is not None:
            command += ["--baseline", str(baseline)]
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=120)
        return result.returncode, result.stdout.decode("utf-8", "replace"), \
            result.stderr.decode("utf-8", "replace")

    def dangling_entries(self, text):
        return [line for line in text.splitlines()
                if line.startswith("- [") and ":" in line]

    def test_five_categories_each_one_dangling_and_one_valid(self):
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        report = self.read(self.out1 / "悬空清单.md")
        self.assertIn("总指针数 15 · 悬空数 5", report)
        self.assertIn("覆盖:desk/ 5 · .agents/skills/ 不存在 · "
                      ".claude/skills/ 不存在 · _office/ 0", report)
        entries = [line for line in report.splitlines() if re.match(r"^- \[", line)]
        self.assertEqual(len(entries), 5)
        seen = set()
        for line in entries:
            match = re.match(r"^- \[([^\]]+)\] (\S+?):(\d+) 「(.+)」$", line)
            self.assertTrue(match, line)
            self.assertIn(match.group(1), LABELS)
            self.assertEqual(match.group(2), "desk/测试.md", line)
            seen.add(match.group(1))
        self.assertEqual(seen, LABELS)

    def test_incremental_new_and_gone(self):
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        fixed = TEST_MD.replace("- 悬空版本:宪法 v9.9", "- 修好版本:宪法 v1.0")
        self.write(self.repo / "desk" / "测试.md", fixed + "- 追加悬空:闸 11\n")
        code, _, _ = self.run_cli(self.out2, baseline=self.out1)
        self.assertEqual(code, 0)
        fresh = self.dangling_entries(self.read(self.out2 / "新悬空.md"))
        gone = self.dangling_entries(self.read(self.out2 / "已消失.md"))
        self.assertEqual(len(fresh), 1)
        self.assertIn("「闸 11」", fresh[0])
        self.assertEqual(len(gone), 1)
        self.assertIn("「宪法 v9.9」", gone[0])

    def test_gate_subitem_only_searched_below_heading(self):
        # 024Br1 不符项 1:子项只在标题行以下的正文里找;标题行的【①核心】
        # 【②可选】不算子项,无子项的闸配 ①/② 必须判悬空。
        constitution = "\n".join([
            "# 迷你宪法 v1.0",
            "",
            "### 闸 4 · 丁闸 【①核心】",
            "- 拦:别类事故",
            "",
            "### 闸 5 · 戊闸 【②可选】",
            "- 拦:②类事故",
            "",
        ])
        self.write(self.repo / "desk" / "宪法.md", constitution)
        self.write(self.repo / "desk" / "探针闸.md", "\n".join([
            "- 悬空子项:闸 4①",
            "- 正常子项:闸 5②",
            "",
        ]))
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        report = self.read(self.out1 / "悬空清单.md")
        self.assertIn("「闸 4①」", report)
        self.assertNotIn("「闸 5②」", report)

    def test_backtick_separated_paths_judged_separately(self):
        # 024Br1 不符项 2:反引号并列的多条绝对路径在反引号与「、」处截断,
        # 每条单独核;不得并成一条判悬空、漏核第二条。
        good1 = (self.repo / "desk").as_posix()
        good2 = self.office.as_posix()
        bad = (self.repo / "desk" / "没有的目录").as_posix()
        self.write(self.repo / "desk" / "探针路径.md",
                   "- 并列:`{}`、`{}`、`{}`\n".format(good1, bad, good2))
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        report = self.read(self.out1 / "悬空清单.md")
        entries = [line for line in report.splitlines()
                   if line.startswith("- [③路径]") and "探针路径.md" in line]
        self.assertEqual(len(entries), 1, entries)
        self.assertIn(bad, entries[0])
        for line in entries:
            self.assertNotIn("`", line)
            self.assertNotIn("、", line)

    def test_file_line_accepts_short_line_numbers(self):
        # 024Br1 不符项 3:「文件:行」的行号任意位数都识别,1~99 行不再漏核。
        self.write(self.repo / "desk" / "探针行号.md", "\n".join([
            "- 悬空两位:朗文不存在.md:95",
            "- 悬空原句不符:朗文.md:3「这句不存在」",
            "- 正常一位:朗文.md:4「第四行」",
            "- 正常两位:朗文.md:05「第五行」",
            "",
        ]))
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        report = self.read(self.out1 / "悬空清单.md")
        self.assertIn("「朗文不存在.md:95」", report)
        self.assertIn("desk/探针行号.md:2 「朗文.md:3」", report)
        self.assertNotIn("「朗文.md:4", report)
        self.assertNotIn("「朗文.md:05", report)

    def test_increment_ignores_line_number_shift(self):
        # 024Br1 不符项 4(插在中间用例):基线里有一条悬空指向某文件第 X 行,
        # 在文件中间插一行使它挪到 X+1;带 --baseline 重跑,该条必须既不进
        # 「新悬空」也不进「已消失」(比对键=(类别,原文,文件),不含行号)。
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.dangling_entries(self.read(self.out1 / "悬空清单.md"))), 5)
        shifted = TEST_MD.replace("- 悬空路径:desk/没有.md",
                                  "插入中间行:无指针\n- 悬空路径:desk/没有.md")
        self.write(self.repo / "desk" / "测试.md", shifted)
        code, _, _ = self.run_cli(self.out2, baseline=self.out1)
        self.assertEqual(code, 0)
        fresh = self.dangling_entries(self.read(self.out2 / "新悬空.md"))
        gone = self.dangling_entries(self.read(self.out2 / "已消失.md"))
        self.assertEqual(fresh, [])
        self.assertEqual(gone, [])

    def test_increment_counts_duplicate_pointers(self):
        # 024Br1 不符项 4(重复指针):同一悬空指针从 1 份变 2 份,只按差额
        # 计 1 条「新悬空」;份数没变的不进任何增量。
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        doubled = TEST_MD.replace("- 悬空闸号:闸 9",
                                  "- 悬空闸号:闸 9\n- 再悬空闸号:闸 9")
        self.write(self.repo / "desk" / "测试.md", doubled)
        code, _, _ = self.run_cli(self.out2, baseline=self.out1)
        self.assertEqual(code, 0)
        fresh = self.dangling_entries(self.read(self.out2 / "新悬空.md"))
        gone = self.dangling_entries(self.read(self.out2 / "已消失.md"))
        self.assertEqual(len(fresh), 1)
        self.assertIn("「闸 9」", fresh[0])
        self.assertEqual(gone, [])

    def test_exit_code_always_zero(self):
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        missing = Path(self.temp.name) / "不存在的跑例"
        code, _, _ = self.run_cli(self.out2, baseline=missing)
        self.assertEqual(code, 0)
        self.assertIn("baseline 不存在,跳过增量", self.read(self.out2 / "悬空清单.md"))
        self.assertFalse((self.out2 / "新悬空.md").exists())
        self.assertFalse((self.out2 / "已消失.md").exists())
        code, _, _ = self.run_cli(self.out3)  # 迷你夹具里本就有悬空
        self.assertEqual(code, 0)

    def test_read_only_over_fixtures(self):
        roots = [self.repo, self.office, self.data]
        before = {path: path.read_bytes()
                  for root in roots for path in sorted(root.rglob("*")) if path.is_file()}
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        after = {path: path.read_bytes()
                 for root in roots for path in sorted(root.rglob("*")) if path.is_file()}
        self.assertEqual(before, after)

    def test_missing_claude_skills_reported_in_header(self):
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        report = self.read(self.out1 / "悬空清单.md")
        self.assertIn(".claude/skills/ 不存在", report)
        self.assertIn(".agents/skills/ 不存在", report)

    def make_junction(self, link, target):
        """建目录 junction(Windows 免特权);失败再试 symlink,都不行返回 False。"""
        try:
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=30)
            if result.returncode == 0 and link.is_dir():
                return True
        except OSError:
            pass
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
        return link.is_dir()

    def test_listing_sources_all_exist(self):
        # 用例 F:两仓之外的内容(经 junction 接进办公仓)绝不被扫;
        # 且清单每条明细的源文件按其标签规则解析后必须真实存在。
        outside = Path(self.temp.name) / "outside"
        self.write(outside / "漏网.md", "- 越区悬空:闸 99\n")
        linked = self.make_junction(self.office / ".agents", outside)
        code, _, _ = self.run_cli(self.out1)
        self.assertEqual(code, 0)
        report = self.read(self.out1 / "悬空清单.md")
        self.assertNotIn("漏网", report)
        self.assertNotIn("闸 99", report)
        self.assertIn("总指针数 15 · 悬空数 5", report)
        repo = self.repo.resolve()
        office = self.office.resolve()
        checked = 0
        for line in report.splitlines():
            match = re.match(r"^- \[([^\]]+)\] (.+?):(\d+) 「(.+)」$", line)
            if not match:
                continue
            ref = match.group(2)
            if ref.startswith("_office/"):
                source = office / ref[len("_office/"):]
            else:
                source = repo / ref
            self.assertTrue(source.is_file(), line)
            checked += 1
        self.assertEqual(checked, 5)
        self.assertTrue(linked, "应能建 junction/symlink 以演练越区扫描")


if __name__ == "__main__":
    unittest.main()
