"""开窗指令第三行带办公目录(需求-022 / T-000036)。

第三行从「新开线程」改成「在 <办公目录> 下新开线程」:各窗一律开在办公目录根上,不开进代码仓或工作树。
★目录只在配置的「办公目录」键写一处(core/desk_config.json,或 TICKET_DESK_CONFIG 指向的那份),
  代码里不写字面值,缺键、空串开库就报错;
★开窗指令仍恒为三行、仍不带平台名(那两条旧闸照守,见 test_ticket_system 的 test_5 / test_r4_5);
★`ticket.py -h`、`new -h`、`set -h` 看得到目录取自哪份配置、当前值是什么。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from tools.tickets import config, model
from tools.tickets.service import TicketService
from tools.tickets.store import TicketStore
from . import test_ticket_system as existing

SLOT = existing.SLOT
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "desk_config.json"


def third_line(tier: str, office: str | None = None) -> str:
    return f"【操作提示·只给设计者】在 {office or config.OFFICE_DIR} 下新开线程,任务档 {tier},模型你定,贴上面那句。"


class WindowDirTests(existing.TicketTestCase):
    def new_dispatch(self, root: Path, taskbook: Path | None, tier: str = "甲", **extra: str):
        worker = TicketService(TicketStore(root)).staff_new(SLOT, "sol")["员工名"]
        command = [
            "new", "--type", "派单", "--slot", SLOT, "--title", "开窗目录",
            "--source", "需求-022", "--consumer", "设计者开窗",
            "--deliverable", str(self.deliverable), "--tier", tier, "--assign", worker, "--internal",
        ]
        if taskbook is not None:
            command += ["--taskbook", str(taskbook)]
        return worker, existing.run_local_cli(command, root, **extra)

    def taskbook(self, name: str) -> Path:
        path = self.root / name
        path.write_text("# 任务书\n", encoding="utf-8")
        return path

    def test_1_new_echo_is_four_lines_and_line_four_names_the_office_dir(self):
        # 夹具值就是这里读到的值:证明 OFFICE_DIR 来自配置文件,不是代码里另有一份。
        self.assertEqual(json.loads(FIXTURE.read_text(encoding="utf-8"))["办公目录"], config.OFFICE_DIR)
        root = self.root / "new-echo"
        _, created = self.new_dispatch(root, self.taskbook("tb-new.md"))
        self.assertEqual(0, created.returncode, created.stderr)
        lines = created.stdout.splitlines()
        self.assertEqual(4, len(lines), created.stdout)
        self.assertEqual(third_line("甲"), lines[3])
        for name in model.WINDOW_PLATFORMS:
            self.assertNotIn(name, lines[3])

    def test_2_set_taskbook_echo_ends_with_the_same_three_lines(self):
        root = self.root / "set-echo"
        worker, created = self.new_dispatch(root, None, tier="乙")
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = existing.created_ticket_id(created)
        taskbook = self.taskbook("tb-set.md")
        changed = existing.run_local_cli(["set", ticket_id, "--taskbook", str(taskbook), "--by", SLOT], root)
        self.assertEqual(0, changed.returncode, changed.stderr)
        lines = changed.stdout.splitlines()
        # set 的回显前面还有单摘要与「已改 …」各行,开窗指令是末三行,与卡片上的逐字相同。
        expected = TicketService(TicketStore(root)).dispatch_instructions(TicketStore(root).load_ticket(ticket_id))
        self.assertEqual(3, len(expected))
        self.assertEqual(expected, lines[-3:])
        self.assertIn(f" claim {ticket_id} --by {worker}", lines[-3])
        self.assertEqual(third_line("乙"), lines[-1])
        for name in model.WINDOW_PLATFORMS:
            self.assertNotIn(name, lines[-1])

    def test_3_line_three_follows_the_config_value(self):
        """换一份配置,第三行跟着换——目录字符串只有配置这一处来源。"""
        values = json.loads(FIXTURE.read_text(encoding="utf-8"))
        values["办公目录"] = "F:/另一处/办公室"
        other = self.root / "other_config.json"
        other.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        root = self.root / "other-config"
        _, created = self.new_dispatch(root, self.taskbook("tb-other.md"), TICKET_DESK_CONFIG=str(other))
        self.assertEqual(0, created.returncode, created.stderr)
        lines = created.stdout.splitlines()
        self.assertEqual(4, len(lines), created.stdout)
        self.assertEqual(third_line("甲", "F:/另一处/办公室"), lines[3])
        self.assertNotIn(config.OFFICE_DIR, created.stdout)

    def test_4_help_shows_where_the_dir_comes_from(self):
        shown_path = str(config.PATH).replace("\\", "/")
        for arguments in (["-h"], ["new", "-h"], ["set", "-h"]):
            with self.subTest(arguments=arguments):
                helped = existing.run_local_cli(arguments, self.root / "help")
                self.assertEqual(0, helped.returncode, helped.stderr)
                self.assertIn(f"开窗目录取自 {shown_path} 的「办公目录」键,当前值 {config.OFFICE_DIR}", helped.stdout)
                self.assertIn("远程模式以服务端那份配置为准", helped.stdout)

    def test_5_missing_blank_or_non_string_key_is_refused(self):
        base = json.loads(FIXTURE.read_text(encoding="utf-8"))
        config.validate(base, FIXTURE)  # 夹具本身合法
        missing = copy.deepcopy(base)
        del missing["办公目录"]
        for label, values in (
            ("缺键", missing),
            ("空串", dict(base, 办公目录="  ")),
            ("不是字符串", dict(base, 办公目录=["E:/work/_office"])),
        ):
            with self.subTest(label=label):
                with self.assertRaises(RuntimeError) as caught:
                    config.validate(values, FIXTURE)
                self.assertIn("办公目录", str(caught.exception))

    def test_6_the_directory_string_is_written_once_in_core(self):
        """随仓配置里的目录字符串,在 core/ 的 *.py、*.json(测试与数据目录除外)里只出现一处。"""
        shipped = existing.repository_file_or_skip(self, "desk_config.json")
        office = json.loads(shipped.read_text(encoding="utf-8-sig"))["办公目录"]
        self.assertTrue(office.strip())
        skipped = {"tests", "data", "demo-data", "__pycache__"}
        hits: list[str] = []
        for path in sorted(existing.ROOT.rglob("*")):
            if path.suffix not in {".py", ".json"} or not path.is_file():
                continue
            relative = path.relative_to(existing.ROOT)
            if skipped & set(relative.parts[:-1]):
                continue
            count = path.read_text(encoding="utf-8", errors="replace").count(office)
            hits += [relative.as_posix()] * count
        self.assertEqual(["desk_config.json"], hits)
