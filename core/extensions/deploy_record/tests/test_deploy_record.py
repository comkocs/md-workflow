"""扩展 deploy_record:上服记录。每条用例都**显式打开**本扩展,收尾再关掉,不影响核心用例。

前三条(test_11/12/13)原样来自核心用例 ReviewInParallelTests 的「上服与取证分离」一段;
其余几条钉「默认不加载、按配置加载」这件事本身。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

from tools.tickets import extension_loader, service as service_module
from tools.tickets.http_server import TicketHTTPServer, TicketRequestHandler
from tools.tickets.model import TicketError
from tools.tickets.service import TicketService
from tools.tickets.store import TicketStore

CORE = Path(__file__).resolve().parents[3]
CLI = [sys.executable, str(CORE / "tools" / "tickets" / "ticket.py")]
FIXTURE = CORE / "tools" / "tickets" / "tests" / "fixtures" / "desk_config.json"
SLOT = "前端·页面接线"
EXTENSION = "deploy_record"
# 只要这几个变量之一漏进测试子进程,CLI 就可能连上真服务器。
CHANNEL_VARIABLES = ("TICKET_REMOTE", "TICKET_TOKEN_FILE", "TICKET_CA_SHA256", "TICKET_ALLOW_STALE", "TICKET_ENV")


def clean_environment(tickets_root: Path, **extra: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in CHANNEL_VARIABLES}
    environment["TICKET_DESK_ROOT"] = str(tickets_root)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(extra)
    return environment


class DeployRecordTestCase(unittest.TestCase):
    def setUp(self) -> None:
        extension_loader.activate(EXTENSION)
        self.addCleanup(extension_loader.deactivate, EXTENSION)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = TicketService(TicketStore(self.root / "tickets"))
        self.worker = self.service.staff_new(SLOT, "sol")["员工名"]


class DeployRecordTests(DeployRecordTestCase):
    # ── 上服与取证分离(原核心用例 ReviewInParallelTests 的 test_11/12/13) ──
    def test_11_a_deploy_record_needs_no_window_no_judge_no_picture(self):
        record = self.service.deploy_record("abc1234de", "服务 active;8443 在听", ["T-000001", "T-000002"])
        self.assertEqual("上服记录", record["部署类"])
        self.assertEqual("实机复验过", record["状态"])       # 免判免复检,直接终态
        self.assertEqual(service_module.SHOT_EXEMPT, record["实机图标记"])   # 免图
        self.assertEqual("", record["指派给"])                # 免员工窗
        self.assertIn("T-000001", record["接线证据"]["文字"])
        self.assertIn("8443 在听", record["接线证据"]["原样输出"])
        # 部署头自动写进当前值面——以前这一格靠人手抄,抄漏全台面读到旧值
        self.assertEqual("abc1234de", self.service.state_board()["值"]["deploy_head_server"])

    def test_12_an_evidence_ticket_carries_the_picture_duty_instead(self):
        """取证单才是欠图的那一张;上服记录不欠。取不到图不挡上服。"""
        record = self.service.deploy_record("abc1234de", "8443 在听")
        evidence = self.service.evidence_ticket("abc1234de", SLOT)
        self.assertEqual("取证", evidence["部署类"])
        self.assertEqual("待独图", evidence["实机图标记"])
        self.assertFalse(evidence["非玩家可感知"])           # 取证要的就是屏上那一眼
        pending = [row["编号"] for row in self.service.list_tickets(shot_pending=True)]
        self.assertIn(evidence["编号"], pending)
        self.assertNotIn(record["编号"], pending, "上服记录不该占待独图那一格")

    def test_13_a_deploy_record_refuses_to_be_built_without_a_head(self):
        with self.assertRaisesRegex(TicketError, "必须写明部署头"):
            self.service.deploy_record("", "8443 在听")
        with self.assertRaisesRegex(TicketError, "server 或 engine"):
            self.service.deploy_record("abc1234de", "x", repo="别的仓")


class DeployRecordLoadingTests(DeployRecordTestCase):
    """默认不加载;打开之后命令行、服务端方法、HTTP op 三处一起出现,关掉一起消失。"""

    def test_default_config_does_not_enable_it(self):
        import tools.tickets.config as config

        self.assertNotIn(EXTENSION, config.ENABLED_EXTENSIONS, "测试夹具位表不许默认启用扩展")
        default = json.loads((CORE / "desk_config.json").read_text(encoding="utf-8-sig"))
        self.assertEqual([], default["启用扩展"], "随仓的默认配置不许启用任何扩展")

    def test_cli_parser_and_service_method_follow_activation(self):
        from tools.tickets.ticket import parser

        def subcommands() -> set[str]:
            action = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
            return set(action.choices)

        self.assertIn("deploy-record", subcommands())
        self.assertTrue(hasattr(TicketService, "deploy_record"))
        extension_loader.deactivate(EXTENSION)
        self.assertNotIn("deploy-record", subcommands())
        self.assertFalse(hasattr(TicketService, "deploy_record"))

    def test_the_http_op_exists_only_while_active(self):
        handler = partial(TicketRequestHandler, directory=str(CORE / "tools" / "browser"))
        server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(payload: dict) -> tuple[int, dict]:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            connection.request("POST", "/api/action", body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            connection.close()
            return response.status, data

        try:
            status, payload = post({"op": "deploy-record", "by": "部署脚本", "head": "abc1234de", "probes": "8443 在听"})
            self.assertEqual(200, status, payload)
            self.assertEqual("上服记录", payload["result"]["部署类"])
            extension_loader.deactivate(EXTENSION)
            status, payload = post({"op": "deploy-record", "by": "部署脚本", "head": "abc1234de"})
            self.assertEqual(400, status)
            self.assertIn("不认识的动作", payload["reason"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_a_config_that_enables_it_gives_the_cli_the_subcommand(self):
        config = json.loads(FIXTURE.read_text(encoding="utf-8"))
        config["启用扩展"] = [EXTENSION]
        enabled = self.root / "enabled.json"
        enabled.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        tickets = self.root / "cli-root"

        def run(config_path: Path, *arguments: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [*CLI, *arguments, "--local"], cwd=CORE,
                env=clean_environment(tickets, TICKET_DESK_CONFIG=str(config_path)),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )

        help_off = run(FIXTURE, "-h")
        self.assertEqual(0, help_off.returncode, help_off.stderr)
        self.assertNotIn("deploy-record", help_off.stdout)
        help_on = run(enabled, "-h")
        self.assertEqual(0, help_on.returncode, help_on.stderr)
        self.assertIn("deploy-record", help_on.stdout)
        done = run(enabled, "deploy-record", "--head", "abc1234de", "--probes", "8443 在听")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("上服记录已建", done.stdout)
        refused = run(FIXTURE, "deploy-record", "--head", "abc1234de")
        self.assertNotEqual(0, refused.returncode)


if __name__ == "__main__":
    unittest.main()
