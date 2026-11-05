"""需求-032:缺配置、未标模型、旧平台和值面存量兼容。"""

import json

import pytest

from tools.tickets import config
from . import test_ticket_system as existing


def test_missing_office_uses_default_and_warns(tmp_path, capsys):
    values = json.loads(config.PATH.read_text(encoding="utf-8-sig"))
    values.pop("办公目录")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    loaded = config.load_config(path)
    expected = (config.CORE_ROOT.parent / "_office").as_posix()
    assert loaded["办公目录"] == expected
    warning = capsys.readouterr().err
    assert len(warning.splitlines()) == 1
    assert "办公目录" in warning and "默认" in warning and expected in warning
    values["办公目录"] = ""
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(RuntimeError, match="办公目录"):
        config.load_config(path)


class ToolsLiteralFixTests(existing.TicketTestCase):
    def test_no_platform_or_actual_model_still_counts_third_rework(self):
        self.worker = self.service.staff_new(existing.SLOT)["员工名"]
        for index in range(3):
            ticket = self.to_judging()
            self.assertFalse(ticket.get("实际模型"))
            _, notice = self.service.judge(ticket["编号"], False, "UI总监", "执行结果不符",
                                           existing.REWORK_VERDICT, "模型")
        scores = self.service.store.load_staff()["模型记分"]
        self.assertEqual(3, scores["未标平台-未标"][existing.SLOT])
        self.assertNotIn("", scores)
        self.assertIn("累计判退 3 次", notice)
        self.assertIn("已到停用线", notice)
        rows = self.service.store.read_jsonl(self.service.store.thread_path(existing.service_module.CONDUCTOR_SLOT))
        self.assertTrue(any("已到停用线" in row["文字"] for row in rows))

    def test_registration_without_platform_preserves_legacy_platform(self):
        self.worker = self.service.staff_new(existing.SLOT, "claude")["员工名"]
        ticket = self.dispatch()
        registered, _ = self.service.open_window(ticket["编号"], "设计者", "new-model")
        member = self.service.find_staff(self.worker)[1]
        self.assertEqual("claude", self.service.staff_platform(member))
        self.assertEqual("claude", member["平台"])
        self.assertEqual("claude", registered["已开窗"]["实际平台"])
        self.assertEqual("new-model", member["工具/窗类型"])

    def test_registration_without_platform_preserves_explicit_platform(self):
        ticket = self.dispatch()
        registered, _ = self.service.open_window(ticket["编号"], "设计者", "new-model")
        self.assertEqual("codex", self.service.find_staff(self.worker)[1]["平台"])
        self.assertEqual("codex", registered["已开窗"]["实际平台"])

    def test_legacy_state_keys_are_tolerated_without_rewriting_database(self):
        # 用分段构造历史字段,避免旧产品键名继续作为出厂字面。
        old = {
            "judging_" + "resolution": {"值": "1280x720"},
            "deploy_head_" + "engine": {"值": "abcdef0"},
            "walk_run_" + "speed": {"值": {"走": 1.6, "跑": 3.2}},
        }
        self.service.store.atomic_json(self.service.store.state_path, old)
        result = existing.run_local_cli(["state", "get"], self.service.store.root)
        self.assertEqual(0, result.returncode, result.stderr)
        values = json.loads(result.stdout)["值"]
        self.assertIn("pending_shared_tools", values)
        self.assertTrue(set(old).isdisjoint(values))
        self.assertEqual(old, self.service.store.read_json(self.service.store.state_path))
