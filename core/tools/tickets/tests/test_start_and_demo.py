"""core/start.py(一条命令起台)与 core/t.py($T 包装)的用例。

★两个脚本在 core/ 根上,上服包里没有它们(包树只有 tools/tickets、tools/browser、extensions、desk_config.json),
  所以整类先核脚本在不在:仓树上照跑,包树上用统一的人话理由干净跳过。
★演示数据用随仓的 core/desk_config.json 播——那就是演示要给人看的位表;不用测试夹具位表。
★起服一律 --port 0(系统给空闲端口),不占 8787/8788;数据一律落临时目录,不碰仓里的 data/ 与 demo-data/。
"""

from __future__ import annotations

import importlib.util
import json
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from .test_ticket_system import PACKAGE_TREE_SKIP_PREFIX, ROOT, clean_environment

START = ROOT / "start.py"
WRAPPER = ROOT / "t.py"
SHIPPED_CONFIG = ROOT / "desk_config.json"
URL_LINE = re.compile(r"工单台服务已启动：http://127\.0\.0\.1:(\d+)/")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def shipped_roles() -> tuple[str, str, str]:
    """从随仓配置里现读:总编 = 角色为总编排的位;两位总监 = 位表里前两个没有角色、也不是只发需求的位。"""
    rows = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))["位表"]
    conductor = next(row["名字"] for row in rows if row.get("角色") == "总编排")
    plain = [row["名字"] for row in rows if not row.get("角色") and not row.get("只发需求")]
    return conductor, plain[0], plain[1]


def desk_environment(data_root: Path | str, **extra: str) -> dict[str, str]:
    return clean_environment(data_root, TICKET_DESK_CONFIG=str(SHIPPED_CONFIG), **extra)


class RunningDesk:
    """把 start.py 当真起起来,等到它打出「工单台服务已启动」那一行为止。

    ★不设 PYTHONUNBUFFERED:那一行必须靠 start.py 自己逐行刷出来——
      Windows 上 Git Bash 走管道,不刷就一直憋着,用户以为没起来。
    """

    def __init__(self, *arguments: str, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(START), *arguments], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        self.lines: list[str] = []
        lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.monotonic() + 90
        while True:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                self.stop()
                raise AssertionError(f"start.py 90 秒内没打出起服那一行:\n{''.join(self.lines)}") from None
            if line is None:
                raise AssertionError(f"start.py 没起来就退出了(退出码 {self.process.wait()}):\n{''.join(self.lines)}")
            self.lines.append(line)
            found = URL_LINE.search(line)
            if found:
                self.port = int(found.group(1))
                return

    @property
    def output(self) -> str:
        return "".join(self.lines)

    def get(self, path: str) -> tuple[int, str, bytes]:
        with NO_PROXY.open(f"http://127.0.0.1:{self.port}{path}", timeout=15) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()

    def api(self, path: str):
        body = json.loads(self.get(path)[2].decode("utf-8"))
        return body.get("result", body)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=15)
        if self.process.stdout:
            self.process.stdout.close()


def run_script(script: Path, arguments: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *arguments], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


class StartScriptAndDemoTests(unittest.TestCase):
    """一条命令起演示台:播种三张不同状态的单、总编与两位总监在位表里;$T 读写同一份数据;演示与正式数据分开。"""

    @classmethod
    def setUpClass(cls) -> None:
        for script in (START, WRAPPER):
            if not script.is_file():
                raise unittest.SkipTest(
                    f"{PACKAGE_TREE_SKIP_PREFIX},这条用例要跑仓内的 {script.name},包树里没有它:{script}"
                )
        cls.temporary = tempfile.TemporaryDirectory()
        cls.demo_root = Path(cls.temporary.name) / "演示台"
        cls.env = desk_environment(cls.demo_root)
        cls.desk = RunningDesk("--demo", "--root", str(cls.demo_root), "--port", "0", env=cls.env)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.desk.stop()
        cls.temporary.cleanup()

    def test_1_demo_seeds_three_states_and_the_conductor_with_two_directors(self):
        status, content_type, page = self.desk.get("/")
        self.assertEqual(200, status)
        self.assertIn("text/html", content_type)
        self.assertIn("<title>工单台</title>", page.decode("utf-8"))

        tickets = self.desk.api("/api/tickets")
        self.assertEqual(["T-000001", "T-000002", "T-000003"], [ticket["编号"] for ticket in tickets])
        self.assertEqual({"新建", "已认领", "关闭"}, {ticket["状态"] for ticket in tickets})
        self.assertIn("先播种", self.desk.output)
        for ticket in tickets:
            self.assertIn(f"{ticket['编号']} · {ticket['标题']} · {ticket['状态']}", self.desk.output)

        conductor, first, second = shipped_roles()
        meta = self.desk.api("/api/slots")
        slot_names = [row["名字"] for row in meta["slots"]["总监位"]]
        for slot in (conductor, first, second):
            self.assertIn(slot, slot_names)
        self.assertEqual({first, second}, {ticket["所属总监位"] for ticket in tickets})
        crew = meta["staff"]["总监位"]
        self.assertEqual(2, len(crew[first]["员工"]))
        self.assertEqual(1, len(crew[second]["员工"]))
        # 演示员工登记的模型取随仓配置「主力模型集合」的第一个,且它就在「模型名册」里(不是名册外的占位名)。
        shipped = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
        default_model = shipped["主力模型集合"][0]
        self.assertIn(default_model, [row["模型"] for row in shipped["模型名册"]])
        tools = {member["工具/窗类型"] for slot in (first, second) for member in crew[slot]["员工"]}
        self.assertEqual({default_model}, tools)
        self.assertTrue(all(ticket["指派给"] for ticket in tickets))
        # 总编在已认领那一位的对话线上留过一句
        thread = self.desk.api(
            f"/api/inbox?slot={urllib.parse.quote(second)}&for={urllib.parse.quote('设计者')}&all=1"
        )
        self.assertTrue(any(row["发言人"] == conductor for row in thread))

    def test_2_a_second_start_on_a_busy_port_is_refused(self):
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "正式台"
            done = run_script(START, ["--root", str(target), "--port", str(self.desk.port)], desk_environment(target))
            self.assertEqual(2, done.returncode, done.stdout + done.stderr)
            self.assertIn(f"本机端口 {self.desk.port} 上已经有服务在听", done.stderr)
            self.assertFalse(target.exists(), "被拦下的那次不该建出数据目录")

    def test_3_the_wrapper_reads_and_writes_the_served_data_and_never_goes_remote(self):
        # 通道变量故意指向一个没人听的端口:$T 若跟着它走,就会连不上而报错。
        env = desk_environment(self.demo_root, TICKET_REMOTE="http://127.0.0.1:9", TICKET_TOKEN_FILE=str(self.demo_root / "无此令牌"))
        listed = run_script(WRAPPER, ["list"], env)
        self.assertEqual(0, listed.returncode, listed.stderr)
        for number in ("T-000001", "T-000002", "T-000003"):
            self.assertIn(number, listed.stdout)
        self.assertNotIn("远程", listed.stdout + listed.stderr)

        cursor = self.desk.api("/api/changes?since=999999999")["游标"]
        conductor, first, _ = shipped_roles()
        said = run_script(WRAPPER, ["say", "--slot", first, "--by", conductor, "来自 t.py 的一句"], env)
        self.assertEqual(0, said.returncode, said.stderr)
        edited = run_script(WRAPPER, ["set", "T-000001", "--body", "t.py 改的正文", "--by", first], env)
        self.assertEqual(0, edited.returncode, edited.stderr)

        # 服务在跑,$T 直写同一目录:网页取数的接口马上就能取到这两笔。
        changes = self.desk.api(f"/api/changes?since={cursor}")
        self.assertIn("T-000001", [ticket["编号"] for ticket in changes["工单"]])
        thread = self.desk.api(
            f"/api/inbox?slot={urllib.parse.quote(first)}&for={urllib.parse.quote('设计者')}&all=1"
        )
        self.assertIn("来自 t.py 的一句", [row["文字"] for row in thread])

        helped = run_script(WRAPPER, ["-h"], env)
        self.assertEqual(0, helped.returncode, helped.stderr)
        for command in ("new", "set", "claim", "submit", "settle", "live", "close", "list", "staff", "serve", "demo"):
            self.assertRegex(helped.stdout, rf"[{{,]{command}[,}}]")

    def test_4_demo_and_formal_data_live_apart_and_a_seeded_demo_is_not_reseeded(self):
        spec = importlib.util.spec_from_file_location("desk_start_under_test", START)
        start = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(start)
        from tools.tickets.store import DEFAULT_ROOT

        self.assertEqual(ROOT / "data", start.data_root(False, environ={}))
        self.assertEqual(DEFAULT_ROOT, start.data_root(False, environ={}))
        self.assertEqual(ROOT / "demo-data", start.data_root(True, environ={}))
        # 演示台不跟 TICKET_DESK_ROOT 走:设了它也不会把演示数据播进正式目录。
        self.assertEqual(ROOT / "demo-data", start.data_root(True, environ={"TICKET_DESK_ROOT": str(ROOT / "data")}))
        self.assertNotEqual(start.data_root(False, environ={}), start.data_root(True, environ={}))
        for directory in ("data", "demo-data"):
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", f"{directory}/probe.json"], cwd=ROOT, capture_output=True,
            )
            self.assertEqual(0, ignored.returncode, f"{directory}/ 没被 git 忽略")

        # t.py --demo 与 start.py --demo 指向同一处(env 子命令只报路径,不建目录)
        reported = run_script(WRAPPER, ["--demo", "env"], desk_environment(ROOT / "data"))
        self.assertEqual(0, reported.returncode, reported.stderr)
        self.assertIn((ROOT / "demo-data").as_posix(), reported.stdout.replace("\\", "/"))

        # 已播过的演示目录再起一次:不再播种,还是那三张单。
        again = RunningDesk("--demo", "--root", str(self.demo_root), "--port", "0", env=self.env)
        try:
            self.assertNotIn("先播种", again.output)
            self.assertEqual(3, len(again.api("/api/tickets")))
        finally:
            again.stop()


if __name__ == "__main__":
    unittest.main()
