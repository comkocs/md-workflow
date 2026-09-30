from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import http.client
import http.server
import io
import inspect
import ipaddress
import json
import os
import random
import socket
import sqlite3
import re
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from PIL import Image, ImageFilter

from tools.tickets import http_server as http_server_module, model, service as service_module, store as store_module
from tools.tickets.model import TicketError
from tools.tickets.auth import AccountManager
from tools.tickets.http_server import TicketHTTPServer, TicketRequestHandler
from tools.tickets.service import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, TicketService
from tools.tickets.store import SqliteStore, TicketStore
from tools.tickets.ticket import parser as cli_parser
from tools.tickets.remote import RemoteClient
from tools.tickets import channel as channel_config


ROOT = Path(__file__).resolve().parents[3]
CLI = [sys.executable, str(ROOT / "tools" / "tickets" / "ticket.py")]
SLOT = "前端·页面接线"
OTHER_SLOT = "后端·服务"
VALID_DECISION_BODY = "一、这是什么\n需要决定界面配色。\n二、选了会怎样\n会统一后续视觉实现。\n三、推荐\n推荐暖色方案。"
PASS_VERDICT = "玩家怎么打开它：双击启动工单台后进入对应卡片。功能与验收均通过。"
REWORK_VERDICT = "模型责任：功能未达到工单验收要求，按返工原因修正后重交。"
QUESTION_REWORK_VERDICT = "出题责任：任务书或判据本身写错，执行方照做无误。"
# 任务书路径的用例钉的是「绝对路径 + {ticket} 占位符能一路活到 show」，不是某个盘符。
# 提交端会走 Path(value).expanduser().resolve()：POSIX 上反斜杠不是分隔符，
# Path(r"D:\a\b.md") 整串只是一个相对文件名，resolve() 会把它接到 cwd 后面，
# 于是写死 D: 的断言只在 Windows 上成立——服务器上就是这么红的。
# 按平台各取一条本平台真绝对的路径，两边都照常跑，不是跳过。
TASKBOOK_DIRECTORY = (
    r"D:\project\_office\平台·工单台\任务书" if os.name == "nt"
    else "/srv/project/_office/平台·工单台/任务书"
)

# 只要这几个变量之一漏进测试子进程,CLI 就可能连上真服务器(方向二:六张假单写进生产库)。
CHANNEL_VARIABLES = ("TICKET_REMOTE", "TICKET_TOKEN_FILE", "TICKET_CA_SHA256", "TICKET_ALLOW_STALE", "TICKET_ENV")
REMOTE_GUARD_MESSAGE = "测试不得连真服务器,请先 unset TICKET_REMOTE"


def clean_environment(tickets_root: Path | str, **extra: str) -> dict[str, str]:
    """测试子进程唯一允许的环境来源:从当前环境派生,但通道变量一律清空,本机库指到用例自己的临时目录。

    需要远程模式的用例,把 TICKET_REMOTE 等作为 extra 显式传进来——那只能是用例自己起的 127.0.0.1 服务。
    """
    environment = {key: value for key, value in os.environ.items() if key not in CHANNEL_VARIABLES}
    environment["TICKET_DESK_ROOT"] = str(tickets_root)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(extra)
    return environment


def run_local_cli(arguments: list[str], tickets_root: Path | str, **extra: str) -> subprocess.CompletedProcess:
    """本机模式跑一次 ticket.py:干净环境 + 显式 --local,双保险。

    errors="replace":子进程 stderr 现在是守门用例的失败消息,
    真出事那次不许因为一个解码不了的字节把整条原因吃掉。
    """
    return subprocess.run(
        [*CLI, *arguments, "--local"], cwd=ROOT, env=clean_environment(tickets_root, **extra),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def created_ticket_id(result: subprocess.CompletedProcess) -> str:
    return result.stdout.splitlines()[0]


# 上服包(扩展 server_deploy 的 update.sh 的 --source)里核心代码只有 tools/tickets 与 tools/browser 两个目录:
# 闸② 就是在那棵包树里跑这套用例的。读 tools/ 以外文件的用例在包树里根本没有前提,
# 从前它们直接红,把每一次上服都拦死。这里给它们一个统一的、写清人话理由的干净跳过。
# ★这不是放水:包树里跳、仓树里照跑照有效,由 PackageTreeGateTests 两条一起钉住。
PACKAGE_TREE_SKIP_PREFIX = "上服包只含 tools/tickets 与 tools/browser 两个目录"


def repository_file_or_skip(test: unittest.TestCase, *parts: str) -> Path:
    """要读 tools/ 以外的仓内文件时走这里:仓树上返回真路径,包树上干净跳过。"""
    path = ROOT.joinpath(*parts)
    if not path.is_file():
        test.skipTest(
            f"{PACKAGE_TREE_SKIP_PREFIX},这条用例要读仓内的 {'/'.join(parts)},包树里没有它:{path}"
        )
    return path


class TicketTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = TicketService(TicketStore(self.root / "tickets"))
        self.worker = self.service.staff_new(SLOT, "sol")["员工名"]
        self.deliverable = self.root / "deliverable.txt"
        self.deliverable.write_text("产物\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def dispatch(self, title: str = "测试派单", assign: str | None = None):
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "主界面/面板根", self.worker if assign is None else assign,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
        )

    def picture(self, name: str = "world.png", size: tuple[int, int] = (1600, 900), mode: str = "RGB") -> Path:
        path = self.root / name
        color = (55, 90, 125, 180) if mode == "RGBA" else (55, 90, 125)
        Image.new(mode, size, color).save(path)
        return path

    def to_judging(self):
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        return self.service.submit(ticket["编号"], "登录后界面已出现")

    def verified(self, ticket_id: str, actor: str = "独立复检"):
        """把复验这一道记成「过」。

        并线前置从 2026-09-08 起是「判过 ∧ 复验过」两道齐,所以凡是**造数据到已合并**
        的用例都要先走这一步。写成夹具而不是在每处内联,是为了让
        「这一步是造数据、不是被测行为」一眼看得出来——
        真正钉并线前置本身的是 ReviewInParallelTests,那里逐条显式调。
        """
        return self.service.verify(ticket_id, actor, "过", gates="夹具:六项闸摘要")[0]

    def merged_ticket(self, ticket_id: str, actor: str = "独立复检", verifier: str = "独立复检"):
        """造数据用:补上复验再并线。被测的是 merge 之后的事,不是这两道闸本身。"""
        self.verified(ticket_id, verifier)
        return self.service.merge(ticket_id, actor)

    def keep_window_open(self) -> None:
        """让 self.worker 回到在岗。

        非固定工位到终态会自动收窗，可这些用例是「一个窗连着做好几张单」的老写法：
        第一张 live 过之后窗就收了，第二张 create_dispatch 立刻被 require_active_staff 拦下。
        它们要钉的是同图/独图分类、批量 live、免独图——不该被名册规则牵着走，
        所以造数据时把窗按回在岗；**规则本身**由 AutoRetireAtTerminalTests 十条专门钉。
        """
        if self.service.find_staff(self.worker)[1]["状态"] != "在岗":
            self.service.staff_reopen(self.worker)

    def to_merged(self, title: str = "待实机复验", internal: bool = False):
        self.keep_window_open()
        ticket = self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台" if internal else "主界面/面板根", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=internal,
        )
        self.service.claim(ticket["编号"], self.worker)
        if internal:
            ticket = self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        else:
            self.service.attach(ticket["编号"], str(self.picture(f"{ticket['编号']}-before.png")), "world", self.worker)
            ticket = self.service.submit(ticket["编号"], "登录后界面已出现")
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        return self.merged_ticket(ticket["编号"], "独立复检")


class JudgeBlameTests(TicketTestCase):
    def test_r1_1_rework_without_blame_is_rejected_with_plain_guidance(self):
        """--blame 缺省时先照判语首行推导(网页判退不带 --blame);首行也没标才拒。"""
        ticket = self.to_judging()
        # 首行有「模型责任」:推导成 blame=模型,放行
        derived = run_local_cli([
            "judge", ticket["编号"], "--rework", "入口仍会闪一下", "--by", "UI总监",
            "--verdict", REWORK_VERDICT,
        ], self.service.store.root)
        self.assertEqual(0, derived.returncode, derived.stderr)
        self.assertEqual("模型", self.service.store.load_ticket(ticket["编号"])["返工原因列表"][-1]["责任"])
        # 首行什么都没标:拒,并给人话
        blank = self.to_judging()
        result = run_local_cli([
            "judge", blank["编号"], "--rework", "入口仍会闪一下", "--by", "UI总监",
            "--verdict", "功能未达到工单验收要求，按返工原因修正后重交。",
        ], self.service.store.root)
        self.assertEqual(2, result.returncode)
        self.assertIn(
            "判退必须写清责任归属:--blame 模型(执行方做错) 或 --blame 出题(任务书/判据本身写错)。",
            result.stderr,
        )
        self.assertIn("出题责任不计入模型判退累计,但会记进该总监的出题账。", result.stderr)

    def test_r1_2_pass_with_blame_is_rejected(self):
        ticket = self.to_judging()
        result = run_local_cli([
            "judge", ticket["编号"], "--pass", "--blame", "模型", "--by", "UI总监",
            "--verdict", PASS_VERDICT,
        ], self.service.store.root)
        self.assertEqual(2, result.returncode)
        self.assertIn("判过不需要责任归属", result.stderr)

    def test_r1_3_question_blame_does_not_add_any_model_score(self):
        ticket = self.to_judging()
        before = json.loads(json.dumps(self.service.store.load_staff()["模型记分"], ensure_ascii=False))
        self.service.judge(
            ticket["编号"], False, "UI总监", "任务书把入口写错了", QUESTION_REWORK_VERDICT, "出题",
        )
        after = self.service.store.load_staff()["模型记分"]
        self.assertEqual(before, after)
        self.assertNotIn("sol", after)

    def test_r1_4_model_blame_adds_the_existing_model_score(self):
        ticket = self.to_judging()
        self.service.judge(ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型")
        score = self.service.store.load_staff()["模型记分"]["sol-未标"]
        self.assertEqual((1, 1), (score[SLOT], score["合计"]))

    def test_r1_5_verdict_first_line_must_match_blame(self):
        ticket = self.to_judging()
        with self.assertRaises(TicketError) as caught:
            self.service.judge(ticket["编号"], False, "UI总监", "执行结果不符", QUESTION_REWORK_VERDICT, "模型")
        message = str(caught.exception)
        self.assertIn('--blame 写的是“模型”', message)
        self.assertIn('判语首行写的是“出题责任”', message)
        self.assertEqual("待判", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_r1_6_blame_is_saved_on_ticket_reason_and_event_for_both_values(self):
        for index, (blame, verdict) in enumerate((("模型", REWORK_VERDICT), ("出题", QUESTION_REWORK_VERDICT)), 1):
            with self.subTest(blame=blame):
                ticket = self.dispatch(f"责任字段 {index}")
                self.service.claim(ticket["编号"], self.worker)
                self.service.attach(ticket["编号"], str(self.picture(f"blame-{index}.png")), "world", self.worker)
                self.service.submit(ticket["编号"], "登录后仍有问题")
                result = run_local_cli([
                    "judge", ticket["编号"], "--rework", "按责任返工", "--blame", blame,
                    "--by", "UI总监", "--verdict", verdict,
                ], self.service.store.root)
                self.assertEqual(0, result.returncode, result.stderr)
                judged = self.service.store.load_ticket(ticket["编号"])
                self.assertEqual(blame, judged["判退责任"])
                self.assertEqual(blame, judged["返工原因列表"][-1]["判退责任"])
                event = self.service.store.read_jsonl(self.service.store.log_path)[-1]
                self.assertEqual(blame, event["判退责任"])
                self.assertIn(f"判退责任：{blame}", event["说明"])


class DemandAnswerPermissionTests(TicketTestCase):
    """需求单答复权交给所属总监位，外加三种前缀闸。

    以前需求只有总编能答：接收位把活干完了也答不动，只能另建一张疑问单回话，
    原单永远挂在「待答」——设计者队列上看着是总编卡了 30 小时，其实活早做完了
。总工单的口径一个字没动，那一条由
    BlockedAnswerPermissionTests.test_general_ticket_still_refuses_the_owner_slot 钉着。
    """

    def demand(self, title: str = "要一套新图标", slot: str = SLOT):
        return self.service.create_question("需求", slot, title, "请排一下期。")

    def test_r1_1_owner_slot_can_answer_with_accepted_prefix(self):
        ticket = self.service.answer(self.demand()["编号"], "受理，本周内给排期。", SLOT)
        self.assertEqual("已答", ticket["状态"])
        self.assertEqual("受理，本周内给排期。", ticket["答复"])
        # 答完还要关得掉：答复权交出去了、关闭权还捏在总编手里的话，单子照样落不了地。
        self.assertEqual("关闭", self.service.close(ticket["编号"], SLOT)["状态"])

    def test_r1_2_other_slot_is_refused_and_the_error_names_the_owner(self):
        ticket = self.demand()
        with self.assertRaises(TicketError) as caught:
            self.service.answer(ticket["编号"], "受理，我来排。", OTHER_SLOT)
        message = str(caught.exception)
        for expected in (ticket["编号"], SLOT, OTHER_SLOT):
            self.assertIn(expected, message)
        self.assertEqual("待答", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_r1_3_orchestrator_can_still_answer(self):
        ticket = self.service.answer(self.demand()["编号"], "已排期→T-000123", "总编")
        self.assertEqual("已答", ticket["状态"])

    def test_r1_4_bad_first_word_is_refused_and_lists_all_three_forms(self):
        ticket = self.demand()
        result = run_local_cli(
            ["answer", ticket["编号"], "好的，知道了。", "--by", SLOT], self.service.store.root,
        )
        self.assertEqual(2, result.returncode)
        for form in ("受理(后面可跟预计)", "已排期→T-xxxxxx(派单号)", "已完成→T-xxxxxx(交板单号)"):
            self.assertIn(form, result.stderr)
        self.assertEqual("待答", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_r1_5_malformed_ticket_id_after_the_arrow_is_refused(self):
        ticket = self.demand()
        with self.assertRaises(TicketError) as caught:
            self.service.answer(ticket["编号"], "已排期→T-12", SLOT)
        self.assertIn("T- 加六位数字的单号", str(caught.exception))
        self.assertEqual("待答", self.service.store.load_ticket(ticket["编号"])["状态"])
        # 形状对的同一句照样过，证明拒的是单号形状不是箭头本身。
        self.assertEqual("已答", self.service.answer(ticket["编号"], "已完成→T-000077 已交板", SLOT)["状态"])


class BlameCorrectionTests(TicketTestCase):
    """判卷人自纠判退归属 set --blame。

    judge 只认「待判」态，一判完就再也进不去，而模型判退是会累计到停用线的硬账：
    2026-09-06 美术·视觉五 在「再判退 1 次就到停用线」的提示下回头复核，
    发现自己把出题责任记成了模型责任，却没有任何路径改回来。
    """

    def reworked(self):
        ticket = self.to_judging()
        ticket, _ = self.service.judge(
            ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型",
        )
        return ticket

    def test_r2_6_model_to_question_moves_the_field_and_both_ledgers(self):
        ticket = self.reworked()
        self.assertEqual({SLOT: 1, "合计": 1}, self.service.store.load_staff()["模型记分"]["sol-未标"])
        result = run_local_cli([
            "set", ticket["编号"], "--blame", "出题",
            "--reason", "复核后确认是任务书判据写错，执行方照做无误", "--by", SLOT,
        ], self.service.store.root)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("已改判退责任：模型 → 出题", result.stdout)
        changed = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("出题", changed["判退责任"])
        self.assertEqual("出题", changed["返工原因列表"][-1]["判退责任"])
        self.assertEqual("出题", changed["返工原因列表"][-1]["责任"])
        staff = self.service.store.load_staff()
        self.assertEqual({SLOT: 0, "合计": 0}, staff["模型记分"]["sol-未标"])
        self.assertEqual({"合计": 1, SLOT: 1}, staff["出题记分"][SLOT])
        event = self.service.store.read_jsonl(self.service.store.log_path)[-1]
        self.assertEqual(("set-blame", "模型", "出题"), (event["事件"], event["旧值"], event["新值"]))
        self.assertEqual("复核后确认是任务书判据写错，执行方照做无误", event["理由"])

    def test_r2_7_judging_state_is_refused_and_points_back_at_judge(self):
        ticket = self.to_judging()
        with self.assertRaises(TicketError) as caught:
            self.service.set_blame(ticket["编号"], "出题", "记反了", SLOT)
        message = str(caught.exception)
        self.assertIn("现在是「待判」", message)
        self.assertIn("judge --blame", message)
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["判退责任"])

    def test_r2_8_other_slot_is_refused(self):
        ticket = self.reworked()
        with self.assertRaises(TicketError) as caught:
            self.service.set_blame(ticket["编号"], "出题", "我替他改一下", OTHER_SLOT)
        message = str(caught.exception)
        self.assertIn(OTHER_SLOT, message)
        self.assertIn(SLOT, message)
        self.assertEqual("模型", self.service.store.load_ticket(ticket["编号"])["判退责任"])
        self.assertEqual(1, self.service.store.load_staff()["模型记分"]["sol-未标"]["合计"])

    def test_r2_9_empty_reason_is_refused(self):
        ticket = self.reworked()
        result = run_local_cli(
            ["set", ticket["编号"], "--blame", "出题", "--by", SLOT], self.service.store.root,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--reason 不能为空", result.stderr)
        self.assertEqual("模型", self.service.store.load_ticket(ticket["编号"])["判退责任"])


class ModelAccountingTests(TicketTestCase):
    def test_r2_1_model_spelling_variants_normalize_to_one_key(self):
        values = [service_module.normalize_model_name(value) for value in ("Sol  High", "sol_high", "sol high")]
        self.assertEqual(["sol-high", "sol-high", "sol-high"], values)

    def test_r2_2_three_models_stay_separate_through_real_judgements(self):
        for index, actual_model in enumerate(("sol", "sol-high", "sol-xhigh"), 1):
            ticket = self.dispatch(f"模型分开 {actual_model}")
            self.service.open_window(ticket["编号"], "设计者", actual_model)
            self.service.claim(ticket["编号"], self.worker)
            self.service.attach(ticket["编号"], str(self.picture(f"separate-{index}.png")), "world", self.worker)
            self.service.submit(ticket["编号"], "登录后仍有问题")
            self.service.judge(ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型")
        scores = self.service.store.load_staff()["模型记分"]
        self.assertEqual(1, scores["sol"]["合计"])
        self.assertEqual(1, scores["sol-high"]["合计"])
        self.assertEqual(1, scores["sol-xhigh"]["合计"])

    def test_r2_3_missing_actual_model_uses_a_separate_unmarked_key(self):
        ticket = self.to_judging()
        _, notice = self.service.judge(
            ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型",
        )
        scores = self.service.store.load_staff()["模型记分"]
        self.assertEqual(1, scores["sol-未标"]["合计"])
        self.assertNotIn("sol", scores)
        self.assertIn("这张单没填实际模型，已按 sol-未标 单独记账", notice)

    def test_r2_4_empty_roster_warns_but_does_not_block_rework(self):
        slots = self.service.store.read_json(self.service.store.slots_path)
        slots["模型名册"] = []
        self.service.store.atomic_json(self.service.store.slots_path, slots)
        ticket = self.dispatch("空名册照常判退")
        self.service.open_window(ticket["编号"], "设计者", "Mystery__Ultra")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("empty-roster.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "登录后仍有问题")
        judged, notice = self.service.judge(
            ticket["编号"], False, "UI总监", "执行结果不符", REWORK_VERDICT, "模型",
        )
        self.assertEqual("返工", judged["状态"])
        self.assertEqual(1, self.service.store.load_staff()["模型记分"]["mystery-ultra"]["合计"])
        self.assertIn("模型名 mystery-ultra 不在名册里，已按字面记账", notice)

    def test_r2_5_digest_splits_the_same_model_by_task_tier(self):
        for index, task_tier in enumerate(("甲", "乙"), 1):
            ticket = self.service.create_dispatch(
                SLOT, f"同模型不同档 {task_tier}", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
                task_tier=task_tier, deliverables=[str(self.deliverable)], internal=False,
            )
            self.service.open_window(ticket["编号"], "设计者", "Sol High")
            self.service.claim(ticket["编号"], self.worker)
            self.service.attach(ticket["编号"], str(self.picture(f"tier-{index}.png")), "world", self.worker)
            self.service.submit(ticket["编号"], "按任务档统计")
            self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        rows = [row for row in self.service.model_statistics() if row["模型"] == "sol-high"]
        self.assertEqual(["乙", "甲"], sorted(row["任务档"] for row in rows))
        digest = self.service.digest()
        self.assertIn("各模型合格率：模型 | 任务档 | 交板 | 判过 | 判退 | 合格率 | 状态", digest)
        self.assertTrue(any(line.startswith("[模型] sol-high | 甲 |") for line in digest))
        self.assertTrue(any(line.startswith("[模型] sol-high | 乙 |") for line in digest))


class QuestionScoreAndBanNoticeTests(TicketTestCase):
    def _judge_model_rework(
        self, index: int, slot: str = SLOT, worker: str | None = None, actual_model: str = "sol",
    ) -> tuple[dict, str]:
        worker = worker or self.worker
        ticket = self.service.create_dispatch(
            slot, f"停用计数 {slot} {index}", ["DECISIONS.md:测试"], "主界面/面板根", worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
        )
        self.service.open_window(ticket["编号"], "设计者", actual_model)
        self.service.claim(ticket["编号"], worker)
        self.service.attach(ticket["编号"], str(self.picture(f"ban-{slot[-2:]}-{index}.png")), "world", worker)
        self.service.submit(ticket["编号"], "登录后仍有问题")
        return self.service.judge(
            ticket["编号"], False, "UI总监", f"第 {index} 次执行错误", REWORK_VERDICT, "模型",
        )

    def test_r3_1_question_blame_adds_owner_score_without_model_score(self):
        ticket = self.to_judging()
        before = json.loads(json.dumps(self.service.store.load_staff()["模型记分"], ensure_ascii=False))
        self.service.judge(
            ticket["编号"], False, "UI总监", "任务书把入口写错了", QUESTION_REWORK_VERDICT, "出题",
        )
        staff = self.service.store.load_staff()
        self.assertEqual(before, staff["模型记分"])
        self.assertEqual({"合计": 1, SLOT: 1}, staff["出题记分"][SLOT])

    def test_r3_2_digest_contains_director_question_score(self):
        ticket = self.to_judging()
        self.service.judge(
            ticket["编号"], False, "UI总监", "任务书把入口写错了", QUESTION_REWORK_VERDICT, "出题",
        )
        digest = self.service.digest()
        self.assertIn("总监出题账:总监位 | 出题判退次数", digest)
        self.assertIn(f"[出题] {SLOT} | 1", digest)

    def test_r3_3_threshold_minus_one_really_notifies_conductor_thread(self):
        # 用非主力模型 glm-5.3:R1.5 之后主力模型(sol/opus/fable)到线只作质量提示,不再有「停用线」措辞。
        self._judge_model_rework(1, actual_model="glm-5.3")
        self._judge_model_rework(2, actual_model="glm-5.3")
        rows = self.service.store.read_jsonl(self.service.store.thread_path(service_module.CONDUCTOR_SLOT))
        texts = [row["文字"] for row in rows]
        self.assertTrue(any("累计判退 2 次" in x and "再判退 1 次就到停用线" in x for x in texts), texts)

    def test_r3_4_reaching_threshold_notifies_conductor_but_writes_no_ban(self):
        """ 总编答「乙」:到停用线只通知,不自动停用(2026-09-05 sol 两次误停后定的)。

        用非主力模型 glm-5.3 钉「原样措辞」:主力模型的到线措辞在 R1.5 改成了质量提示,
        由 BanLineCountTests 单独钉。
        """
        for index in range(1, 4):
            self._judge_model_rework(index, actual_model="glm-5.3")
        rows = self.service.store.read_jsonl(self.service.store.thread_path(service_module.CONDUCTOR_SLOT))
        text = next(row["文字"] for row in rows if "已到停用线" in row["文字"])
        self.assertIn("模型 glm-5.3", text)
        self.assertIn("累计判退 3 次", text)
        self.assertIn("自动停用已关", text)
        bans = self.service.store.load_staff().get("模型停用", {})
        self.assertNotIn("glm-5.3", bans.get("按位", {}).get(SLOT, []))

    def test_r3_5_thresholds_unchanged_but_neither_local_nor_global_auto_ban_is_written(self):
        other_worker = self.service.staff_new(OTHER_SLOT, "sol")["员工名"]
        for index in range(1, 4):
            self._judge_model_rework(index)
        for index in range(4, 6):
            self._judge_model_rework(index, OTHER_SLOT, other_worker)
        slots = self.service.store.read_json(self.service.store.slots_path)
        staff = self.service.store.load_staff()
        self.assertEqual({"同位": 3, "全项目": 5}, slots["停用阈值"])
        self.assertEqual(5, staff["模型记分"]["sol"]["合计"])
        self.assertNotIn("sol", staff["模型停用"]["按位"][SLOT])
        self.assertNotIn("sol", staff["模型停用"]["全项目"])


class BanLineCountTests(TicketTestCase):
    """停用线只数「判退责任=模型」的条目,按模型基名+任务档归并。

    2026-09-05 工单台把「模型 opus(乙档)跨位累计 5 次,已到停用线」报给了总编:
    逐张核 315 张派单后,opus 且有返工的 9 张里判退责任=模型的只有 3 张,两张明写出题、
    一张返工次数为 0——旧实现拿「模型记分」账本的累计直接对阈值,把出题责任也数了进去。
    现在账本照旧记(给合格率与 digest 用),停用线单独从工单现算。
    """

    def _rework(
        self, index: int, slot: str = SLOT, worker: str | None = None,
        actual_model: str = "glm-5.3", task_tier: str = "乙", blame: str = "模型",
    ) -> tuple[dict, str]:
        worker = worker or self.worker
        ticket = self.service.create_dispatch(
            slot, f"停用线归并 {slot} {index}", ["DECISIONS.md:测试"], "主界面/面板根", worker,
            task_tier=task_tier, deliverables=[str(self.deliverable)], internal=False,
        )
        self.service.open_window(ticket["编号"], "设计者", actual_model)
        self.service.claim(ticket["编号"], worker)
        self.service.attach(ticket["编号"], str(self.picture(f"banline-{index}.png")), "world", worker)
        self.service.submit(ticket["编号"], "登录后仍有问题")
        verdict = REWORK_VERDICT if blame == "模型" else QUESTION_REWORK_VERDICT
        return self.service.judge(ticket["编号"], False, "UI总监", f"第 {index} 次执行错误", verdict, blame)

    def test_r1_1_ban_line_counts_only_model_blame(self):
        """三张判退责任=模型 + 两张=出题:停用线的数是 3,不是 5。"""
        model_ids = []
        for index in (1, 2):
            ticket, _ = self._rework(index)
            model_ids.append(ticket["编号"])
        question_ids = []
        for index in (3, 4):
            ticket, _ = self._rework(index, blame="出题")
            question_ids.append(ticket["编号"])
        third, warning = self._rework(5)
        self.assertIn("累计判退 3 次", warning, warning)
        self.assertIn("已到停用线", warning)
        for ticket_id in model_ids:
            self.assertIn(ticket_id, warning)
        for ticket_id in question_ids:
            self.assertNotIn(ticket_id, warning)

    def test_r1_2_blank_blame_entry_is_not_counted(self):
        """返工原因列表里责任字段为空的条目(旧库迁移常见)不计入停用线。"""
        first, _ = self._rework(1)
        stored = self.service.store.load_ticket(first["编号"])
        stored.setdefault("返工原因列表", []).append({
            "时间": "2026-09-05 00:00:00", "判卷人": "UI总监", "原因": "旧库迁移来的条目,没有责任字段",
        })
        self.service.store.save_ticket(stored, "set", "测试", "补一条无责任字段的旧条目")
        _, warning = self._rework(2)
        self.assertIn("累计判退 2 次", warning, warning)
        self.assertIn("再判退 1 次就到停用线", warning)
        self.assertNotIn("已到停用线", warning)

    def test_r1_3_variant_models_merge_into_one_base_cell(self):
        """opus 与 opus-high 两张同档单归并成同一个基名:数是 2。"""
        self._rework(1, actual_model="opus")
        _, warning = self._rework(2, actual_model="opus-high")
        self.assertIn("模型 opus(乙档)", warning, warning)
        self.assertIn("记模型责任判退 2 次", warning, warning)

    def test_r1_4_task_tiers_never_merge(self):
        """甲档一张 + 乙档一张各算各的:乙档第二张时是 2(差一到线),不是 3(已到线)。。"""
        self._rework(1, task_tier="甲")
        self._rework(2)
        _, warning = self._rework(3)
        self.assertIn("累计判退 2 次", warning, warning)
        self.assertIn("再判退 1 次就到停用线", warning)
        self.assertNotIn("已到停用线", warning)

    def test_r1_5_notice_lists_every_counted_entry(self):
        """到线时通知正文列出计入的每一笔:单号 · 所属位 · 任务档 · 责任字段。"""
        ids = []
        for index in (1, 2, 3):
            ticket, _ = self._rework(index)
            ids.append(ticket["编号"])
        rows = self.service.store.read_jsonl(self.service.store.thread_path(service_module.CONDUCTOR_SLOT))
        text = next(row["文字"] for row in rows if "已到停用线" in row["文字"])
        self.assertIn("计入的每一笔(单号 · 所属位 · 任务档 · 责任字段):", text)
        for ticket_id in ids:
            self.assertIn(f"{ticket_id} · {SLOT} · 乙 · 模型", text)

    def test_r1_6_ledger_untouched_by_ban_line_recount(self):
        """同一场景跑完,模型记分与改动前的记账口径一致:出题责任进不了模型账。"""
        for index, blame in ((1, "模型"), (2, "出题"), (3, "出题"), (4, "模型"), (5, "模型")):
            self._rework(index, blame=blame)
        staff = self.service.store.load_staff()
        self.assertEqual({SLOT: 3, "合计": 3}, staff["模型记分"]["glm-5.3"])
        self.assertEqual({"合计": 2, SLOT: 2}, staff["出题记分"][SLOT])
        bans = staff["模型停用"]
        self.assertEqual([], bans["全项目"])
        self.assertFalse(any(bans["按位"].values()))

    def test_r2_1_main_model_never_says_ban_line(self):
        """R1.5:主力模型到线只作质量提示,不再出现「已到停用线」这类吓人的措辞。"""
        ids = []
        for index in (1, 2, 3):
            ticket, warning = self._rework(index, actual_model="opus")
            ids.append(ticket["编号"])
        self.assertIn("记模型责任判退 3 次", warning, warning)
        self.assertIn("主力模型不停用", warning)
        self.assertIn("质量提示", warning)
        self.assertNotIn("已到停用线", warning)
        self.assertNotIn("停不停", warning)
        for ticket_id in ids:
            self.assertIn(ticket_id, warning)

    def test_r2_2_non_main_model_keeps_original_wording(self):
        """非主力模型到线措辞保持原样:仍是「已到停用线」+「自动停用已关」,只通知不落停用名单。"""
        _, warning = self._rework(1, actual_model="glm-5.3")
        self.assertNotIn("停用线", warning)
        for index in (2, 3):
            _, warning = self._rework(index, actual_model="glm-5.3")
        self.assertIn("已到停用线", warning)
        self.assertIn("自动停用已关", warning)
        self.assertIn("累计判退 3 次", warning)
        self.assertNotIn("主力模型不停用", warning)
        self.assertEqual([], self.service.store.load_staff()["模型停用"]["全项目"])

    def test_r3_1_main_model_ban_is_designer_only(self):
        """R1.5 的闸:staff ban 主力模型只有设计者能落笔,总编署名拒并带出设计者原话;非主力不变。"""
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("opus", "总编", reason="核过三次判退确属模型责任")
        message = str(caught.exception)
        self.assertIn("只有设计者", message)
        self.assertIn("绝对不能停用", message)
        # 变体也算主力:换个写法(opus-high)同样过不了这道闸。
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("Opus High", "总编", reason="换个写法试试")
        self.assertIn("只有设计者", str(caught.exception))
        detail = self.service.staff_ban("opus", "设计者", reason="设计者本人拍板,长期算力不足")
        self.assertIn("opus", detail)
        self.assertIn("opus", self.service.store.load_staff()["模型停用"]["全项目"])
        self.service.staff_ban("glm-5.3", "总编", reason="核过责任归属,确属模型责任")
        self.assertIn("glm-5.3", self.service.store.load_staff()["模型停用"]["全项目"])


class SqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = TicketStore(self.root / "files")
        self.service = TicketService(self.source)
        worker = self.service.staff_new(SLOT, "sol")["员工名"]
        deliverable = self.root / "deliverable.txt"
        deliverable.write_text("产物\n", encoding="utf-8")
        ticket = self.service.create_dispatch(
            SLOT, "SQLite 对账", ["DECISIONS.md:SQLite"], "主界面/面板根", worker,
            task_tier="乙", deliverables=[str(deliverable)], internal=False,
        )
        self.service.claim(ticket["编号"], worker)
        image = self.root / "world.png"
        Image.new("RGB", (80, 60), (10, 20, 30)).save(image)
        self.service.attach(ticket["编号"], str(image), "world", worker)
        self.service.say(SLOT, SLOT, "SQLite 对话", reference=ticket["编号"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migrate_is_idempotent_and_dump_round_trips(self):
        database = self.root / "db" / "tickets.sqlite"
        sqlite_store = SqliteStore(database)
        first = sqlite_store.import_files(self.source.root)
        second = sqlite_store.import_files(self.source.root)
        self.assertEqual(first, second)
        self.assertTrue(first["工单字段全同"])
        self.assertTrue(first["图片SHA全同"])

        dumped = self.root / "dumped"
        sqlite_store.dump_files(dumped)
        roundtrip = SqliteStore(self.root / "roundtrip" / "tickets.sqlite").import_files(dumped)
        self.assertTrue(roundtrip["工单字段全同"])
        self.assertTrue(roundtrip["图片SHA全同"])
        self.assertEqual(self.source.list_tickets(), TicketStore(dumped).list_tickets())

    def test_r2_5_migrate_and_dump_preserve_source_image_sha256(self):
        source_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.source.images_dir.iterdir()
            if path.is_file()
        }
        sqlite_store = SqliteStore(self.root / "sha-db" / "tickets.sqlite")
        sqlite_store.import_files(self.source.root)
        self.assertEqual(source_hashes, sqlite_store.image_hashes())

        dumped = self.root / "sha-dump"
        sqlite_store.dump_files(dumped)
        dumped_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in TicketStore(dumped).images_dir.iterdir()
            if path.is_file()
        }
        self.assertEqual(source_hashes, dumped_hashes)

    def test_sqlite_store_runs_service_without_business_changes(self):
        sqlite_store = SqliteStore(self.root / "db" / "tickets.sqlite")
        sqlite_store.import_files(self.source.root)
        service = TicketService(sqlite_store)
        ticket = service.list_tickets(SLOT)[0]
        self.assertEqual("已认领", ticket["状态"])
        # 本例要证的是「say 的那一行能原样穿过 SQLite 导入」,不是它排第几。
        # 起建单会先往该位对话线落一行唤醒通知,原来的下标 0 断言会被挤掉,故改按内容找。
        rows = service.store.read_jsonl(service.store.thread_path(SLOT))
        self.assertIn("SQLite 对话", [row["文字"] for row in rows])

    def test_sqlite_store_preserves_state_entry_time_on_same_state_save(self):
        sqlite_store = SqliteStore(self.root / "fresh" / "tickets.sqlite")
        service = TicketService(sqlite_store)
        worker = service.staff_new(SLOT, "sol")["员工名"]
        deliverable = self.root / "fresh-deliverable.txt"
        deliverable.write_text("产物\n", encoding="utf-8")
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T01:00:00+00:00"):
            ticket = service.create_dispatch(
                SLOT, "SQLite 状态时间", ["DECISIONS.md:SQLite"], "主界面/面板根", worker,
                task_tier="乙", deliverables=[str(deliverable)], internal=False,
            )
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T02:00:00+00:00"):
            ticket = service.claim(ticket["编号"], worker)
        entered = ticket["状态进入时间"]
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T03:00:00+00:00"):
            ticket, _ = service.edit(ticket["编号"], SLOT, deliverables=[str(deliverable), "review/report.md"])
        self.assertEqual("2026-09-01T03:00:00+00:00", ticket["最后更新时间"])
        self.assertEqual(entered, ticket["状态进入时间"])


class StateMachineTests(TicketTestCase):
    def test_stale_thresholds_are_per_state(self):
        now = datetime.now().astimezone()
        claimed = self.dispatch("已认领九小时")
        claimed["状态"] = "已认领"
        claimed["状态进入时间"] = (now - timedelta(hours=9)).isoformat()
        judging = self.dispatch("待判三小时")
        judging["状态"] = "待判"
        judging["状态进入时间"] = (now - timedelta(hours=3)).isoformat()
        self.assertEqual(8, self.service.stale_info(claimed, now)["阈值小时"])
        self.assertIsNone(self.service.stale_info(judging, now))

    def test_stale_check_missing_state_time_falls_back_to_last_update(self):
        now = datetime.now().astimezone()
        ticket = self.dispatch("存量字段回落")
        ticket["状态"] = "已认领"
        ticket.pop("状态进入时间")
        ticket["最后更新时间"] = (now - timedelta(hours=9)).isoformat()
        info = self.service.stale_info(ticket, now)
        self.assertEqual((8, 9), (info["阈值小时"], info["卡住小时"]))

    def test_blocked_and_terminal_tickets_are_never_stale(self):
        now = datetime.now().astimezone()
        for state in ("阻塞", "关闭", "作废", "实机复验过"):
            with self.subTest(state=state):
                ticket = self.dispatch(state)
                ticket["状态"] = state
                ticket["状态进入时间"] = (now - timedelta(days=30)).isoformat()
                self.assertIsNone(self.service.stale_info(ticket, now))
        blocker = self.service.create_question("阻塞", SLOT, "阻塞类型", "等待协调")
        blocker["状态进入时间"] = (now - timedelta(days=30)).isoformat()
        self.assertIsNone(self.service.stale_info(blocker, now))

    def test_digest_summarizes_stale_tickets_by_state_and_turn(self):
        now = datetime.now().astimezone()
        claimed_rows = [self.dispatch(f"已认领停滞 {index}") for index in range(2)]
        for row in claimed_rows:
            row["状态"] = "已认领"
            row["状态进入时间"] = (now - timedelta(hours=9)).isoformat()
            self.service.store.atomic_json(self.service.store.item_path(row["编号"]), row)
        judging = self.dispatch("待判停滞")
        judging["状态"] = "待判"
        judging["状态进入时间"] = (now - timedelta(hours=5)).isoformat()
        self.service.store.atomic_json(self.service.store.item_path(judging["编号"]), judging)
        fresh = self.dispatch("待判未超线")
        fresh["状态"] = "待判"
        fresh["状态进入时间"] = (now - timedelta(hours=3)).isoformat()
        self.service.store.atomic_json(self.service.store.item_path(fresh["编号"]), fresh)

        digest = self.service.digest()
        self.assertIn("停滞 3 张(已认领 2 · 待判 1)", digest[1])
        stale_lines = [line for line in digest if line.startswith("[停滞]")]
        self.assertEqual(3, len(stale_lines))
        self.assertTrue(any(f"轮到 {self.worker}" in line for line in stale_lines))
        self.assertTrue(any(f"轮到 {SLOT}" in line for line in stale_lines))

    def test_digest_never_truncates_stale_section(self):
        now = datetime.now().astimezone()
        old = (now - timedelta(hours=9)).isoformat()
        rows = [
            {
                "编号": f"T-{index:06d}", "标题": f"停滞单 {index}", "类型": "派单", "状态": "已认领",
                "状态进入时间": old, "最后更新时间": old, "发起时间": old, "所属总监位": SLOT,
                "指派给": self.worker, "发起位": "", "转交历史": [],
            }
            for index in range(1, 62)
        ]
        with mock.patch.object(self.service.store, "list_tickets", return_value=rows), \
             mock.patch.object(self.service.store, "read_jsonl", return_value=[]), \
             mock.patch.object(self.service, "model_statistics", return_value=[]):
            digest = self.service.digest()
        self.assertTrue(any(line.startswith("[停滞] T-000061") for line in digest))
        self.assertEqual("48 小时", self.service._stale_duration_text(48))
        self.assertEqual("2 天", self.service._stale_duration_text(49))

    def test_state_entry_time_changes_only_when_state_changes(self):
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T01:00:00+00:00"):
            ticket = self.dispatch()
        self.assertEqual("2026-09-01T01:00:00+00:00", ticket["状态进入时间"])
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T02:00:00+00:00"):
            ticket = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("2026-09-01T02:00:00+00:00", ticket["状态进入时间"])
        old_state_time = ticket["状态进入时间"]
        with mock.patch.object(store_module, "now_text", return_value="2026-09-01T03:00:00+00:00"):
            ticket, _ = self.service.edit(ticket["编号"], SLOT, deliverables=[str(self.deliverable), "review/report.md"])
        self.assertEqual("2026-09-01T03:00:00+00:00", ticket["最后更新时间"])
        self.assertEqual(old_state_time, ticket["状态进入时间"])

    def test_missing_state_entry_time_falls_back_to_previous_update(self):
        ticket = self.dispatch()
        path = self.service.store.item_path(ticket["编号"])
        stored = self.service.store.load_ticket(ticket["编号"])
        previous_update = stored["最后更新时间"]
        stored.pop("状态进入时间")
        self.service.store.atomic_json(path, stored)
        ticket, _ = self.service.edit(ticket["编号"], SLOT, deliverables=[str(self.deliverable), "review/report.md"])
        self.assertEqual(previous_update, ticket["状态进入时间"])

    def test_backfill_state_time_cli_is_idempotent(self):
        ticket = self.dispatch()
        ticket = self.service.claim(ticket["编号"], self.worker)
        expected = ticket["状态进入时间"]
        path = self.service.store.item_path(ticket["编号"])
        stored = self.service.store.load_ticket(ticket["编号"])
        stored.pop("状态进入时间")
        self.service.store.atomic_json(path, stored)

        first = run_local_cli(["migrate", "--backfill-state-time"], self.service.store.root)
        second = run_local_cli(["migrate", "--backfill-state-time"], self.service.store.root)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("已补齐 1", first.stdout)
        self.assertIn("已跳过 1", second.stdout)
        self.assertEqual(expected, self.service.store.load_ticket(ticket["编号"])["状态进入时间"])

    def test_full_legal_dispatch_path(self):
        ticket = self.dispatch()
        ticket = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("已认领", ticket["状态"])
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        ticket = self.service.submit(ticket["编号"], "登录后可见")
        self.assertEqual("待判", ticket["状态"])
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("待复检", ticket["状态"])
        ticket = self.merged_ticket(ticket["编号"], "独立复检")
        self.assertEqual("已合并", ticket["状态"])
        ticket = self.service.live(ticket["编号"], str(self.picture("live.png")), "独立复检", "独图")
        self.assertEqual("实机复验过", ticket["状态"])
        ticket = self.service.close(ticket["编号"], "总编")
        self.assertEqual("关闭", ticket["状态"])

    def test_rework_returns_to_claim(self):
        ticket = self.to_judging()
        ticket, _ = self.service.judge(
            ticket["编号"], False, "UI总监", "入口仍会闪一下", REWORK_VERDICT, "模型",
        )
        self.assertEqual("返工", ticket["状态"])
        self.assertEqual(1, ticket["返工次数"])
        ticket = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("已认领", ticket["状态"])

    def test_block_and_unblock_lands_on_rework(self):
        """阻塞照旧能解开,只是落点从「阻塞前状态」改成一律「返工」。

        这条守的仍是老命题「阻塞解得开、流程提示会换」,断言里变的只有那一个落点。
        """
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        ticket = self.service.block(ticket["编号"], "缺一张登录图")
        self.assertEqual("阻塞", ticket["状态"])
        self.assertEqual("先把不依赖它的部分做完并交板,再收窗", ticket["流程提示"])
        ticket = self.service.unblock(ticket["编号"])
        self.assertEqual("返工", ticket["状态"])
        self.assertIn("发续单", ticket["流程提示"])

    def test_answer_path_and_close(self):
        ticket = self.service.create_question("拍板", SLOT, "颜色选择", VALID_DECISION_BODY, sources=["DECISIONS.md:颜色"])
        ticket = self.service.answer(ticket["编号"], "采用暖色", "设计者")
        self.assertEqual("已答", ticket["状态"])
        self.assertEqual("关闭", self.service.close(ticket["编号"], "设计者")["状态"])

    def test_blocker_is_a_distinct_ticket_type(self):
        ticket = self.service.create_question("阻塞", SLOT, "缺登录凭据", "请总编协调")
        self.assertEqual("阻塞", ticket["类型"])
        self.assertEqual("待答", ticket["状态"])
        with self.assertRaisesRegex(TicketError, "只能由总编"):
            self.service.answer(ticket["编号"], "已协调", "设计者")
        self.assertEqual("已答", self.service.answer(ticket["编号"], "已协调", "总编")["状态"])

    def test_each_wrong_state_is_rejected_with_plain_reason(self):
        ticket = self.dispatch()
        image = str(self.picture())
        calls = [
            lambda: self.service.submit(ticket["编号"], "说明"),
            lambda: self.service.judge(ticket["编号"], True, "判卷人"),
            lambda: self.service.merge(ticket["编号"], "复检人"),
            lambda: self.service.live(ticket["编号"], image, "复检人", "独图"),
            lambda: self.service.close(ticket["编号"], "总编"),
            lambda: self.service.unblock(ticket["编号"]),
        ]
        for call in calls:
            with self.subTest(call=call), self.assertRaisesRegex(TicketError, "只有|必须"):
                call()


class BlockedAnswerPermissionTests(TicketTestCase):
    """阻塞单放行「所属总监位 + 总编」两方。

    设计者仍然答不动阻塞，那一条由 StateMachineTests.test_blocker_is_a_distinct_ticket_type
    钉着，本类不重复；需求已在另行放开（见 DemandAnswerPermissionTests），
    总工单的口径一个字没动，这里只留一条回归。
    """

    def blocked(self, title: str = "缺登录凭据", slot: str = SLOT):
        return self.service.create_question("阻塞", slot, title, "请协调一下登录凭据。")

    def test_owner_slot_can_answer_blocked_including_after_transfer(self):
        ticket = self.service.answer(self.blocked()["编号"], "已协调，凭据放在 server-keys。", SLOT)
        self.assertEqual("已答", ticket["状态"])
        self.assertEqual("已协调，凭据放在 server-keys。", ticket["答复"])
        # transfer 把「所属总监位」改成接收位，所以放行的是转交后的接收位，不是最初挂的那一位；
        # 这也正是的场景：总编把阻塞转回所属位，所属位要按得动。
        moved = self.service.transfer(self.blocked("转给别位的阻塞")["编号"], OTHER_SLOT, "归他管", "总编")
        self.assertEqual(OTHER_SLOT, moved["所属总监位"])
        self.assertEqual("已答", self.service.answer(moved["编号"], "已协调。", OTHER_SLOT)["状态"])

    def test_other_slot_is_refused_and_the_error_names_the_owner(self):
        ticket = self.blocked()
        with self.assertRaises(TicketError) as caught:
            self.service.answer(ticket["编号"], "我来代答。", OTHER_SLOT)
        message = str(caught.exception)
        for expected in (ticket["编号"], SLOT, OTHER_SLOT):
            self.assertIn(expected, message)
        self.assertEqual("待答", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_orchestrator_can_still_answer_blocked(self):
        self.assertEqual("已答", self.service.answer(self.blocked()["编号"], "已协调。", "总编")["状态"])

    def test_general_ticket_still_refuses_the_owner_slot(self):
        """总工单是总编自己的账本，放开需求时它一个字没动。"""
        ticket = self.service.create_question("总工单", SLOT, "本周总账", "请总编汇总。")
        with self.assertRaisesRegex(TicketError, "只能由总编答复"):
            self.service.answer(ticket["编号"], "已汇总。", SLOT)
        self.assertEqual("已答", self.service.answer(ticket["编号"], "已汇总。", "总编")["状态"])

    def test_r2_front_end_shows_blocked_but_never_answers_it(self):
        """网页端只显示不答复（总编 2026-09-05 15:32 定的口径）。

        解阻的事实只在所属总监手里，页面上按下去等于替他背名；总监答单走命令行。
        所以这里钉三件事：阻塞不进设计者队列、answerTicket 对阻塞就地 return 不发请求、
        卡片 meta 行替他说清在等谁。
        """
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")

        wants = re.search(r"function wantsDesignerAnswer\(t\)\{(.*?)\n\}", script, re.S)
        self.assertIsNotNone(wants, "没找到 wantsDesignerAnswer，网页钉测的锚点漂了")
        self.assertNotIn("阻塞", wants.group(1), "阻塞不该进设计者的待答队列")

        answer_fn = re.search(r"async function answerTicket\(id\)\{.*", script)
        self.assertIsNotNone(answer_fn, "没找到 answerTicket，网页钉测的锚点漂了")
        body = answer_fn.group(0)
        guard = re.search(r"if\(t&&t\.类型==='阻塞'\)\{(.*?)return\}", body)
        self.assertIsNotNone(guard, "answerTicket 里没有「阻塞就地 return」那一段")
        self.assertNotIn("api(", guard.group(1), "阻塞分支不许发请求")
        self.assertIn("notify(", guard.group(1), "阻塞分支要留一句人话")
        actor = re.search(r"actor=(.*?);await api\(", body)
        self.assertIsNotNone(actor, "没找到 answerTicket 的署名分支")
        self.assertNotIn("阻塞", actor.group(1), "阻塞不该再参与网页署名")

        # 不用 assertRegex：它失败时会把整份 tickets.js（90 KB）原样打进报告，判卷根本没法看。
        card = re.search(r"等 \$\{[^}]*所属总监位[^}]*\} 答", script)
        self.assertIsNotNone(card, "卡片 meta 行没有「等 <所属总监位> 答」那一句")


class HardGateTests(TicketTestCase):
    def test_gate_1_source_and_consumer_required_before_claim(self):
        missing_source = self.service.create_dispatch(SLOT, "缺依据", [], "主界面", self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=False)
        with self.assertRaisesRegex(TicketError, "真源指针"):
            self.service.claim(missing_source["编号"], self.worker)
        missing_consumer = self.service.create_dispatch(SLOT, "缺消费者", ["DECISIONS.md:1"], "", self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=False)
        with self.assertRaisesRegex(TicketError, "实机消费者"):
            self.service.claim(missing_consumer["编号"], self.worker)

    def test_gate_2_isolated_picture_stays_isolated_and_submit_goes_through(self):
        """附真登录图改为选填:只附隔离场景图也能交板,但那张图照记「隔离场景」,不会被算成真登录图。"""
        ticket = self.dispatch(); self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "isolated", self.worker)
        submitted = self.service.submit(ticket["编号"], "隔离场景看到了")
        self.assertEqual("待判", submitted["状态"])
        self.assertEqual(["隔离场景"], [row["来源标注"] for row in submitted["接线证据"]["图片列表"]])
        self.assertEqual([], self.service._world_images(submitted))
        self.assertNotIn("欠真登录图", submitted)

    def test_gate_3_judge_must_differ_from_worker(self):
        ticket = self.to_judging()
        with self.assertRaisesRegex(TicketError, "不能与执行员工"):
            self.service.judge(ticket["编号"], True, self.worker)

    def test_gate_4_reviewer_must_differ_from_both(self):
        ticket = self.to_judging(); ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        # 先把复验那一道补上,否则先撞的是「还没复验」,测不到这里要钉的三方互斥闸。
        self.verified(ticket["编号"])
        for actor in (self.worker, "UI总监"):
            with self.subTest(actor=actor), self.assertRaisesRegex(TicketError, "都不同"):
                self.service.merge(ticket["编号"], actor)

    def test_gate_5_live_adds_a_second_world_picture(self):
        ticket = self.to_judging(); ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        ticket = self.merged_ticket(ticket["编号"], "独立复检")
        before = len(ticket["接线证据"]["图片列表"])
        ticket = self.service.live(ticket["编号"], str(self.picture("second.png")), "独立复检", "独图")
        self.assertEqual(before + 1, len(ticket["接线证据"]["图片列表"]))
        self.assertEqual("真登录", ticket["接线证据"]["图片列表"][-1]["来源标注"])

    def test_gate_6_answer_roles(self):
        decision = self.service.create_question("拍板", SLOT, "拍板", VALID_DECISION_BODY)
        with self.assertRaisesRegex(TicketError, "设计者或总编"):
            self.service.answer(decision["编号"], "乱答", "普通员工")
        # 需求的答复权放开给了所属位与设计者，但三种前缀对谁都一样：「乱答」照样拒。
        requirement = self.service.create_question("需求", SLOT, "需求", "请改")
        with self.assertRaisesRegex(TicketError, "首词必须是这三种之一"):
            self.service.answer(requirement["编号"], "乱答", "设计者")
        general = self.service.create_question("总工单", SLOT, "总工单", "请汇总")
        with self.assertRaisesRegex(TicketError, "只能由总编"):
            self.service.answer(general["编号"], "乱答", "设计者")

    def test_gate_7_staff_registry_and_format(self):
        for actor in ("前端·页面接线-99", "格式错误"):
            ticket = self.service.create_dispatch(SLOT, actor, ["DECISIONS.md:1"], "主界面", task_tier="乙", deliverables=[str(self.deliverable)], internal=False)
            with self.subTest(actor=actor), self.assertRaisesRegex(TicketError, "员工名格式|名册"):
                self.service.claim(ticket["编号"], actor)

    def test_rework_to_retired_worker_prints_reassign_warning(self):
        ticket = self.to_judging(); self.service.staff_retire(self.worker)
        ticket, warning = self.service.judge(
            ticket["编号"], False, "UI总监", "入口不稳", REWORK_VERDICT, "模型",
        )
        self.assertEqual(self.worker, ticket["指派给"])
        self.assertIn("已收窗", warning)
        self.assertIn("改派", warning)


class LiveShotTests(TicketTestCase):
    def test_live_requires_shot_with_exact_plain_message(self):
        ticket = self.to_merged()
        result = run_local_cli([
            "live", ticket["编号"], str(self.picture("missing-shot.png")), "--by", "独立复检",
        ], self.service.store.root)
        self.assertEqual(2, result.returncode)
        self.assertIn(
            "live 必须说明这张图是同图(全批共用一张)还是独图(专为这张单拍):--shot 同图 或 --shot 独图。",
            result.stderr,
        )

    def test_shot_classifies_unique_shared_internal_and_old_rows(self):
        unique = self.service.live(
            self.to_merged("独图单")["编号"], str(self.picture("unique.png")), "独立复检", "独图",
        )
        shared = self.service.live(
            self.to_merged("同图可感知")["编号"], str(self.picture("shared.png")), "独立复检", "同图",
        )
        internal = self.service.live(self.to_merged("同图内部", internal=True)["编号"], "", "独立复检", "同图")
        self.assertEqual("独图", unique["实机图标记"])
        self.assertEqual("待独图", shared["实机图标记"])
        self.assertEqual("同图", internal["实机图标记"])
        self.assertEqual([], internal["图片列表"])
        old = dict(unique)
        old.pop("实机图标记")
        from tools.tickets.ticket import compact_ticket
        self.assertTrue(compact_ticket(old).endswith(" · 乙档"))

    def test_pending_ticket_can_add_unique_picture_without_state_change(self):
        ticket = self.to_merged("补独图")
        ticket = self.service.live(ticket["编号"], str(self.picture("shared-first.png")), "独立复检", "同图")
        before = len(ticket["接线证据"]["图片列表"])
        entered = ticket["状态进入时间"]
        ticket = self.service.live(ticket["编号"], str(self.picture("unique-later.png")), "独立复检", "独图")
        self.assertEqual("实机复验过", ticket["状态"])
        self.assertEqual("独图", ticket["实机图标记"])
        self.assertEqual(before + 1, len(ticket["接线证据"]["图片列表"]))
        self.assertEqual(entered, ticket["状态进入时间"])
        events = self.service.store.read_jsonl(self.service.store.log_path)
        self.assertTrue(any(row.get("工单号") == ticket["编号"] and "补独图" in row.get("说明", "") for row in events))

    def test_only_pending_ticket_can_be_lived_again(self):
        ticket = self.to_merged("不可重复")
        ticket = self.service.live(ticket["编号"], str(self.picture("already-unique.png")), "独立复检", "独图")
        with self.assertRaisesRegex(TicketError, "只有实机图标记为.*待独图"):
            self.service.live(ticket["编号"], str(self.picture("repeat.png")), "独立复检", "独图")


class ShotExemptTests(TicketTestCase):
    """诊断类单与验证对象已退役的单可以免掉那张独图,但只有复检席与总编能打,且必须写原因。"""

    REASON = "判语已写明无玩家可见产出,属诊断类单"

    def pending(self, title: str = "待独图豁免"):
        ticket = self.to_merged(title)
        return self.service.live(
            ticket["编号"], str(self.picture(f"{ticket['编号']}-shared.png")), "独立复检", "同图",
        )

    def test_review_slot_marks_exempt_and_writes_reason(self):
        ticket = self.pending()
        ticket = self.service.shot_exempt(ticket["编号"], "复检·合并-01", self.REASON)
        self.assertEqual("免独图", ticket["实机图标记"])
        self.assertEqual(self.REASON, ticket["免独图原因"])
        self.assertEqual("实机复验过", ticket["状态"])
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("免独图", stored["实机图标记"])
        self.assertEqual(self.REASON, stored["免独图原因"])
        events = self.service.store.read_jsonl(self.service.store.log_path)
        self.assertTrue(any(
            row.get("工单号") == ticket["编号"] and row.get("事件") == "live"
            and f"免独图 · {self.REASON}" == row.get("说明") for row in events
        ))

    def test_conductor_may_also_mark_exempt(self):
        ticket = self.pending("总编也能打")
        ticket = self.service.shot_exempt(ticket["编号"], "总编", "验证对象已退役,原命题不再成立")
        self.assertEqual("免独图", ticket["实机图标记"])
        self.assertEqual("验证对象已退役,原命题不再成立", ticket["免独图原因"])

    def test_other_slot_is_rejected_with_its_signature_quoted(self):
        ticket = self.pending("别位总监不能打")
        with self.assertRaises(TicketError) as caught:
            self.service.shot_exempt(ticket["编号"], SLOT, self.REASON)
        self.assertIn(SLOT, str(caught.exception))
        self.assertIn(ticket["编号"], str(caught.exception))
        self.assertEqual("待独图", self.service.store.load_ticket(ticket["编号"])["实机图标记"])

    def test_wrong_mark_or_wrong_state_is_rejected(self):
        unique = self.service.live(
            self.to_merged("已经是独图")["编号"], str(self.picture("exempt-unique.png")), "独立复检", "独图",
        )
        with self.assertRaisesRegex(TicketError, "实机图标记现在是「独图」"):
            self.service.shot_exempt(unique["编号"], "复检·合并-01", self.REASON)
        merged = self.to_merged("还没复验过")
        with self.assertRaisesRegex(TicketError, "现在是「已合并」"):
            self.service.shot_exempt(merged["编号"], "复检·合并-01", self.REASON)
        self.assertEqual("独图", self.service.store.load_ticket(unique["编号"])["实机图标记"])
        self.assertEqual("", self.service.store.load_ticket(merged["编号"])["实机图标记"])

    def test_empty_reason_is_rejected(self):
        ticket = self.pending("缺原因")
        for reason in ("", "   "):
            with self.subTest(reason=reason), self.assertRaisesRegex(TicketError, "--reason 不能为空"):
                self.service.shot_exempt(ticket["编号"], "复检·合并-01", reason)
        self.assertEqual("待独图", self.service.store.load_ticket(ticket["编号"])["实机图标记"])

    def test_exempt_drops_out_of_pending_list_and_digest_count(self):
        from tools.tickets.ticket import execute, parser

        target = self.pending("要被豁免的")
        other = self.pending("仍然待独图")
        before_rows, _ = execute(parser().parse_args(["list", "--shot-pending"]), self.service)
        before_count = self._digest_pending(self.service.digest())
        self.assertEqual(2, before_count)
        self.assertEqual(
            {target["编号"], other["编号"]}, {row["编号"] for row in before_rows},
        )
        self.service.shot_exempt(target["编号"], "复检·合并-01", self.REASON)
        after_rows, _ = execute(parser().parse_args(["list", "--shot-pending"]), self.service)
        after_count = self._digest_pending(self.service.digest())
        self.assertEqual([other["编号"]], [row["编号"] for row in after_rows])
        self.assertEqual(before_count - 1, after_count)

    @staticmethod
    def _digest_pending(lines: list[str]) -> int:
        row = next(line for line in lines if line.startswith("待独图 "))
        return int(re.fullmatch(r"待独图 (\d+) 张", row).group(1))

    def test_cli_rejects_batch_and_picture_and_marks_through_service(self):
        ticket = self.pending("命令行豁免")
        batched = run_local_cli([
            "live", ticket["编号"], "--batch", "T-000001", "--shot", "免独图",
            "--reason", self.REASON, "--by", "复检·合并-01",
        ], self.service.store.root)
        self.assertEqual(2, batched.returncode)
        self.assertIn("不能和 --batch 一起用", batched.stderr)
        withpic = run_local_cli([
            "live", ticket["编号"], str(self.picture("exempt-with-pic.png")), "--shot", "免独图",
            "--reason", self.REASON, "--by", "复检·合并-01",
        ], self.service.store.root)
        self.assertEqual(2, withpic.returncode)
        self.assertIn("不要图", withpic.stderr)
        self.assertEqual("待独图", self.service.store.load_ticket(ticket["编号"])["实机图标记"])
        good = run_local_cli([
            "live", ticket["编号"], "--shot", "免独图",
            "--reason", self.REASON, "--by", "复检·合并-01",
        ], self.service.store.root)
        self.assertEqual(0, good.returncode, good.stderr)
        self.assertIn(f"免独图 · {self.REASON}", good.stdout)
        self.assertEqual("免独图", self.service.store.load_ticket(ticket["编号"])["实机图标记"])

    def test_first_live_cannot_take_the_exempt_value(self):
        """已合并态的首次 live 不许免图:SHOT_EXEMPT 不在 SHOT_VALUES,豁免只走 shot_exempt。"""
        from tools.tickets.service import SHOT_EXEMPT, SHOT_VALUES

        self.assertNotIn(SHOT_EXEMPT, SHOT_VALUES)
        merged = self.to_merged("首次 live 不能免图")
        with self.assertRaisesRegex(TicketError, "live 必须说明这张图"):
            self.service.live(merged["编号"], str(self.picture("first-live.png")), "总编", SHOT_EXEMPT)
        self.assertEqual("已合并", self.service.store.load_ticket(merged["编号"])["状态"])

    def test_browser_card_shows_exempt_reason(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn("免独图原因", script)
        self.assertIn('value==="免独图"', script)


class LiveBatchTests(TicketTestCase):
    def five_merged(self) -> list[dict[str, object]]:
        return [
            self.to_merged("可感知一"), self.to_merged("内部一", internal=True),
            self.to_merged("可感知二"), self.to_merged("内部二", internal=True),
            self.to_merged("可感知三"),
        ]

    def test_one_picture_batch_marks_two_shared_and_three_pending(self):
        tickets = self.five_merged()
        rows = self.service.live_batch(
            [row["编号"] for row in tickets], str(self.picture("batch.png")), "独立复检", "同图",
        )
        self.assertEqual(["已复验"] * 5, [row["结果"] for row in rows])
        stored = [self.service.store.load_ticket(row["编号"]) for row in tickets]
        self.assertEqual(["实机复验过"] * 5, [row["状态"] for row in stored])
        self.assertEqual(2, sum(row["实机图标记"] == "同图" for row in stored))
        self.assertEqual(3, sum(row["实机图标记"] == "待独图" for row in stored))

    def test_wrong_state_is_skipped_without_dragging_down_other_five(self):
        tickets = self.five_merged()
        wrong = self.to_judging()
        rows = self.service.live_batch(
            [wrong["编号"], *[row["编号"] for row in tickets]],
            str(self.picture("mixed-batch.png")), "独立复检", "同图",
        )
        self.assertEqual("跳过", rows[0]["结果"])
        self.assertIn("当前状态是“待判”", rows[0]["原因"])
        self.assertEqual(["已复验"] * 5, [row["结果"] for row in rows[1:]])
        self.assertEqual("待判", self.service.store.load_ticket(wrong["编号"])["状态"])
        self.assertTrue(all(
            self.service.store.load_ticket(row["编号"])["状态"] == "实机复验过" for row in tickets
        ))

    def test_pending_filter_digest_and_compact_output(self):
        pending = self.service.live(
            self.to_merged("筛选目标")["编号"], str(self.picture("pending-filter.png")), "独立复检", "同图",
        )
        self.service.live(
            self.to_merged("不是目标")["编号"], str(self.picture("unique-filter.png")), "独立复检", "独图",
        )
        from tools.tickets.ticket import execute, parser
        rows, text = execute(parser().parse_args(["list", "--shot-pending"]), self.service)
        self.assertEqual([pending["编号"]], [row["编号"] for row in rows])
        self.assertIn("· 待独图", text)
        self.assertNotIn("不是目标", text)
        self.assertIn("待独图 1 张", self.service.digest())

    def test_browser_card_guard_contains_pending_marker(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn("待独图", script)
        self.assertIn("shotMark(t)", script)


class StaffAndConversationTests(TicketTestCase):
    def test_staff_numbers_never_reuse_and_are_independent_per_slot(self):
        second = self.service.staff_new(SLOT, "codex")["员工名"]
        third = self.service.staff_new(SLOT, "claude")["员工名"]
        other = self.service.staff_new(OTHER_SLOT, "sol")["员工名"]
        self.assertEqual(["前端·页面接线-01", "前端·页面接线-02", "前端·页面接线-03"], [self.worker, second, third])
        self.assertEqual("后端·服务-01", other)
        self.service.staff_retire(second)
        reopened = self.service.staff_reopen(second)
        self.assertEqual(second, reopened["员工名"])
        self.assertEqual("在岗", reopened["状态"])

    def test_history_lists_owned_tickets(self):
        ticket = self.dispatch()
        history = self.service.history(self.worker)
        self.assertEqual([ticket["编号"]], [row["编号"] for row in history["工单"]])

    def test_say_inbox_mark_read_and_slot_isolation(self):
        self.service.say(SLOT, "设计者", "请检查", reference="")
        self.service.say(OTHER_SLOT, "设计者", "另一个位")
        rows = self.service.inbox(SLOT, "总编")
        self.assertEqual(["请检查"], [row["文字"] for row in rows])
        self.assertEqual(1, len(self.service.inbox(SLOT, "总编", True)))
        self.assertEqual([], self.service.inbox(SLOT, "总编"))


class TransferAndHumanGateTests(TicketTestCase):
    def test_transfer_four_targets_keeps_id_and_state_and_updates_views_log_digest(self):
        targets = ("总编", "复检·合并", OTHER_SLOT, "设计者")
        ticket_ids = []
        for target in targets:
            original = self.dispatch(f"转给{target}")
            ticket_ids.append(original["编号"])
            transferred = self.service.transfer(original["编号"], target, f"需要{target}接手", SLOT)
            self.assertEqual(original["编号"], transferred["编号"])
            self.assertEqual("新建", transferred["状态"])
            self.assertEqual(target, transferred["指派给"])
            self.assertEqual(target, transferred["转交历史"][-1]["到"])
            self.assertEqual(1, sum(row["编号"] == original["编号"] for row in self.service.list_tickets(SLOT)))
            if target in ("总编", "复检·合并", OTHER_SLOT):
                self.assertEqual(1, sum(row["编号"] == original["编号"] for row in self.service.list_tickets(target)))

        transfer_logs = [row for row in self.service.store.read_jsonl(self.service.store.log_path) if row.get("op") == "transfer"]
        self.assertEqual(4, len(transfer_logs))
        self.assertEqual({"from", "to", "reason", "by"}, {key for key in transfer_logs[0] if key in {"from", "to", "reason", "by"}})
        digest = "\n".join(self.service.digest())
        self.assertIn("今日转交", digest)
        for ticket_id in ticket_ids:
            self.assertIn(ticket_id, digest)
        source_notices = [row for row in self.service.inbox(SLOT, "总编") if row.get("引用工单号") in ticket_ids]
        self.assertEqual(4, len(source_notices))
        incoming = [row for row in self.service.inbox(OTHER_SLOT, "总编") if row.get("引用工单号") == ticket_ids[2]]
        self.assertEqual(1, len(incoming))

    def test_empty_transfer_reason_is_rejected(self):
        ticket = self.dispatch()
        with self.assertRaisesRegex(TicketError, "请用一句话写明原因"):
            self.service.transfer(ticket["编号"], "总编", "  ", SLOT)

    def test_submitted_dispatch_transfer_keeps_original_worker(self):
        ticket = self.to_judging()
        transferred = self.service.transfer(ticket["编号"], "总编", "交给总编判卷", SLOT)
        self.assertEqual("待判", transferred["状态"])
        self.assertEqual(self.worker, transferred["指派给"])
        self.assertEqual("总编", transferred["所属总监位"])

    def test_fresh_dispatch_transfer_still_changes_assignee(self):
        ticket = self.dispatch("新建态换人接手")
        transferred = self.service.transfer(ticket["编号"], "总编", "换人接手", SLOT)
        self.assertEqual("新建", transferred["状态"])
        self.assertEqual("总编", transferred["指派给"])

    def test_decision_requires_three_human_sections_and_limits_bare_ids(self):
        with self.assertRaisesRegex(TicketError, "拍板单要写成三段人话"):
            self.service.create_question("拍板", SLOT, "缺段", "一、这是什么\n一个问题\n二、选了会怎样\n会改变界面")
        too_many = VALID_DECISION_BODY + "\nDA-1 DA-2 PV-3 BE-4"
        # ★这一条原来也断言「拍板单要写成三段人话」——它把「两个病因共用一句拒绝语」钉成了期望值。
        #   三段标题在这份正文里是齐的,真病是编号太多;照那句话去改标题永远改不好(顺手修)。
        with self.assertRaisesRegex(TicketError, "专业编号"):
            self.service.create_question("拍板", SLOT, "术语过多", too_many)
        parenthesized = VALID_DECISION_BODY + "\n（DA-1 DA-2 PV-3 BE-4）"
        self.assertEqual("待答", self.service.create_question("拍板", SLOT, "括号说明", parenthesized)["状态"])
        legacy = self.service.create_question("拍板", SLOT, "旧专业单", VALID_DECISION_BODY)
        legacy["正文"] = "只有专业编号 DA-1 DA-2 PV-3 BE-4，没有三段人话"
        self.service.store.atomic_json(self.service.store.item_path(legacy["编号"]), legacy)
        with self.assertRaisesRegex(TicketError, "拍板单要写成三段人话"):
            self.service.transfer(legacy["编号"], "设计者", "送设计者拍板", SLOT)

    def test_non_dispatch_transfer_returns_to_waiting_answer(self):
        ticket = self.service.create_question("拍板", SLOT, "重新转交", VALID_DECISION_BODY)
        ticket = self.service.answer(ticket["编号"], "先按推荐", "设计者")
        self.assertEqual("已答", ticket["状态"])
        ticket = self.service.transfer(ticket["编号"], "总编", "请总编复核", "设计者")
        self.assertEqual("待答", ticket["状态"])

    def test_non_three_party_say_is_rejected_with_exact_reason(self):
        # 起，拒的话里带着「带 --ref 就能在自己那张单上留言」的出路；
        # (2.4)起总监间默认直达——DESK 不再在拒的范围内,
        # 这一条只剩员工(不带窄缝)会被拒。
        expected = TicketService.SAY_REFUSED
        self.assertIn("任一总监位", expected)
        with self.assertRaises(TicketError) as caught:
            self.service.say(SLOT, self.worker, "员工不带 --ref 仍然进不去")
        self.assertEqual(expected, str(caught.exception))

    def test_cross_desk_say_now_lands_without_the_staff_prefix(self):
        """ 总监间直达:A 位写 B 位的线,直接落、不带【员工留言】前缀。"""
        row = self.service.say(OTHER_SLOT, SLOT, "本位补一句:先看 T-000190 的用法")
        self.assertEqual(SLOT, row["发言人"])
        self.assertEqual("本位补一句:先看 T-000190 的用法", row["文字"])
        self.assertNotIn("【员工留言】", row["文字"])
        lines = self.service.store.read_jsonl(self.service.store.thread_path(OTHER_SLOT))
        self.assertEqual(SLOT, lines[-1]["发言人"], "话要落在被写的 B 位线上")

    def test_other_desks_employee_still_needs_the_narrow_seam(self):
        """员工不在放开之列:别位的员工写别位的线,不带自己单的 --ref 照旧被拒。"""
        with self.assertRaises(TicketError):
            self.service.say(SLOT, f"{OTHER_SLOT}-07", "别位员工串门")

    def test_cross_slot_ticket_records_origin_and_is_copied_to_digest(self):
        ticket = self.service.create_question("疑问", OTHER_SLOT, "跨位求证", "请后端总监答复", initiator=self.worker)
        self.assertEqual(SLOT, ticket["发起位"])
        self.assertEqual(OTHER_SLOT, ticket["指派给"])
        digest = "\n".join(self.service.digest())
        self.assertIn(f"[跨位单] {ticket['编号']} · {SLOT}→{OTHER_SLOT} · 抄送总编", digest)

    def test_missing_deliverable_blocks_whole_submit_and_lists_the_line(self):
        missing = self.root / "missing-output.bin"
        ticket = self.service.create_dispatch(
            SLOT, "缺交付件", ["DECISIONS.md:1"], "主界面/面板根", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable), str(missing)], internal=False,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        with self.assertRaisesRegex(TicketError, re.escape(str(missing))):
            self.service.submit(ticket["编号"], "已有一部分")
        self.assertEqual("已认领", self.service.store.load_ticket(ticket["编号"])["状态"])


class InternalToolAndVerdictTests(TicketTestCase):
    def internal_dispatch(self):
        return self.service.create_dispatch(
            SLOT, "内部工具改造", ["tools/tickets/service.py"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )

    def test_internal_submit_requires_command_and_raw_output_but_not_world_picture(self):
        ticket = self.internal_dispatch()
        self.assertTrue(ticket["非玩家可感知"])
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaisesRegex(TicketError, "验证命令与原样输出"):
            self.service.submit(ticket["编号"], "内部验证", "python -m pytest", "")
        ticket = self.service.submit(ticket["编号"], "内部验证", "python -m pytest", "44 passed")
        self.assertEqual("待判", ticket["状态"])
        self.assertEqual("python -m pytest", ticket["接线证据"]["验证命令"])
        self.assertEqual("44 passed", ticket["接线证据"]["原样输出"])
        self.assertEqual([], ticket["图片列表"])

    def test_internal_live_bypasses_second_world_picture_gate(self):
        ticket = self.internal_dispatch()
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "44 passed")
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        ticket = self.merged_ticket(ticket["编号"], "独立复检")
        ticket = self.service.live(ticket["编号"], "", "独立复检", "同图")
        self.assertEqual("实机复验过", ticket["状态"])
        self.assertEqual([], ticket["图片列表"])

    def test_judge_requires_verdict_and_pass_requires_opening_sentence(self):
        ticket = self.to_judging()
        with self.assertRaisesRegex(TicketError, "判语不能为空"):
            self.service.judge(ticket["编号"], True, "UI总监")
        with self.assertRaisesRegex(TicketError, "玩家怎么打开它"):
            self.service.judge(ticket["编号"], True, "UI总监", verdict="功能通过。")
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual(PASS_VERDICT, ticket["判语"])

    def test_internal_judge_accepts_player_or_designer_opening_sentence(self):
        verdicts = (
            "玩家怎么打开它：双击启动工单台。功能通过。",
            "设计者怎么打开它：双击启动工单台。功能通过。",
        )
        for verdict in verdicts:
            with self.subTest(verdict=verdict):
                ticket = self.internal_dispatch()
                self.service.claim(ticket["编号"], self.worker)
                self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
                judged, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=verdict)
                self.assertEqual("待复检", judged["状态"])

    def test_judge_takes_either_opening_sentence_on_internal_and_player_visible_alike(self):
        """两句写哪一句都放行（总编判实撞）。

        原来「设计者怎么打开它」只对内部单放行。可取证/事实核查那一类单要真登录图、
        因此标不了 --internal，产出却是屏上那一眼——玩家没有「打开它」这回事，于是判不过去，
        而 judge -h 又写着可以那么写。放宽的是**措辞**，玩家可感知那条纪律一个字没动：
        它靠 --consumer、交板的 真登录图、独图与实机复验守，那几道都比一句措辞硬。
        """
        internal = self.internal_dispatch()
        self.service.claim(internal["编号"], self.worker)
        self.service.submit(internal["编号"], "验证完成", "python -m pytest", "all passed")
        judged, _ = self.service.judge(
            internal["编号"], True, "UI总监", verdict="设计者怎么打开它：双击启动工单台。通过。")
        self.assertEqual("待复检", judged["状态"])

        visible = self.to_judging()
        judged, _ = self.service.judge(
            visible["编号"], True, "UI总监", verdict="设计者怎么打开它：打开工单台。通过。")
        self.assertEqual("待复检", judged["状态"], "玩家可感知单也要认「设计者怎么打开它」")

    def test_judge_still_refuses_a_verdict_with_neither_opening_sentence(self):
        """放宽的只是二选一，不是把这道闸拆了:一句都不写照样拦，且报错要把两句都摆出来。"""
        internal = self.internal_dispatch()
        self.service.claim(internal["编号"], self.worker)
        self.service.submit(internal["编号"], "验证完成", "python -m pytest", "all passed")
        for ticket in (internal, self.to_judging()):
            with self.subTest(ticket=ticket["编号"]):
                with self.assertRaisesRegex(TicketError, "玩家怎么打开它.*设计者怎么打开它"):
                    self.service.judge(ticket["编号"], True, "UI总监", verdict="功能通过。")

    def test_digest_always_has_zero_transfer_section(self):
        self.assertIn("今日转交 0", self.service.digest())


class ImageExportAndBuildTests(TicketTestCase):
    def test_r2_4_save_image_replace_failure_never_exposes_final_path(self):
        image_dir = self.root / "atomic-images"
        target = image_dir / "evidence.jpg"
        with mock.patch.object(store_module.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.service.store.save_image(image_dir, target.name, b"partial-or-complete-bytes")
        self.assertFalse(target.exists())
        leftovers = list(image_dir.iterdir())
        self.assertTrue(all(path.name == "evidence.jpg.tmp" for path in leftovers))

    def test_r1_1_noisy_1280_png_compresses_under_limit(self):
        size = (1280, 720)
        pixels = random.Random(38).randbytes(size[0] * size[1] * 3)
        source = self.root / "noisy-1280.png"
        Image.frombytes("RGB", size, pixels).filter(ImageFilter.GaussianBlur(0.7)).save(source)
        self.assertGreater(source.stat().st_size, 1.5 * 1024 * 1024)

        ticket = self.dispatch("R1-1 质量循环")
        _, record = self.service.attach(ticket["编号"], str(source), "other", self.worker)
        target = self.service.store.images_dir / record["文件名"]
        self.assertLessEqual(target.stat().st_size, MAX_IMAGE_BYTES)

    def test_r1_2_path_bytes_and_remote_compression_have_identical_sha256(self):
        source = self.picture("same-input.png", (1600, 900))
        path_ticket = self.dispatch("R1-2 路径入口")
        bytes_ticket = self.dispatch("R1-2 字节入口")
        _, path_record = self.service.attach(path_ticket["编号"], str(source), "other", self.worker)
        _, bytes_record = self.service.attach_bytes(
            bytes_ticket["编号"], source.read_bytes(), source.name, "other", self.worker
        )
        _, remote_data = RemoteClient._compress(source)
        outputs = (
            (self.service.store.images_dir / path_record["文件名"]).read_bytes(),
            (self.service.store.images_dir / bytes_record["文件名"]).read_bytes(),
            remote_data,
        )
        digests = tuple(hashlib.sha256(data).hexdigest() for data in outputs)
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(digests[0], digests[2])

    def test_r1_3_uncompressible_error_reports_quality_edge_and_size(self):
        source = self.picture("too-large-for-one-byte.png", (128, 96))
        with mock.patch.object(service_module, "MAX_IMAGE_BYTES", 1):
            with self.assertRaisesRegex(
                TicketError, r"已降到质量 50 / 长边 \d+,仍 \d+(?:\.\d+)?KB"
            ):
                service_module.compress_image(source)

    def test_exif_orientation_is_transposed_before_encoding(self):
        source = self.root / "rotated-by-exif.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (40, 80), (15, 30, 45)).save(source, exif=exif)
        _, data = service_module.compress_image(source)
        with Image.open(io.BytesIO(data)) as compressed:
            self.assertEqual((80, 40), compressed.size)

    def test_large_png_is_resized_and_under_limit(self):
        source = self.picture("large.png", (4000, 3000))
        ticket = self.dispatch()
        _, record = self.service.attach(ticket["编号"], str(source), "world", self.worker)
        target = self.service.store.images_dir / record["文件名"]
        self.assertTrue(source.exists())
        self.assertLessEqual(target.stat().st_size, MAX_IMAGE_BYTES)
        with Image.open(target) as image:
            self.assertLessEqual(max(image.size), MAX_IMAGE_EDGE)
        self.assertEqual(".jpg", target.suffix)

    def test_transparent_picture_uses_webp(self):
        source = self.picture("alpha.png", (500, 400), "RGBA")
        ticket = self.dispatch()
        _, record = self.service.attach(ticket["编号"], str(source), "other", self.worker)
        target = self.service.store.images_dir / record["文件名"]
        self.assertEqual(".webp", target.suffix)
        self.assertLessEqual(target.stat().st_size, MAX_IMAGE_BYTES)

    def test_export_writes_to_the_explicit_out_path(self):
        ticket = self.dispatch("导出测试")
        out = self.root / "自定目录" / "导出的任务书.md"
        path = self.service.export(ticket["编号"], out)
        self.assertEqual(out.resolve(), path)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual("# 导出测试", lines[0])
        self.assertTrue(any(ticket["编号"] in line for line in lines[:4]))
        self.assertTrue(any("主界面/面板根" in line for line in lines))

    def test_export_default_path_uses_slot_ticket_and_sanitized_title(self):
        ticket = self.dispatch('标题有 / : * 非法字符')
        path = self.service.export(ticket["编号"], workspace_root=self.root)
        self.assertEqual(
            self.root / "_office" / SLOT / "任务书" / f"{ticket['编号']}_标题有 - - - 非法字符.md",
            path,
        )
        self.assertTrue(path.is_file())

    def test_c_tier_export_uses_restricted_template(self):
        ticket = self.service.create_dispatch(SLOT, "丙档导出", ["D:/source.txt:1-20"], r"D:\output.csv", self.worker, notes=r"把 D:\client\input.csv 转成一张 CSV", task_tier="丙", context_lines=20, deliverables=[str(self.deliverable)], internal=False)
        path = self.service.export(ticket["编号"], self.root / "丙档.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("任务档:丙", text)
        self.assertIn("## §2 只读这些文件", text)
        self.assertIn("D:/source.txt:1-20", text)
        self.assertIn(r"D:\client\input.csv", text)
        self.assertIn(r"D:\output.csv", text)
        self.assertIn("# 合计预算：20 行", text)
        self.assertIn("来源标注为「真登录」", text)
        self.assertIn("做完 §4 全部步骤再交,不中途停下等审", text)
        self.assertNotIn("tasks/模板", text)
        # 模板里给人照抄的命令路径是占位符,导出时必须换成本机真路径,一个都不许剩。
        self.assertNotIn("<ticket.py>", text)
        self.assertNotIn("<remote.env>", text)
        self.assertIn(service_module.CLI_PATH, text)

    def test_export_rejects_the_retired_inbox_names_with_a_clear_message(self):
        result = run_local_cli(["export", "T-000001", "--inbox", "codex"], self.root / "cli-export")
        self.assertEqual(2, result.returncode)
        self.assertIn("--inbox 已废弃，改用 --out", result.stderr)

    def test_bundle_is_byte_identical_on_second_build(self):
        self.dispatch(); self.service.say(SLOT, "设计者", "幂等测试")
        output = self.root / "tickets-bundle.js"
        self.service.build_bundle(output); first = output.read_bytes()
        self.service.build_bundle(output); second = output.read_bytes()
        self.assertEqual(first, second)

    def test_receipt_is_exactly_one_line(self):
        ticket = self.dispatch("一行回执")
        receipt = self.service.receipt(ticket)
        self.assertEqual(f"已进入工单 {ticket['编号']} · 一行回执 · 新建", receipt)
        self.assertNotIn("\n", receipt)


# 判语全文特意写成多行:R1 要的是「原样,不截断、不改写」,单行判语钉不住这一条。
MULTILINE_REWORK_VERDICT = (
    "模型责任：四条守门用例一条都没写,交板证据也只写了「验证完成」。\n"
    "按任务书 R4 补齐五条用例后重交；每条要说清钉的是哪一句。\n"
    "另:基线绿数与完工绿数都要写进 --raw-output,不许只贴一句话。"
)


class VerdictReceiptTests(TicketTestCase):
    """判语随 receipt 打到员工窗。

    开窗指令只有三行、只带任务书路径,判退的判语只留在卡片上;新窗跑 receipt
    看到的也只有标题与状态。就是这么空转两轮的:判语要求「补四条守门用例」,
    新窗看不见,照着旧任务书又交了一模一样的板。
    """

    def to_rework(self, reason: str = "五条用例一条都没写", verdict: str = MULTILINE_REWORK_VERDICT):
        ticket = self.to_judging()
        ticket, _ = self.service.judge(ticket["编号"], False, "UI总监", reason, verdict, "模型")
        return ticket

    def test_rework_receipt_carries_the_whole_verdict_and_the_rework_count(self):
        """第 1 条:判退之后跑 receipt → 判语全文、首行责任归属、返工次数都在。"""
        ticket = self.to_rework()
        receipt = self.service.receipt(ticket)
        # 首行仍是原来那一行,员工一眼看得出单号与状态
        self.assertTrue(receipt.startswith(f"已进入工单 {ticket['编号']} · 测试派单 · 返工"))
        # ①判语原样:整段逐字在,连中间那两行都不许掉
        self.assertIn(MULTILINE_REWORK_VERDICT, receipt)
        for line in MULTILINE_REWORK_VERDICT.splitlines():
            self.assertIn(line, receipt)
        # 首行责任归属要看得见——员工得知道这一次是不是他的错
        self.assertIn("模型责任：", receipt)
        # ②一眼看出这是「上一轮为什么被退」
        self.assertIn("上一轮为什么被退", receipt)
        self.assertIn("第 1 次判退", receipt)
        self.assertIn("判卷:UI总监", receipt)
        # 最近一条返工原因也要带上
        self.assertIn("五条用例一条都没写", receipt)

    def test_rework_receipt_counts_up_on_the_second_bounce(self):
        """返工次数是累计的:第二次判退,receipt 上写的是「第 2 次」、判语换成新那份。"""
        ticket = self.to_rework()
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("second.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "改完再交")
        second = "模型责任：第二次仍然缺那条变异检验。"
        ticket, _ = self.service.judge(ticket["编号"], False, "UI总监", "变异检验没做", second, "模型")
        receipt = self.service.receipt(ticket)
        self.assertIn("第 2 次判退", receipt)
        self.assertIn(second, receipt)
        self.assertNotIn(MULTILINE_REWORK_VERDICT, receipt)

    def test_other_states_keep_the_receipt_at_exactly_one_line(self):
        """第 2 条:非返工态一个字都不加——receipt 每次开工都要跑,不能变成一堵墙。

        故意拿一张**判退过又被重新认领**的单:它的「判语」字段还留着上一轮那份,
        所以这条钉的是「按状态判」,不是「按判语字段空不空判」。
        """
        ticket = self.to_rework()
        # 已认领
        claimed = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual(MULTILINE_REWORK_VERDICT, claimed["判语"])
        receipt = self.service.receipt(claimed)
        self.assertEqual(f"已进入工单 {claimed['编号']} · 测试派单 · 已认领", receipt)
        self.assertNotIn("\n", receipt)
        self.assertNotIn("模型责任", receipt)
        self.assertNotIn("上一轮为什么被退", receipt)
        # 待判
        self.service.attach(ticket["编号"], str(self.picture("again.png")), "world", self.worker)
        judging = self.service.submit(ticket["编号"], "改完再交")
        receipt = self.service.receipt(judging)
        self.assertEqual(f"已进入工单 {judging['编号']} · 测试派单 · 待判", receipt)
        self.assertNotIn("\n", receipt)
        self.assertNotIn("模型责任", receipt)

    def test_cli_prints_the_verdict_and_keeps_the_protocol_tag_on_the_first_line(self):
        """网页与 CLI 都消费服务端同一处生成的文本:CLI 打出来的就是 service.receipt 那一份。

        协议尾巴只贴第一行——落到判语末尾会让人以为那句提示也是判语的一部分。
        """
        ticket = self.to_rework()
        tagged = channel_config.receipt_with_protocol(self.service.receipt(ticket), 3, 3)
        first, _, rest = tagged.partition("\n")
        self.assertTrue(first.endswith("· 客户端协议 3 · 服务端协议 3"), first)
        self.assertIn("返工", first)
        self.assertIn(MULTILINE_REWORK_VERDICT, rest)
        # 单行回执的输出与从前逐字相同
        plain = self.service.receipt(self.dispatch("一行回执"))
        self.assertEqual(
            f"{plain} · 客户端协议 3 · 服务端协议 3",
            channel_config.receipt_with_protocol(plain, 3, 3),
        )


class UnblockLandsOnReworkTests(TicketTestCase):
    """unblock 改落「返工」态,换书不换号(/R3)。

    单子会被阻塞,多半正说明任务书要改;而以前 unblock 把单退回「已认领」,
    任务书换不了,总监只能 void 掉再建新号——断点、返工次数、模型账全丢。
    """

    def test_unblock_lands_on_rework_and_still_remembers_the_blocked_state(self):
        """第 3 条:unblock 之后状态是「返工」,「阻塞前状态」仍记得住原来那一档。"""
        for blocked_at, prepare in (
            ("已认领", lambda t: self.service.claim(t["编号"], self.worker)),
            ("新建", lambda t: t),
        ):
            with self.subTest(阻塞前状态=blocked_at):
                ticket = self.dispatch(f"阻塞落点-{blocked_at}")
                prepare(ticket)
                ticket = self.service.block(ticket["编号"], "缺一张登录图")
                self.assertEqual(blocked_at, ticket["阻塞前状态"])
                ticket = self.service.unblock(ticket["编号"])
                self.assertEqual("返工", ticket["状态"])
                self.assertEqual(blocked_at, ticket["阻塞前状态"])
                self.assertEqual("", ticket["阻塞原因"])
                # 落库的也是同一份,不是只在返回值上好看
                self.assertEqual("返工", self.service.store.load_ticket(ticket["编号"])["状态"])
                self.assertEqual(blocked_at, self.service.store.load_ticket(ticket["编号"])["阻塞前状态"])

    def test_unblock_clears_the_opened_stamp_so_the_queue_shows_it_again(self):
        """★解阻塞必须把「已开窗」戳记一起清掉,否则设计者队列里根本看不到它。

        前端 isOpened() 判的是「已开窗.轮次 == 返工次数」,wantsDispatch() 又要求 !isOpened。
        unblock 从前两个数都不动,0 == 0 恒成立——这张单**不进**「要你传达的」,
        要熬过返工那 8 小时线才从折叠着的「卡住了」段冒出来。
        2026-09-07 设计者当面撞到: 解阻塞后他一直反映没看到重新派发的工单。
        judge --rework 早就清了这个戳记,unblock 漏了——这里按同一条口径钉死。
        """
        ticket = self.dispatch("解阻塞要回到要你传达的")
        self.service.claim(ticket["编号"], self.worker)
        opened, _ = self.service.open_window(ticket["编号"], "设计者", "opus")
        self.assertEqual(0, int(opened["已开窗"]["轮次"]))
        self.service.block(ticket["编号"], "等别位先答")
        unblocked = self.service.unblock(ticket["编号"])
        self.assertEqual("返工", unblocked["状态"])
        # 戳记清了 → 前端那条 轮次==返工次数 再也成立不了 → 单子回到「要你传达的」
        self.assertIsNone(unblocked["已开窗"])
        self.assertIsNone(self.service.store.load_ticket(ticket["编号"])["已开窗"])
        # 返工次数一分不动:它是模型账,不能借着解阻塞悄悄加一次(判退才算判退)
        self.assertEqual(0, self.service.store.load_ticket(ticket["编号"])["返工次数"])
        # 回执要说出来,别让人以为只是换了个状态
        rows = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("事件") == "unblock" and row.get("工单号") == ticket["编号"]
        ]
        self.assertIn("已开窗标记已清", rows[-1]["说明"])

    def test_the_page_gate_is_the_reason_that_stamp_must_go(self):
        """把前端那两条判据抄在这里,免得以后有人改了网页却不知道服务端在迁就它。"""
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn('if(t?.已开窗&&Number(t.已开窗.轮次)===Number(t?.返工次数||0))return true;', script)
        self.assertIn('&&!isOpened(t);', script)

    def test_unblocking_a_ticket_blocked_while_in_rework_stays_rework(self):
        """已经在返工态的单被阻塞后再 unblock,行为不变(仍然是返工),不报错。"""
        ticket = self.to_judging()
        ticket, _ = self.service.judge(ticket["编号"], False, "UI总监", "入口仍会闪一下", REWORK_VERDICT, "模型")
        ticket = self.service.block(ticket["编号"], "等设计者拍板配色")
        self.assertEqual("返工", ticket["阻塞前状态"])
        ticket = self.service.unblock(ticket["编号"])
        self.assertEqual("返工", ticket["状态"])
        self.assertEqual("返工", ticket["阻塞前状态"])

    def test_rework_state_swaps_the_taskbook_without_changing_the_id(self):
        """第 4 条:返工态 set --taskbook 过、单号不变;待判态仍拒。

        白名单里本来就有返工(EDITABLE_STATES),这条钉死它,防止以后有人收紧——
        收紧了「换书不换号」就没了,总监又只能 void 掉重建。
        """
        ticket = self.dispatch("换书不换号")
        self.service.claim(ticket["编号"], self.worker)
        blocked = self.service.block(ticket["编号"], "任务书判据本身写错了")
        reworking = self.service.unblock(blocked["编号"])
        self.assertEqual("返工", reworking["状态"])
        new_path = str(Path(TASKBOOK_DIRECTORY) / f"{ticket['编号']}_改过判据的任务书.md")
        updated, _ = self.service.edit(reworking["编号"], SLOT, taskbook=new_path)
        self.assertEqual(ticket["编号"], updated["编号"])          # 单号不变
        self.assertEqual("返工", updated["状态"])
        self.assertEqual(new_path, updated["任务书路径"])
        # --body 同样在返工态可改
        updated, _ = self.service.edit(reworking["编号"], SLOT, body="改过判据的正文")
        self.assertEqual("改过判据的正文", updated["正文"])
        # 员工 claim 一次即回「已认领」,单号仍旧不变
        claimed = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("已认领", claimed["状态"])
        self.assertEqual(ticket["编号"], claimed["编号"])
        self.assertEqual(new_path, claimed["任务书路径"])
        # 待判态仍然只准改指派给
        judging = self.to_judging()
        with self.assertRaisesRegex(TicketError, "待判态只允许改指派给"):
            self.service.edit(judging["编号"], SLOT, taskbook=new_path)


class ReworkCardTaskbookHintTests(unittest.TestCase):
    """第 5 条:返工态卡片上要有「换书后再认领」与当前任务书路径。

    照现有那批读 tickets.js 源码的钉测写法(ClaimedReminderSourceTests)。
    """

    @classmethod
    def setUpClass(cls) -> None:
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        cls.note = script.split("function reworkNote(t){", 1)[1].split("\nfunction ", 1)[0]

    def test_rework_card_tells_the_director_to_swap_the_taskbook_first(self):
        self.assertIn("换书后再认领", self.note)
        self.assertIn("--taskbook", self.note)
        self.assertIn("set ${esc(t.编号)} --taskbook", self.note)

    def test_rework_card_shows_the_current_taskbook_path(self):
        self.assertIn("const taskbook=dispatchInitialPath(t)", self.note)
        self.assertIn("esc(taskbook)", self.note)
        self.assertIn("还没填任务书路径", self.note)

    def test_rework_card_does_not_reassemble_the_verdict_text_in_the_frontend(self):
        """判语全文归服务端 receipt 那一处生成;前端再拼一份,两处必漂。"""
        self.assertNotIn("上一轮为什么被退", self.note)
        self.assertNotIn("t.判语", self.note)


class ClaimedReminderSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        cls.reminder = script.split('const text=t.状态==="已认领"', 1)[1].split(
            ':(t.状态==="新建"&&isOpened(t))', 1,
        )[0]

    def test_claimed_internal_reminder_has_submit_verification_and_raw_output(self):
        self.assertIn('submit ${t.编号} --verify-command', self.reminder)
        self.assertIn('--raw-output', self.reminder)

    def test_claimed_player_reminder_has_world_attachment(self):
        self.assertIn('attach ${t.编号}', self.reminder)
        self.assertIn('--origin world', self.reminder)

    def test_claimed_reminder_checks_receipt_first(self):
        self.assertIn('先核通道:python', self.reminder)
        self.assertIn('receipt ${t.编号}', self.reminder)

    def test_claimed_reminder_uses_the_configured_cli_path(self):
        # 四条给人照抄的命令都走同一个 CLI_PATH(位表配置里的「命令行」,正斜杠),
        # 不再写死某台机器的 Windows 路径——模板字符串里也就不会再有反斜杠被吃掉一半的事。
        self.assertEqual(4, self.reminder.count("python ${CLI_PATH} "))
        self.assertNotRegex(self.reminder, r"[A-Za-z]:\\")


class FixtureAndInterfaceTests(unittest.TestCase):
    def test_move_verify_fixture_has_equal_hashes(self):
        path = repository_file_or_skip(self, "review", "ticket-system", "MOVE-VERIFY.md")
        rows = re.findall(r"^\| ([^|]+) \| ([0-9a-f]{32}) \| ([0-9a-f]{32}) \| 同 \|$", path.read_text(encoding="utf-8"), re.M)
        self.assertEqual(22, len(rows))
        self.assertEqual(21, sum(name.endswith(".json") for name, _, _ in rows))
        self.assertTrue(all(before == after for _, before, after in rows))

    def test_cli_json_output_can_appear_after_subcommand(self):
        with tempfile.TemporaryDirectory() as root:
            result = run_local_cli(["staff", "list", "--json"], Path(root) / "tickets")
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual([], payload["result"])

    def test_web_page_has_five_views_local_files_and_all_gate_messages(self):
        html = (ROOT / "tools" / "browser" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        for label in ("总监位", "设计者队列", "日览", "搜索", "新单"):
            self.assertIn(label, html)
        self.assertNotRegex(html, r"https?://")
        for marker in ("showDirectoryPicker", "indexedDB", "tickets-bundle.js", "真登录", "判卷人不能与执行员工", "复检人必须与执行员工、判卷人都不同", "只能由${CONDUCTOR_SLOT}", "名册里格式正确的在岗员工", "toBlob"):
            self.assertIn(marker, html + script)
        for marker in ("转给${esc(CONDUCTOR_SLOT)}", "转给复检", "转给指定总监", "转给设计者", "data-transfer-reason", "一、这是什么", "交付项"):
            self.assertIn(marker, html + script)
        for marker in ("非玩家可感知", "验证命令", "原样输出", "判语", "verify_command"):
            self.assertIn(marker, html + script)
        self.assertNotIn("我处理不了，转总工单", html + script)

    def test_new_transfer_ask_and_judge_missing_parameters_use_plain_errors(self):
        with tempfile.TemporaryDirectory() as root:
            commands = (["new"], ["transfer", "T-000001"], ["ask"], ["judge", "T-000001", "--pass", "--by", "总编"])
            for command in commands:
                with self.subTest(command=command):
                    result = run_local_cli(command, Path(root) / "tickets")
                    self.assertEqual(2, result.returncode)
                    self.assertTrue(result.stderr.startswith("拦下:"), result.stderr)
                    self.assertNotIn("usage:", result.stderr)
                    self.assertNotIn("required", result.stderr)


class TaskTierAndModelScoreTests(TicketTestCase):
    def test_dispatch_requires_explicit_tier(self):
        with self.assertRaisesRegex(TicketError, "任务档必填"):
            self.service.create_dispatch(SLOT, "缺任务档", ["DECISIONS.md:1"], "主界面", self.worker)

    def test_c_tier_requires_budget_at_most_2000(self):
        before = self.service.store.read_json(self.service.store.counter_path)["最后编号"]
        with self.assertRaisesRegex(TicketError, "必须填写上下文预算"):
            self.service.create_dispatch(SLOT, "缺预算", ["DECISIONS.md:1"], "主界面", self.worker, task_tier="丙", deliverables=[str(self.deliverable)])
        with self.assertRaisesRegex(TicketError, "超过 2000"):
            self.service.create_dispatch(SLOT, "预算过大", ["DECISIONS.md:1"], "主界面", self.worker, task_tier="丙", context_lines=2001, deliverables=[str(self.deliverable)])
        self.assertEqual(before, self.service.store.read_json(self.service.store.counter_path)["最后编号"])
        ticket = self.service.create_dispatch(SLOT, "预算合规", ["DECISIONS.md:1"], "主界面", self.worker, task_tier="丙", context_lines=2000, deliverables=[str(self.deliverable)], internal=False)
        self.assertEqual(("丙", 2000), (ticket["任务档"], ticket["上下文预算"]))

    def test_slots_have_editable_model_policy(self):
        slots = self.service.store.read_json(self.service.store.slots_path)
        self.assertEqual(["sol", "opus", "fable"], slots["主力模型集合"])
        self.assertEqual({"同位": 3, "全项目": 5}, slots["停用阈值"])
        self.assertTrue(all(row["主力模型"] for row in slots["总监位"]))

    def test_r4_actual_model_is_not_required_when_dispatch_is_created(self):
        ticket = self.dispatch("建单不替设计者选模型")
        self.assertEqual("乙", ticket["任务档"])
        self.assertEqual("", ticket["实际模型"])

    def test_r4_roster_has_all_sol_levels_and_retired_spark_cannot_be_new(self):
        roster = self.service.store.read_json(self.service.store.slots_path)["模型名册"]
        sol = next(row for row in roster if row["模型"] == "sol")
        spark = next(row for row in roster if row["模型"] == "codex-spark")
        self.assertEqual(["high", "middle", "low"], sol["可选档位"])
        self.assertEqual("退役", spark["状态"])
        self.assertEqual("", self.service.staff_new(OTHER_SLOT, "sol low")["提示"])

        staff = self.service.store.load_staff()
        historical = staff["总监位"][SLOT]["员工"][0]
        historical["工具/窗类型"] = "codex-spark"
        self.service.store.save_staff(staff)
        with self.assertRaisesRegex(TicketError, "已退役"):
            self.service.staff_new(SLOT, "codex-spark")
        self.assertEqual("codex-spark", self.service.find_staff(self.worker)[1]["工具/窗类型"])

    def test_r4_sol_low_warns_for_an_a_tier_ticket_but_does_not_block(self):
        ticket = self.service.create_dispatch(
            SLOT, "甲档用低档模型提醒", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="甲", deliverables=[str(self.deliverable)], internal=False,
        )
        updated, warning = self.service.open_window(ticket["编号"], "设计者", "sol low")
        self.assertEqual("sol low", updated["实际模型"])
        self.assertIn("低于本单甲档", warning)

    def test_non_main_model_warns_but_is_registered(self):
        member = self.service.staff_new(OTHER_SLOT, "codex")
        self.assertIn("不在主力模型名册里", member["提示"])
        self.assertEqual("在岗", self.service.find_staff(member["员工名"])[1]["状态"])

    def test_pending_tool_registers_without_warning_or_model_rate_and_ban_score(self):
        member = self.service.staff_new(OTHER_SLOT, "待定")
        self.assertEqual("", member["提示"])
        self.assertEqual("待定", self.service.find_staff(member["员工名"])[1]["工具/窗类型"])
        ticket = self.service.create_dispatch(
            OTHER_SLOT, "待定模型不计分", ["DECISIONS.md:测试"], "主界面/面板根",
            member["员工名"], task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
        )
        self.service.claim(ticket["编号"], member["员工名"])
        self.service.attach(ticket["编号"], str(self.picture("pending-world.png")), "world", member["员工名"])
        self.service.submit(ticket["编号"], "登录后仍有问题")
        self.service.judge(ticket["编号"], False, "UI总监", "判退", REWORK_VERDICT, "模型")
        self.assertFalse(any(row["模型"] == "待定" for row in self.service.model_statistics()))
        self.assertNotIn("待定", self.service.store.load_staff()["模型记分"])

    def test_three_same_slot_reworks_warn_but_no_longer_ban_the_model(self):
        """ 乙:到停用线只通知,不自动停用;停不停由总编落 D9。

        改之前这里断言第 3 次判退后 staff_new 报「已停用」——那正是 2026-09-05 两次误停 sol 的机制。
        用非主力模型 glm-5.3:R1.5之后主力模型到线只作质量提示,不再有「已到停用线」措辞。
        """
        last_warning = ""
        for index in range(3):
            ticket = self.dispatch(f"判退{index + 1}")
            self.service.open_window(ticket["编号"], "设计者", "glm-5.3")
            self.service.claim(ticket["编号"], self.worker)
            self.service.attach(ticket["编号"], str(self.picture(f"world-{index}.png")), "world", self.worker)
            self.service.submit(ticket["编号"], "登录后仍有问题")
            _, last_warning = self.service.judge(
                ticket["编号"], False, "UI总监", f"第{index + 1}次判退", REWORK_VERDICT, "模型",
            )
        self.assertIn("累计判退 3 次", last_warning)
        self.assertIn("已到停用线", last_warning)
        self.assertIn("自动停用已关", last_warning)
        bans = self.service.store.load_staff().get("模型停用", {})
        self.assertEqual([], bans.get("全项目", []))
        self.assertEqual([], bans.get("按位", {}).get(SLOT, []))
        # 不再被拦:新窗照开
        self.assertEqual("前端·页面接线-02", self.service.staff_new(SLOT, "glm-5.3")["员工名"])

    def test_first_review_notice_and_model_rate(self):
        ticket = self.to_judging()
        _, notice = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertIn("首检从严", notice)
        stats = self.service.model_statistics()
        sol = next(row for row in stats if row["模型"] == "sol-未标")
        self.assertEqual((1, 1, 0, "100.0%"), (sol["交板数"], sol["判过"], sol["判退"], sol["合格率"]))

    def test_open_window_writes_actual_model_to_staff_and_ticket_then_stats_use_it(self):
        ticket = self.service.create_dispatch(
            SLOT, "已开窗写回真实模型", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="甲", deliverables=[str(self.deliverable)], internal=False,
        )
        updated, warning = self.service.open_window(ticket["编号"], "设计者", "glm-5.3")
        self.assertEqual("glm-5.3", updated["实际模型"])
        self.assertEqual("glm-5.3", self.service.find_staff(self.worker)[1]["工具/窗类型"])
        self.assertIn("低于本单甲档", warning)
        _, repeated_warning = self.service.open_window(ticket["编号"], "设计者", "glm-5.3")
        self.assertEqual("", repeated_warning)

        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("actual-model.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "实际模型统计")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        stats = self.service.model_statistics()
        actual = next(row for row in stats if row["模型"] == "glm-5.3")
        self.assertEqual((1, 1, "100.0%"), (actual["交板数"], actual["判过"], actual["合格率"]))
        self.assertFalse(any(row["模型"] == "sol" for row in stats))

    def test_open_window_keeps_confirm_before_model_choice_and_r_prefixed_stamp(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        confirm_at = script.index("if(next&&!confirm(`确认已经把")
        prompt_at = script.index("const chosen=prompt(`请选择本次实际模型与档位")
        stamp_at = script.index("lsSet(`deskOpened:${id}`,reworkStamp(ticket))")
        api_at = script.index("op:'open-window'")
        # 二次确认与模型选择的先后不许动。
        self.assertLess(confirm_at, prompt_at)
        self.assertLess(prompt_at, stamp_at)
        # 起顺序是「先落本机标记、再把写请求丢进后台队列」:
        # 原来这里要 await 写请求 + await refresh(),refresh 会重拉 409 张单与 13 个对话线共 15 个请求,
        # 设计者点一下就干等(他 2026-09-04 报「点开窗之后越来越卡」)。
        # 改成乐观更新之后,标记必须在 api 之前;换来的风险由回滚兜——所以下面那条断言不能删。
        self.assertLess(stamp_at, api_at)
        self.assertIn("onFail:()=>{lsDel(`deskOpened:${id}`)", script)
        self.assertIn("app.data.items[index]=saved", script)
        self.assertIn('function reworkStamp(t){return "r"+String(t?.返工次数 ?? 0)}', script)

    def test_actual_model_is_snapshotted_per_rework_round(self):
        ticket = self.dispatch("返工换模型不改写旧成绩")
        self.service.open_window(ticket["编号"], "设计者", "glm-5.3")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("round-one.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "第一轮")
        self.service.judge(ticket["编号"], False, "UI总监", "第一轮判退", REWORK_VERDICT, "模型")

        self.service.open_window(ticket["编号"], "设计者", "sol high")
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "第二轮")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        stats = {row["模型"]: row for row in self.service.model_statistics()}
        self.assertEqual((1, 0, 1), (stats["glm-5.3"]["交板数"], stats["glm-5.3"]["判过"], stats["glm-5.3"]["判退"]))
        self.assertEqual((1, 1, 0), (stats["sol-high"]["交板数"], stats["sol-high"]["判过"], stats["sol-high"]["判退"]))


class DeskOpenedServerFieldTests(TicketTestCase):
    def opened_to_judging(self):
        ticket = self.dispatch("服务端已开窗字段")
        opened, _ = self.service.open_window(ticket["编号"], "设计者", "sol high")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture("opened-world.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "服务端已开窗字段验收")
        return opened

    def test_open_window_saves_current_round_time_and_actual_model(self):
        ticket = self.dispatch("开窗写服务端")
        updated, _ = self.service.open_window(ticket["编号"], "设计者", "sol high")
        marker = updated["已开窗"]
        self.assertEqual(ticket["返工次数"], marker["轮次"])
        self.assertEqual("sol high", marker["实际模型"])
        self.assertEqual(marker, self.service.store.load_ticket(ticket["编号"])["已开窗"])
        datetime.fromisoformat(marker["时间"])

    def test_rework_clears_opened_marker(self):
        opened = self.opened_to_judging()
        self.assertIsNotNone(opened["已开窗"])
        reworked, _ = self.service.judge(opened["编号"], False, "UI总监", "字段未清", REWORK_VERDICT)
        self.assertIsNone(reworked["已开窗"])
        self.assertIsNone(self.service.store.load_ticket(opened["编号"])["已开窗"])

    def test_legacy_ticket_without_opened_key_loads_as_null_without_migration(self):
        for name, store in (
            ("files", TicketStore(self.root / "legacy-files")),
            ("sqlite", SqliteStore(self.root / "legacy-sqlite" / "tickets.sqlite")),
        ):
            with self.subTest(store=name):
                store.ensure()
                legacy = model.new_ticket_record(
                    "T-000001", "派单", SLOT, "旧单", "总编", assign=self.worker,
                    sources=["DECISIONS.md:旧单"], consumer="主界面/面板根", task_tier="乙",
                )
                legacy.pop("已开窗")
                store.save_ticket(legacy, "new", "总编", "旧格式写入")
                loaded = store.load_ticket(legacy["编号"])
                listed = store.list_tickets()[0]
                self.assertIn("已开窗", loaded)
                self.assertIsNone(loaded["已开窗"])
                self.assertIsNone(listed["已开窗"])

    def test_open_and_rework_event_details_explain_marker_changes(self):
        ticket = self.opened_to_judging()
        self.service.judge(ticket["编号"], False, "UI总监", "重做", REWORK_VERDICT)
        rows = [row for row in self.service.store.read_jsonl(self.service.store.log_path) if row["工单号"] == ticket["编号"]]
        opened = next(row for row in rows if row["事件"] == "window-opened")
        reworked = next(row for row in rows if row["事件"] == "judge-rework")
        self.assertIn("已开窗·第 0 轮", opened["说明"])
        self.assertIn("已开窗标记已清,等设计者重新开窗", reworked["说明"])

    def test_browser_checks_server_marker_before_local_storage_fallback(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        start = script.index("function isOpened(t){")
        end = script.index("\n}", start)
        body = script[start:end]
        server_at = body.index("t?.已开窗")
        explicit_at = body.index('hasOwnProperty.call(t,"已开窗")')
        local_at = body.index("lsGet(`deskOpened:${t.编号}`)")
        self.assertLess(server_at, explicit_at)
        self.assertLess(explicit_at, local_at)
        self.assertIn("return false", body[explicit_at:local_at])
        self.assertIn('return v==="0"&&Number(t?.返工次数 ?? 0)===0', body[local_at:])

    def test_rework_card_says_server_marker_was_cleared(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn("已开窗标记已清,请重新开窗", script)


def _self_signed_cert(directory: Path) -> tuple[str, str]:
    """给 TLS 用例造一张自签证书。造不出来(缺 cryptography)就让调用方跳过。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now()
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


class TlsAcceptLoopTests(TicketTestCase):
    """TLS 握手不许在 accept 主循环里做,否则一个不握手的客户端能钉死整台服务。

    2026-09-05 线上真事故:服务 systemd 显示 active、进程还在,但 8443 全超时。
    ss 显示 Recv-Q 6 / Send-Q 5(accept 队列满),Tasks 只剩 1(一个工作线程都没起)。
    根因是 serve() 把监听套接字整个 wrap_socket 成了 SSLSocket,于是握手在 accept 那一步、
    主循环线程里做,而且没有超时;ThreadingHTTPServer 的多线程要等 accept 返回才轮得到。
    """

    def setUp(self) -> None:
        super().setUp()
        try:
            self.cert, self.key = _self_signed_cert(self.root)
        except Exception as exc:  # pragma: no cover - 只在缺 cryptography 的机器上走到
            self.skipTest(f"造不出自签证书,跳过 TLS 用例:{exc}")
        handler = partial(TicketRequestHandler, directory=str(ROOT / "tools" / "browser"))
        self.server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        # 和 serve() 里的写法保持一致:挂 ssl_context，不包监听套接字。
        self.server.ssl_context = context
        self.server.connection_timeout = 5
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.dead: list[socket.socket] = []

    def tearDown(self) -> None:
        for sock in self.dead:
            with contextlib.suppress(OSError):
                sock.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def https_get(self, path: str, timeout: float = 8.0) -> tuple[int, bytes]:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=timeout, context=context)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_tls_serves_normally(self):
        status, _ = self.https_get("/api/tickets")
        self.assertEqual(200, status)

    def test_clients_that_never_finish_the_handshake_do_not_wedge_the_accept_loop(self):
        """连上来就不吭声的客户端,开满旧 backlog 的两倍,正常请求仍要能过。

        改之前这一条必挂:那些连接会卡在 accept 里的握手上,accept 队列排满之后
        新连接的 SYN 被内核丢掉,https_get 只会超时。
        """
        for _ in range(12):  # 旧 backlog 是 5,这里给它两倍多
            sock = socket.socket()
            sock.settimeout(5)
            sock.connect(("127.0.0.1", self.port))
            self.dead.append(sock)  # 连上就不发任何字节，握手永远开不了头

        status, _ = self.https_get("/api/tickets")
        self.assertEqual(200, status)

    def test_plain_http_probe_on_the_tls_port_does_not_kill_the_service(self):
        """拿明文 HTTP 去捅 HTTPS 端口(扫描器天天干),服务要照常活着。"""
        probe = socket.socket()
        probe.settimeout(5)
        probe.connect(("127.0.0.1", self.port))
        probe.sendall(b"GET / HTTP/1.0\r\n\r\n")
        with contextlib.suppress(OSError):
            probe.recv(64)
        probe.close()

        status, _ = self.https_get("/api/tickets")
        self.assertEqual(200, status)

    def test_backlog_is_big_enough_for_every_director_window(self):
        """默认 5 太小:11 个总监窗 + 网页轮询,排满之后客户端看到的是超时,不是拒绝连接。"""
        self.assertGreaterEqual(TicketHTTPServer.request_queue_size, 128)

class HttpServiceTests(TicketTestCase):
    def setUp(self) -> None:
        super().setUp()
        handler = partial(TicketRequestHandler, directory=str(ROOT / "tools" / "browser"))
        self.server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        sent_headers = dict(headers or {})
        if body is not None:
            sent_headers["Content-Type"] = "application/json; charset=utf-8"
        connection.request(method, path, body=body, headers=sent_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def test_get_tickets_over_temporary_port(self):
        ticket = self.dispatch("HTTP 列表")
        status, payload = self.request("GET", "/api/tickets")
        self.assertEqual(200, status)
        self.assertEqual(channel_config.PROTOCOL_VERSION, payload["server_protocol"])
        self.assertEqual(ticket["编号"], payload["result"][0]["编号"])
        self.assertEqual(ticket["状态进入时间"], payload["result"][0]["状态进入时间"])
        self.assertEqual([], payload["result"][0]["开窗指令"])

    def test_http_open_window_writes_actual_model_before_browser_marks_it(self):
        ticket = self.dispatch("HTTP 已开窗")
        status, payload = self.request("POST", "/api/action", {
            "op": "open-window", "ticket": ticket["编号"], "by": "设计者", "actual_model": "sol high",
        })
        self.assertEqual(200, status)
        self.assertEqual("sol high", payload["result"]["工单"]["实际模型"])
        self.assertEqual("sol high", self.service.find_staff(self.worker)[1]["工具/窗类型"])

    def test_open_window_rework_then_ticket_api_returns_cleared_marker_end_to_end(self):
        ticket = self.dispatch("端到端清已开窗")
        self.service.claim(ticket["编号"], self.worker)
        status, opened = self.request("POST", "/api/action", {
            "op": "open-window", "ticket": ticket["编号"], "by": "设计者", "actual_model": "sol high",
        })
        self.assertEqual(200, status)
        self.assertIsNotNone(opened["result"]["工单"]["已开窗"])
        self.service.attach(ticket["编号"], str(self.picture("end-to-end-opened.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "端到端判退前交板")
        status, _ = self.request("POST", "/api/action", {
            "op": "judge", "ticket": ticket["编号"], "by": "UI总监", "passed": False,
            "reason": "端到端判退", "verdict": REWORK_VERDICT,
        })
        self.assertEqual(200, status)
        status, listing = self.request("GET", "/api/tickets")
        self.assertEqual(200, status)
        shown = next(row for row in listing["result"] if row["编号"] == ticket["编号"])
        self.assertIsNone(shown["已开窗"])

    def test_http_set_taskbook_writes_the_server_ticket(self):
        ticket = self.dispatch("浏览器补任务书")
        path = rf"D:\project\_office\{SLOT}\任务书\{ticket['编号']}_补路径.md"
        status, payload = self.request("POST", "/api/action", {
            "op": "set", "ticket": ticket["编号"], "taskbook": path, "by": SLOT,
        })
        self.assertEqual(200, status)
        self.assertEqual(path, payload["result"]["工单"]["任务书路径"])
        self.assertEqual(path, self.service.store.load_ticket(ticket["编号"])["任务书路径"])
        _, listing = self.request("GET", "/api/tickets")
        shown = next(row for row in listing["result"] if row["编号"] == ticket["编号"])
        # 开窗指令 = 认领行 + 执行行 + 提示行 = 3 行(设计者 2026-09-14 撤掉那两行)。
        self.assertEqual(3, len(shown["开窗指令"]))
        self.assertIn(f" claim {ticket['编号']} --by {self.worker}", shown["开窗指令"][0])

    def test_http_internal_dispatch_submits_with_command_output_and_no_picture(self):
        status, payload = self.request("POST", "/api/action", {
            "op": "new", "slot": SLOT, "title": "HTTP 内部单", "source": ["service.py"],
            "consumer": "工单台", "deliverables": [str(self.deliverable)], "assign": self.worker,
            "tier": "乙", "internal": True, "by": SLOT,
        })
        self.assertEqual(200, status)
        ticket_id = payload["result"]["编号"]
        self.assertTrue(payload["result"]["非玩家可感知"])
        self.assertEqual(200, self.request("POST", "/api/action", {"op": "claim", "ticket": ticket_id, "by": self.worker})[0])
        status, payload = self.request("POST", "/api/action", {
            "op": "submit", "ticket": ticket_id, "verify_command": "python -m pytest",
            "raw_output": "48 passed", "evidence": "接口验证完成",
        })
        self.assertEqual(200, status)
        self.assertEqual("待判", payload["result"]["状态"])

    def test_post_action_hard_gate_returns_plain_reason(self):
        ticket = self.service.create_dispatch(SLOT, "缺真源", [], "主界面/面板根", self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=False)
        status, payload = self.request("POST", "/api/action", {"op": "claim", "ticket": ticket["编号"], "by": self.worker})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("真源指针还没填", payload["reason"])

    def test_unknown_option_from_newer_client_reports_both_real_protocols(self):
        status, payload = self.request("POST", "/api/cli", {
            "argv": ["list", "--server-does-not-know"], "client_protocol": 99,
        })
        self.assertEqual(400, status)
        self.assertEqual(channel_config.PROTOCOL_VERSION, payload["server_protocol"])
        self.assertEqual(
            "你的客户端比服务器新,服务器还没上这一版:"
            f"客户端 99 / 服务端 {channel_config.PROTOCOL_VERSION};"
            "请等平台位上服,或改用并线前的客户端。",
            payload["reason"],
        )

    def test_unknown_option_without_client_protocol_keeps_old_argparse_message(self):
        status, payload = self.request("POST", "/api/cli", {
            "argv": ["list", "--server-does-not-know"],
        })
        self.assertEqual(400, status)
        # 不认识的 --选项,尾部多一行版本差提示
        # (新参数常先上服后并 main,别让人当成自己写错)。
        # ★头部现在把 argparse 自己那句话带上了(哪个参数不对),原来整个吞掉,
        #   只留「多半是检出旧」这一种解释——本位为此白跑两趟换检出。
        reason = payload["reason"]
        self.assertTrue(reason.startswith("ticket.py 参数不对"), reason)
        self.assertIn("--server-does-not-know", reason, "要说得出是哪个参数不对")
        self.assertIn("git pull 主检出", reason)

    def test_token_requires_header(self):
        self.server.token = "one-run-secret"
        status, payload = self.request("GET", "/api/tickets")
        self.assertEqual(401, status)
        self.assertEqual("未获授权：请提供正确的 X-Ticket-Token。", payload["reason"])
        status, payload = self.request("GET", "/api/tickets", headers={"X-Ticket-Token": "one-run-secret"})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_upload_4000_by_3000_is_bounded(self):
        ticket = self.dispatch("大图上传")
        stream = io.BytesIO()
        Image.new("RGB", (4000, 3000), (50, 100, 150)).save(stream, "PNG")
        status, payload = self.request(
            "POST",
            "/api/upload",
            {"ticket": ticket["编号"], "filename": "4000x3000.png", "origin": "other", "by": self.worker, "base64": base64.b64encode(stream.getvalue()).decode("ascii")},
        )
        self.assertEqual(200, status)
        target = self.service.store.images_dir / payload["result"]["图片"]["文件名"]
        self.assertLessEqual(target.stat().st_size, MAX_IMAGE_BYTES)
        with Image.open(target) as image:
            self.assertLessEqual(max(image.size), MAX_IMAGE_EDGE)

    def test_cli_and_http_write_same_ticket_without_overwrite(self):
        ticket = self.dispatch("并发同单")
        source = self.picture("concurrent.png", (900, 600))
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def cli_attach() -> None:
            barrier.wait()
            results["cli"] = run_local_cli(
                ["attach", ticket["编号"], str(source), "--origin", "other", "--by", self.worker], self.service.store.root,
            )

        def http_block() -> None:
            barrier.wait()
            results["http"] = self.request("POST", "/api/action", {"op": "block", "ticket": ticket["编号"], "reason": "并发验证", "by": "总编"})

        threads = [threading.Thread(target=cli_attach), threading.Thread(target=http_block)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        cli = results["cli"]
        status, _ = results["http"]
        final = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual(0, cli.returncode, cli.stderr)
        self.assertEqual(200, status)
        self.assertEqual("阻塞", final["状态"])
        self.assertEqual(1, len(final["图片列表"]))
        self.assertEqual(3, final["事件序号"])
        print("CONCURRENT ORIGINAL " + json.dumps({"http_status": status, "cli_exit": cli.returncode, "cli_stdout": cli.stdout.strip(), "final_state": final["状态"], "image_count": len(final["图片列表"]), "event_sequence": final["事件序号"]}, ensure_ascii=False))


class AuthHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SqliteStore(self.root / "db" / "tickets.sqlite")
        self.service = TicketService(self.store)
        self.auth = AccountManager(self.store.database)
        self.username = "owner"
        self.auth.init_admin(self.username)
        handler = partial(TicketRequestHandler, directory=str(ROOT / "tools" / "browser"))
        self.server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "", self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        sent = dict(headers or {})
        if body is not None:
            sent["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), raw)
        connection.close()
        return result

    def test_setup_login_cookie_and_personal_token(self):
        status, _, _ = self.request("GET", "/setup")
        self.assertEqual(200, status)
        password = secrets.token_urlsafe(24)
        status, headers, _ = self.request("POST", "/auth/setup", {"username": self.username, "password": password})
        self.assertEqual(200, status)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        session_cookie = cookie.split(";", 1)[0]
        self.assertEqual(404, self.request("GET", "/setup")[0])
        self.assertEqual(200, self.request("GET", "/api/tickets", headers={"Cookie": session_cookie})[0])
        status, _, raw = self.request("POST", "/api/token", {"op": "generate"}, {"Cookie": session_cookie})
        token = json.loads(raw.decode("utf-8"))["result"]["token"]
        self.assertEqual(200, self.request("GET", "/api/tickets", headers={"X-Ticket-Token": token})[0])
        status, token_headers, _ = self.request("POST", "/auth/token-login", {"token": token})
        self.assertEqual(200, status)
        self.assertIn("HttpOnly", token_headers["Set-Cookie"])
        self.request("POST", "/api/token", {"op": "revoke"}, {"Cookie": session_cookie})
        self.assertEqual(401, self.request("GET", "/api/tickets", headers={"X-Ticket-Token": token})[0])

    def test_no_token_wrong_token_and_twenty_failures_ban(self):
        self.assertEqual(401, self.request("GET", "/api/tickets")[0])
        for _ in range(19):
            self.assertEqual(401, self.request("GET", "/api/tickets", headers={"X-Ticket-Token": secrets.token_urlsafe(8)})[0])
        self.assertEqual(429, self.request("GET", "/api/tickets")[0])

    def test_ten_login_failures_ban_for_ten_minutes(self):
        for _ in range(10):
            status, _, _ = self.request(
                "POST", "/auth/login", {"username": self.username, "password": secrets.token_urlsafe(12)}
            )
            self.assertEqual(401, status)
        self.assertEqual(
            429,
            self.request("POST", "/auth/login", {"username": self.username, "password": secrets.token_urlsafe(12)})[0],
        )


class RemoteClientTests(TicketTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dispatch("远程模式")
        self.sqlite = SqliteStore(self.root / "remote" / "db" / "tickets.sqlite")
        self.sqlite.import_files(self.service.store.root)
        self.remote_service = TicketService(self.sqlite)
        self.service_token = secrets.token_urlsafe(32)
        handler = partial(TicketRequestHandler, directory=str(ROOT / "tools" / "browser"))
        self.server = TicketHTTPServer(
            ("127.0.0.1", 0), handler, self.remote_service, self.service_token, AccountManager(self.sqlite.database)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token_file = self.root / "remote.token"
        self.token_file.write_text(self.service_token, encoding="utf-8")
        self.client = RemoteClient(
            f"http://127.0.0.1:{self.server.server_address[1]}", str(self.token_file)
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    @contextlib.contextmanager
    def protocol_zero_server(self):
        class ProtocolZeroHandler(TicketRequestHandler):
            def _cli(handler, data):
                from tools.tickets.ticket import execute, parser

                argv = data.get("argv")
                handler.server.cli_requests.append(list(argv) if isinstance(argv, list) else argv)
                if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
                    raise TicketError("远程命令参数必须是字符串列表。")
                # 模拟协议 0：不读取 client_protocol，旧 parser 也不认识这个新开关。
                if "--taskbook-client-checked" in argv:
                    raise TicketError("ticket.py 参数不对，请检查命令写法。")
                args = parser().parse_args(argv)
                payload, text = execute(args, handler.server.service)
                return {"payload": payload, "text": text}

        handler = partial(ProtocolZeroHandler, directory=str(ROOT / "tools" / "browser"))
        server = TicketHTTPServer(("127.0.0.1", 0), handler, self.remote_service, self.service_token)
        server.cli_requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = RemoteClient(f"http://127.0.0.1:{server.server_address[1]}", str(self.token_file))
        try:
            with mock.patch.object(http_server_module, "PROTOCOL_VERSION", 0):
                yield client, server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_remote_read_output_matches_service_output(self):
        from tools.tickets.ticket import execute, parser

        _, local_text = execute(parser().parse_args(["list"]), self.remote_service)
        _, remote_text = self.client.execute(["list"])
        self.assertEqual(local_text, remote_text)

    def test_env_probe_and_receipt_report_the_same_live_server_protocol(self):
        ticket = self.remote_service.list_tickets()[0]
        environment = clean_environment(
            self.service.store.root,
            TICKET_REMOTE=f"http://127.0.0.1:{self.server.server_address[1]}",
            TICKET_TOKEN_FILE=str(self.token_file),
        )
        probed = subprocess.run(
            [*CLI, "env", "--probe"], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8",
        )
        receipt = subprocess.run(
            [*CLI, "receipt", ticket["编号"]], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(0, probed.returncode, probed.stderr)
        self.assertEqual(0, receipt.returncode, receipt.stderr)
        expected = f"服务端协议 {channel_config.PROTOCOL_VERSION}"
        self.assertIn(expected, probed.stdout)
        self.assertIn(expected, receipt.stdout)

    def test_protocol_handshake_uses_real_http_request_and_response_envelope(self):
        seen: dict[str, object] = {}
        original = TicketRequestHandler._cli

        def recording_cli(handler, data):
            seen.update(data)
            return original(handler, data)

        with mock.patch.object(TicketRequestHandler, "_cli", recording_cli):
            self.client.execute(["list"])
        self.assertEqual(channel_config.PROTOCOL_VERSION, seen["client_protocol"])
        self.assertEqual(channel_config.PROTOCOL_VERSION, self.client.server_protocol)
        self.client.server_protocol = 0
        with self.assertRaises(TicketError):
            self.client.execute(["list", "--server-does-not-know"])
        self.assertEqual(channel_config.PROTOCOL_VERSION, self.client.server_protocol)

    def test_response_without_server_protocol_is_cached_as_protocol_zero(self):
        class Response:
            status = 200

            @staticmethod
            def read():
                return b'{"ok":true,"result":{"payload":[],"text":""}}'

        class Connection:
            def request(self, method, path, body=None, headers=None):
                pass

            @staticmethod
            def getresponse():
                return Response()

            @staticmethod
            def close():
                pass

        with mock.patch.object(self.client, "_connection", return_value=Connection()):
            self.client.execute(["list"])
        self.assertEqual(0, self.client.server_protocol)

    def test_protocol_zero_server_downgrades_taskbook_check_once_with_full_warning(self):
        ticket = self.remote_service.list_tickets()[0]
        taskbook = self.root / "协议零任务书.md"
        taskbook.write_text("# 已在客户端核过\n", encoding="utf-8")
        argv = [
            "set", ticket["编号"], "--taskbook", str(taskbook), "--by", SLOT,
            "--taskbook-client-checked",
        ]
        stderr = io.StringIO()
        with self.protocol_zero_server() as (client, server), contextlib.redirect_stderr(stderr):
            payload, _ = client.execute(argv)
            stored = self.remote_service.store.load_ticket(ticket["编号"])
            self.assertIn("--taskbook-client-checked", server.cli_requests[0])
            self.assertNotIn("--taskbook-client-checked", server.cli_requests[1])
            self.assertEqual(2, len(server.cli_requests))
            self.assertEqual("待回核", stored["任务书校验"])
            self.assertEqual("待回核", payload["工单"]["任务书校验"])
        warning = stderr.getvalue().strip()
        self.assertIn("已自动省掉 --taskbook-client-checked 重发", warning)
        # ★(「非业务闸不停车」)改了这段话的口径:
        # 版本差是账面事,不是活没做好。旧文案把「它不会进设计者队列」写成后果,读起来像出了大事,
        # 员工据此停车等平台上服;而 ③ 之后「待回核」已经不再挡队列,那条后果本身也不成立了。
        # 现在必须明说三件事:这次操作已经生效、待回核不挡任何动作、不要为此停车。
        self.assertIn("不影响这次操作", warning)
        self.assertIn("命令已经生效", warning)
        self.assertIn("不挡开窗、不挡认领、不挡交板", warning)
        self.assertIn("不要为此停车等谁", warning)
        self.assertIn("set --taskbook <同一个路径>", warning)
        self.assertNotIn("它不会进设计者队列", warning)

    def test_new_client_keeps_new_set_list_and_receipt_working_against_protocol_zero(self):
        taskbook = self.root / "旧服兼容任务书.md"
        taskbook.write_text("# 已在客户端核过\n", encoding="utf-8")
        replacement = self.root / "旧服兼容任务书-改.md"
        replacement.write_text("# 客户端再次核过\n", encoding="utf-8")
        new_argv = [
            "new", "--slot", SLOT, "--title", "新客户端打旧服", "--source", "DECISIONS.md:兼容",
            "--consumer", "主界面/面板根", "--assign", self.worker, "--tier", "乙",
            "--deliverable", str(self.deliverable), "--taskbook", str(taskbook),
            "--taskbook-client-checked", "--player-facing",
        ]
        with self.protocol_zero_server() as (client, _), contextlib.redirect_stderr(io.StringIO()):
            created, _ = client.execute(new_argv)
            changed, _ = client.execute([
                "set", created["编号"], "--taskbook", str(replacement), "--by", SLOT,
                "--taskbook-client-checked",
            ])
            listed, _ = client.execute(["list"])
            receipt_payload, receipt_text = client.execute(["receipt", created["编号"]])
        self.assertEqual("待回核", created["任务书校验"])
        self.assertEqual("待回核", changed["工单"]["任务书校验"])
        self.assertTrue(any(row["编号"] == created["编号"] for row in listed))
        self.assertEqual(0, client.server_protocol)
        self.assertIn(f"客户端协议 {channel_config.PROTOCOL_VERSION} · 服务端协议 0", receipt_text)
        self.assertIn("客户端比服务端新", receipt_payload["receipt"])

    def test_remote_attach_compresses_locally_and_returns_same_copy(self):
        ticket = self.remote_service.list_tickets()[0]
        source = self.picture("remote-large.png", (1600, 900))
        _, text = self.client.execute([
            "attach", ticket["编号"], str(source), "--origin", "other", "--by", self.worker,
        ])
        self.assertRegex(text, r"^已附图 T-\d{6}-\d{2}\.jpg · 其他$")
        record = self.remote_service.store.load_ticket(ticket["编号"])["图片列表"][0]
        self.assertLessEqual((self.sqlite.images_dir / record["文件名"]).stat().st_size, MAX_IMAGE_BYTES)

    def test_remote_live_batch_with_picture_after_batch_options_still_uploads_bytes(self):
        """复检席实际敲的是 live <单号> --batch … <图> --shot 同图 --by …(图在选项后面)。

        旧判据只看 argv[2] 是不是图,于是整条 argv 被转发到服务端,服务端拿客户端本机路径去开图,
        16 张玩家可感知单全部回显「找不到图片:<本机路径>」。凡带图片位置参数的 live 都必须在客户端读图上传。
        """
        picture = self.picture("after-options.png", (900, 600))
        first = self.remote_service.list_tickets()[0]
        argv = ["live", first["编号"], "--batch", "T-999999", str(picture), "--shot", "同图", "--by", "复检·合并"]
        self.assertEqual([first["编号"], str(picture)], self.client._live_positionals(argv))
        with mock.patch.object(self.client, "request", wraps=self.client.request) as requested:
            try:
                self.client.execute(argv)
            except TicketError:
                pass  # 单子状态不对会被服务端拦,这里只看是不是先上传了字节
        paths = [call.args[1] for call in requested.call_args_list]
        self.assertEqual("/api/upload", paths[0], paths)
        self.assertNotIn("/api/cli", paths)

    def test_remote_live_batch_uploads_picture_exactly_once(self):
        service = self.remote_service

        def merged(title: str, internal: bool) -> dict[str, object]:
            ticket = service.create_dispatch(
                SLOT, title, ["DECISIONS.md:remote-batch"], "工单台" if internal else "主界面/面板根",
                self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=internal,
            )
            service.claim(ticket["编号"], self.worker)
            if internal:
                ticket = service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
            else:
                service.attach(ticket["编号"], str(self.picture(f"{ticket['编号']}-remote-before.png")), "world", self.worker)
                ticket = service.submit(ticket["编号"], "登录后界面已出现")
            ticket, _ = service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
            service.verify(ticket["编号"], "独立复检", "过", gates="夹具:六项闸摘要")
            return service.merge(ticket["编号"], "独立复检")

        tickets = [merged("远程批量内部首单", True)] + [
            merged(f"远程批量 {index}", index >= 4) for index in range(1, 5)
        ]
        argv = ["live", tickets[0]["编号"], str(self.picture("remote-batch.png"))]
        for ticket in tickets[1:]:
            argv.extend(["--batch", ticket["编号"]])
        argv.extend(["--shot", "同图", "--by", "独立复检"])
        with mock.patch.object(self.client, "request", wraps=self.client.request) as requested:
            rows, text = self.client.execute(argv)
        upload_count = sum(call.args[1] == "/api/upload" for call in requested.call_args_list)
        self.assertEqual(1, upload_count)
        self.assertEqual(["已复验"] * 5, [row["结果"] for row in rows])
        self.assertEqual(5, text.count("· 已复验 ·"))
        self.assertEqual([], service.store.load_ticket(tickets[0]["编号"])["图片列表"])

    def test_full_remote_flow_output_matches_file_mode(self):
        from tools.tickets.ticket import execute, parser

        image = self.picture("parity.png", (900, 600))
        new_args = [
            "new", "--slot", SLOT, "--title", "远程逐字节一致", "--source", "DECISIONS.md:remote",
            "--consumer", "主界面/面板根", "--assign", self.worker, "--tier", "乙",
            "--deliverable", str(self.deliverable), "--player-facing",
        ]

        def both(argv: list[str]) -> tuple[object, object]:
            local_payload, local_text = execute(parser().parse_args(argv), self.service)
            remote_payload, remote_text = self.client.execute(argv)
            self.assertEqual(local_text, remote_text)
            return local_payload, remote_payload

        local_ticket, remote_ticket = both(new_args)
        ticket_id = local_ticket["编号"]
        self.assertEqual(ticket_id, remote_ticket["编号"])
        both(["claim", ticket_id, "--by", self.worker])
        both(["attach", ticket_id, str(image), "--origin", "world", "--by", self.worker])
        both(["submit", ticket_id, "--evidence", "远程路径完成"])
        both(["judge", ticket_id, "--pass", "--by", "UI总监", "--verdict", PASS_VERDICT])
        both(["transfer", ticket_id, "--to", OTHER_SLOT, "--reason", "交给后端复检", "--by", "UI总监"])
        both(["digest"])

    def test_remote_say_with_image_on_terminal_ticket_prints_same_hint(self):
        service = self.remote_service

        def dispatched(title: str) -> dict[str, object]:
            return service.create_dispatch(
                SLOT, title, ["DECISIONS.md:remote-say"], "主界面/面板根", self.worker,
                task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
            )

        terminal = dispatched("远程带图终态单")
        service.void(terminal["编号"], "建错了", SLOT)
        running = dispatched("远程带图在跑单")
        service.claim(running["编号"], self.worker)
        image = self.picture("tiny.png", (120, 80))
        # 带图的 say 走客户端 _say(POST /api/say → say_uploaded):终态单照常写入,
        # 提示拼进返回文本第二行,且 --json 的 result 里不残留该键(与本地 pop 行为一致)。
        result, text = self.client.execute([
            "say", "--slot", SLOT, "--by", "设计者", "--ref", terminal["编号"],
            "--img", str(image), "远程带图到终态单",
        ])
        self.assertIn(TicketService.SAY_TERMINAL_HINT.format(state="作废"), text)
        self.assertNotIn("终态提示", result)
        # 对照:同一命令引到在跑单,输出与修复前逐字相同——单行、以「已写入」开头、无★。
        result, text = self.client.execute([
            "say", "--slot", SLOT, "--by", "设计者", "--ref", running["编号"],
            "--img", str(image), "远程带图到在跑单",
        ])
        self.assertTrue(text.startswith(f"已写入 {SLOT} 对话线 · "))
        self.assertEqual(1, len(text.splitlines()))
        self.assertNotIn("★", text)
        self.assertNotIn("终态提示", result)

    def test_unavailable_remote_reads_snapshot_but_never_writes_local(self):
        environment = clean_environment(
            self.service.store.root, TICKET_REMOTE="http://127.0.0.1:1", TICKET_TOKEN_FILE=str(self.token_file),
        )
        command = CLI
        readable = subprocess.run(command + ["list"], cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(3, readable.returncode)
        self.assertIn("远程不可达", readable.stderr)
        self.assertNotIn("T-0000", readable.stdout)
        stale = subprocess.run(
            command + ["list"], cwd=ROOT, env=dict(environment, TICKET_ALLOW_STALE="1"),
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(3, stale.returncode)
        self.assertIn("本机只读快照", stale.stderr)
        self.assertIn("T-0000", stale.stdout)
        ticket = self.service.list_tickets()[0]
        before = ticket["状态"]
        blocked = subprocess.run(
            command + ["block", ticket["编号"], "断网不应落本机"],
            cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(2, blocked.returncode)
        self.assertIn("没有落回本机", blocked.stderr)
        self.assertEqual(before, self.service.store.load_ticket(ticket["编号"])["状态"])


class TestsNeverTouchProductionTests(unittest.TestCase):
    """测试进程、测试子进程,都不许有任何一条路通到真服务器。"""

    def test_r1_test_process_itself_has_no_ticket_remote(self):
        # 环境里留着 TICKET_REMOTE 跑测试,曾在生产库里建出六张假单(~86)。这里直接报红,不替人擦屁股。
        value = os.environ.get("TICKET_REMOTE", "")
        self.assertEqual("", value, f"{REMOTE_GUARD_MESSAGE}(当前 TICKET_REMOTE={value!r})")

    def test_r1_clean_environment_strips_every_channel_variable(self):
        polluted = {name: f"污染-{name}" for name in CHANNEL_VARIABLES}
        with mock.patch.dict(os.environ, polluted):
            environment = clean_environment("D:/tmp/用例库")
        for name in CHANNEL_VARIABLES:
            self.assertNotIn(name, environment, name)
        self.assertEqual("D:/tmp/用例库", environment["TICKET_DESK_ROOT"])
        self.assertEqual("utf-8", environment["PYTHONIOENCODING"])
        self.assertIn("PATH", environment, "派生环境仍要带系统变量,子进程才起得来")

    def test_r1_clean_environment_only_lets_a_test_pass_its_own_remote_in(self):
        with mock.patch.dict(os.environ, {"TICKET_REMOTE": "https://生产"}):
            environment = clean_environment("D:/tmp", TICKET_REMOTE="http://127.0.0.1:1")
        self.assertEqual("http://127.0.0.1:1", environment["TICKET_REMOTE"])

    def test_r1_local_flag_beats_a_polluted_parent_environment(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"TICKET_REMOTE": "http://127.0.0.1:1", "TICKET_TOKEN_FILE": str(Path(root) / "无")}):
                result = run_local_cli(["staff", "list"], Path(root) / "tickets")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("远程", result.stderr + result.stdout)
        self.assertEqual("名册为空。", result.stdout.strip())

    def test_r1_every_cli_subprocess_in_this_file_derives_from_clean_environment(self):
        source = Path(__file__).read_text(encoding="utf-8")
        # mock.patch.dict(os.environ, ...) 是往测试进程里放污染,允许;直接拿 os.environ 给子进程,不允许。
        self.assertNotRegex(source, r"(?<!\.)dict\(os\.environ", "起子进程只准用 clean_environment / run_local_cli")
        self.assertNotRegex(source, r"env\s*=\s*os\.environ\b")


class ChannelConfigTests(unittest.TestCase):
    """取配置三级顺序 + --local;remote.env 缺什么要说人话。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # 模拟一台别的机器:仓库放在 E:/别的盘/proj/repo,worktree 在 proj/_work/wt-x。
        self.project = self.root / "proj"
        self.repo = self.project / "repo"
        self.worktree = self.project / "_work" / "wt-x"
        for path in (self.repo, self.worktree, self.project / "tasks" / "tickets"):
            path.mkdir(parents=True)
        self.token = self.project / "server-keys" / "token.txt"
        self.token.parent.mkdir()
        self.token.write_text("秘密令牌\n", encoding="utf-8")
        self.env_file = self.project / "tasks" / "tickets" / "remote.env"
        self.pointer_file = self.root / "elsewhere.env"
        self.local_store = str(self.root / "本机库")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_env(self, path: Path, remote: str = "https://file.example:8443", token: str | None = None, extra: str = "") -> Path:
        lines = [f"# 注释\nTICKET_REMOTE={remote}"]
        if token is not None:
            lines.append(f"TICKET_TOKEN_FILE={token}")
        if extra:
            lines.append(extra)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def environ(self, **values: str) -> dict[str, str]:
        return {"TICKET_DESK_ROOT": self.local_store, **values}

    def test_r2_level_one_environment_wins_over_pointer_and_derived(self):
        self.write_env(self.env_file, token=str(self.token))
        self.write_env(self.pointer_file, remote="https://pointer.example", token=str(self.token))
        channel = channel_config.resolve(False, self.environ(
            TICKET_REMOTE="https://env.example:1", TICKET_TOKEN_FILE=str(self.token), TICKET_ENV=str(self.pointer_file),
        ), search_from=self.repo)
        self.assertTrue(channel.is_remote)
        self.assertEqual(channel_config.LEVEL_ENVIRONMENT, channel.level)
        self.assertEqual("env.example:1", channel.host)
        self.assertEqual("", channel.problem)

    def test_r2_level_two_pointer_wins_over_derived_and_reads_backslash_paths(self):
        self.write_env(self.env_file, token=str(self.token))
        self.write_env(self.pointer_file, remote="https://pointer.example", token=str(self.token).replace("/", "\\"))
        channel = channel_config.resolve(False, self.environ(TICKET_ENV=str(self.pointer_file).replace("/", "\\")), search_from=self.repo)
        self.assertEqual(channel_config.LEVEL_POINTER, channel.level)
        self.assertEqual("pointer.example", channel.host)
        self.assertNotIn("\\", channel.token_file)
        self.assertTrue(channel.token_file_exists)
        self.assertEqual("", channel.problem)

    def test_r2_level_three_is_derived_from_repo_position_including_worktrees(self):
        self.write_env(self.env_file, token=str(self.token))
        for start in (self.repo, self.worktree):
            with self.subTest(start=start):
                channel = channel_config.resolve(False, self.environ(), search_from=start)
                self.assertEqual(channel_config.LEVEL_DERIVED, channel.level)
                self.assertEqual("file.example:8443", channel.host)
                self.assertEqual(channel_config.forward_slashes(str(self.env_file)), channel.source)
                self.assertEqual("", channel.problem)

    def test_r2_level_three_relative_token_path_resolves_next_to_the_env_file(self):
        (self.env_file.parent / "token.txt").write_text("x\n", encoding="utf-8")
        self.write_env(self.env_file, token="token.txt")
        channel = channel_config.resolve(False, self.environ(), search_from=self.worktree)
        self.assertEqual(channel_config.forward_slashes(str(self.env_file.parent / "token.txt")), channel.token_file)
        self.assertTrue(channel.token_file_exists)

    def test_r2_level_four_is_local_and_says_where_the_env_file_should_go(self):
        channel = channel_config.resolve(False, self.environ(), search_from=self.worktree)
        self.assertFalse(channel.is_remote)
        self.assertEqual(channel_config.LEVEL_NONE, channel.level)
        self.assertEqual(Path(self.local_store).resolve(), channel.local_root)
        expected = channel_config.forward_slashes(str(self.env_file))
        self.assertIn(expected, channel.source)
        self.assertIn(expected, channel_config.connect_hint(channel, search_from=self.worktree))
        self.assertIn("TICKET_ENV", channel_config.connect_hint(channel, search_from=self.worktree))

    def test_r2_local_flag_ignores_all_three_levels(self):
        self.write_env(self.env_file, token=str(self.token))
        self.write_env(self.pointer_file, token=str(self.token))
        channel = channel_config.resolve(True, self.environ(
            TICKET_REMOTE="https://env.example", TICKET_TOKEN_FILE=str(self.token), TICKET_ENV=str(self.pointer_file),
        ), search_from=self.repo)
        self.assertFalse(channel.is_remote)
        self.assertEqual(channel_config.LEVEL_FORCED, channel.level)
        self.assertEqual("去掉 --local 再跑", channel_config.connect_hint(channel))

    def test_r2_env_file_missing_token_line_is_explained_in_plain_words(self):
        self.write_env(self.env_file)
        channel = channel_config.resolve(False, self.environ(), search_from=self.repo)
        self.assertTrue(channel.is_remote, "配置找到了就该算远程,哪怕它有问题")
        self.assertIn("缺 TICKET_TOKEN_FILE", channel.problem)
        self.assertIn(channel_config.forward_slashes(str(self.env_file)), channel.problem)
        self.assertIn("TICKET_TOKEN_FILE=<令牌文件的正斜杠路径>", channel.problem)

    def test_r2_env_file_pointing_at_a_missing_token_file_names_the_path(self):
        ghost = self.project / "server-keys" / "没有的令牌.txt"
        self.write_env(self.env_file, token=str(ghost))
        channel = channel_config.resolve(False, self.environ(), search_from=self.repo)
        self.assertIn("不存在", channel.problem)
        self.assertIn(channel_config.forward_slashes(str(ghost)), channel.problem)
        self.assertIn("向本位总监领", channel.problem)
        self.assertFalse(channel.token_file_exists)

    def test_r2_env_file_without_remote_line_and_pointer_to_missing_file_are_both_explained(self):
        self.env_file.write_text("# 只有注释\nTICKET_TOKEN_FILE=x\n", encoding="utf-8")
        channel = channel_config.resolve(False, self.environ(), search_from=self.repo)
        self.assertIn("没有 TICKET_REMOTE 这一行", channel.problem)
        missing = channel_config.resolve(False, self.environ(TICKET_ENV=str(self.root / "无.env")), search_from=self.repo)
        self.assertTrue(missing.is_remote)
        self.assertIn("TICKET_ENV 指向的配置文件不存在", missing.problem)

    def test_r2_env_file_parser_tolerates_export_quotes_and_comments(self):
        self.env_file.write_text(
            "export TICKET_REMOTE='https://q.example'\n"
            "  # 注释\n\n"
            'TICKET_TOKEN_FILE="%s"\n' % str(self.token).replace("\\", "/") +
            "TICKET_CA_SHA256=AB:CD\n",
            encoding="utf-8",
        )
        values = channel_config.parse_env_file(self.env_file)
        self.assertEqual("https://q.example", values["TICKET_REMOTE"])
        self.assertEqual("AB:CD", values["TICKET_CA_SHA256"])
        channel = channel_config.resolve(False, self.environ(), search_from=self.repo)
        self.assertEqual("AB:CD", channel.ca_sha256)
        self.assertTrue(channel.token_file_exists)

    def test_r2_cli_with_broken_env_file_refuses_instead_of_falling_back_to_local(self):
        self.write_env(self.pointer_file)
        result = subprocess.run(
            [*CLI, "list"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            env=clean_environment(self.local_store, TICKET_ENV=str(self.pointer_file)),
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("缺 TICKET_TOKEN_FILE", result.stderr)
        self.assertFalse((Path(self.local_store) / "items").exists(), "配置有误时一个字都不许落到本机库")

    def test_r2_level_four_prints_one_local_mode_line_before_the_data(self):
        from tools.tickets.ticket import main

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self.environ(), clear=False), \
                mock.patch.object(channel_config, "REPO_ROOT", self.repo), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(stderr):
            for name in CHANNEL_VARIABLES:
                os.environ.pop(name, None)
            code = main(["staff", "list"])
        self.assertEqual(0, code)
        self.assertEqual("名册为空。", stdout.getvalue().strip())
        notice = stderr.getvalue().strip()
        self.assertTrue(notice.startswith("本机模式(未接通道):本机库 "), notice)
        self.assertIn(channel_config.forward_slashes(str(Path(self.local_store).resolve())), notice)
        self.assertIn(channel_config.forward_slashes(str(self.env_file)), notice)

    def test_r3_missing_ticket_in_unconfigured_local_mode_says_two_exact_sentences(self):
        from tools.tickets.ticket import main

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, self.environ(), clear=False), \
                mock.patch.object(channel_config, "REPO_ROOT", self.repo), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            for name in CHANNEL_VARIABLES:
                os.environ.pop(name, None)
            code = main(["submit", "T-000069"])
        self.assertEqual(2, code)
        lines = [line for line in stderr.getvalue().splitlines() if line.startswith("拦下:")]
        self.assertEqual(1, len(lines), stderr.getvalue())
        root = channel_config.forward_slashes(str(Path(self.local_store).resolve()))
        expected_first = f"拦下:本机模式(未接通道):本机库 {root} 里没有 T-000069。"
        self.assertEqual(expected_first, lines[0])
        second = stderr.getvalue().splitlines()[stderr.getvalue().splitlines().index(lines[0]) + 1]
        self.assertTrue(second.startswith("如果这张单在服务器上,先接通道:"), second)
        self.assertIn(channel_config.forward_slashes(str(self.env_file)), second)
        self.assertIn("TICKET_ENV", second)

    def test_r3_missing_ticket_under_local_flag_tells_you_to_drop_the_flag(self):
        result = run_local_cli(["show", "T-000069"], self.local_store)
        self.assertEqual(2, result.returncode)
        self.assertIn("拦下:本机模式(--local 强制):本机库 ", result.stderr)
        self.assertIn(" 里没有 T-000069。", result.stderr)
        self.assertIn("如果这张单在服务器上,先接通道:去掉 --local 再跑。", result.stderr)
        self.assertNotIn("找不到工单", result.stderr)

    def env_cli(self, *arguments: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*CLI, "env", *arguments], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            env=clean_environment(self.local_store, **extra),
        )

    def test_r4_env_in_remote_mode_prints_host_token_path_existence_and_level_but_never_the_token(self):
        secret = "S3cr3t-" + secrets.token_urlsafe(24)
        self.token.write_text(secret + "\n", encoding="utf-8")
        self.write_env(self.pointer_file, remote="https://desk.example:8443", token=str(self.token))
        for arguments in ((), ("--json",)):
            with self.subTest(arguments=arguments):
                result = self.env_cli(*arguments, TICKET_ENV=str(self.pointer_file))
                self.assertEqual(0, result.returncode, result.stderr)
                output = result.stdout + result.stderr
                self.assertNotIn(secret, output, "令牌本身绝不能被打出来")
                self.assertNotIn(secret[:12], output)
                self.assertIn("desk.example:8443", output)
                self.assertIn(channel_config.forward_slashes(str(self.token)), output)
                self.assertIn(channel_config.forward_slashes(str(Path(self.local_store).resolve())), output)
                self.assertIn("②TICKET_ENV", output)
                self.assertEqual("", result.stderr)
        text = self.env_cli(TICKET_ENV=str(self.pointer_file)).stdout.strip()
        self.assertEqual(1, len(text.splitlines()), text)
        self.assertTrue(text.startswith("远程模式 · 服务器 desk.example:8443 · 令牌文件 "), text)
        self.assertIn("（存在）", text)
        payload = json.loads(self.env_cli("--json", TICKET_ENV=str(self.pointer_file)).stdout)
        self.assertEqual("远程模式", payload["模式"])
        self.assertTrue(payload["令牌文件存在"])
        self.assertEqual("", payload["问题"])
        self.assertNotIn("令牌", json.dumps({k: v for k, v in payload.items() if k not in {"令牌文件", "令牌文件存在"}}, ensure_ascii=False))

    def test_env_without_probe_on_a_real_dead_port_stays_offline_and_returns_zero(self):
        self.token.write_text("dead-port-token\n", encoding="utf-8")
        result = self.env_cli(
            TICKET_REMOTE="http://127.0.0.1:1", TICKET_TOKEN_FILE=str(self.token),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"客户端协议 {channel_config.PROTOCOL_VERSION}", result.stdout)
        self.assertNotIn("服务端协议", result.stdout + result.stderr)
        self.assertNotIn("服务端版本没查到", result.stdout + result.stderr)

    def test_env_without_probe_sends_no_packet_to_a_real_listener(self):
        self.token.write_text("listener-token\n", encoding="utf-8")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(1)
        connections: list[object] = []

        def trap() -> None:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                return
            connections.append(connection)
            connection.close()

        thread = threading.Thread(target=trap, daemon=True)
        thread.start()
        try:
            result = self.env_cli(
                TICKET_REMOTE=f"http://127.0.0.1:{listener.getsockname()[1]}",
                TICKET_TOKEN_FILE=str(self.token),
            )
            thread.join(timeout=2)
        finally:
            listener.close()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], connections, "光跑 env 不得连接远程地址")

    def test_env_probe_on_a_real_dead_port_explains_failure_but_returns_zero(self):
        self.token.write_text("dead-port-token\n", encoding="utf-8")
        result = self.env_cli(
            "--probe", TICKET_REMOTE="http://127.0.0.1:1", TICKET_TOKEN_FILE=str(self.token),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("服务端版本没查到:", result.stdout)
        self.assertIn("远程工单台不可达", result.stdout)

    def test_r4_env_reports_a_missing_token_file_and_a_broken_config_without_failing(self):
        ghost = self.project / "server-keys" / "没有的令牌.txt"
        self.write_env(self.pointer_file, token=str(ghost))
        result = self.env_cli(TICKET_ENV=str(self.pointer_file))
        self.assertEqual(0, result.returncode, "自检命令自己不该炸,要把问题报出来")
        self.assertIn("远程模式(配置有误)", result.stdout)
        self.assertIn("（不存在）", result.stdout)
        self.assertIn("问题 ", result.stdout)
        self.assertIn(channel_config.forward_slashes(str(ghost)), result.stdout)

    def test_r4_env_in_local_mode_names_the_local_store_and_the_level(self):
        forced = self.env_cli("--local")
        self.assertEqual(0, forced.returncode, forced.stderr)
        self.assertTrue(forced.stdout.startswith("本机模式(--local 强制) · 服务器 无 · 令牌文件 无 · 本机库 "), forced.stdout)
        self.assertIn(channel_config.forward_slashes(str(Path(self.local_store).resolve())), forced.stdout)
        self.assertEqual("", forced.stderr, "env 自己不该再打一遍本机模式提示")

        from tools.tickets.ticket import main

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.environ(), clear=False), \
                mock.patch.object(channel_config, "REPO_ROOT", self.repo), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            for name in CHANNEL_VARIABLES:
                os.environ.pop(name, None)
            self.assertEqual(0, main(["env"]))
        self.assertIn("本机模式(未接通道) · 服务器 无 · 令牌文件 无 · 本机库 ", stdout.getvalue())
        self.assertIn("④无配置", stdout.getvalue())
        self.assertIn(channel_config.forward_slashes(str(self.env_file)), stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_r4_env_never_reads_the_token_file_contents(self):
        self.write_env(self.pointer_file, token=str(self.token))
        real_read_text = Path.read_text

        def guarded(path: Path, *args, **kwargs):
            if path.resolve() == self.token.resolve():
                raise AssertionError("env 自检读了令牌文件的内容")
            return real_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded):
            channel = channel_config.resolve(False, self.environ(TICKET_ENV=str(self.pointer_file)), search_from=self.repo)
            channel_config.describe(channel)
            channel_config.describe_payload(channel)
        self.assertTrue(channel.token_file_exists)

    def test_r5_readme_has_the_three_line_channel_section_with_env_first(self):
        readme = (ROOT / "tools" / "tickets" / "README.md").read_text(encoding="utf-8")
        self.assertIn("## 员工窗怎么接通道", readme)
        section = readme.split("## 员工窗怎么接通道", 1)[1].split("\n## ", 1)[0]
        steps = [line for line in section.splitlines() if re.match(r"^\d\. ", line)]
        self.assertEqual(3, len(steps), steps)
        self.assertIn("ticket.py env", steps[0])
        self.assertIn("remote.env", steps[1])
        self.assertIn("TICKET_ENV", steps[1])
        self.assertIn("receipt", steps[2])
        self.assertIn("--local", section)

    def test_protocol_readme_documents_versions_probe_whitelist_and_recovery(self):
        from tools.tickets.ticket import DEGRADABLE_OPTIONS

        readme = (ROOT / "tools" / "tickets" / "README.md").read_text(encoding="utf-8")
        section = readme.split("## 协议版本、探测与安全降级", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`0` 是没有版本握手的旧版", section)
        self.assertIn("`1` 是认识 `--taskbook-client-checked` 的版本", section)
        self.assertIn("`2` 是加入握手、版本回显和安全降级后的版本", section)
        self.assertIn("普通 `env` 永远不出网", section)
        self.assertIn("env --probe", section)
        self.assertIn("安全降级白名单", section)
        self.assertIn("set <工单号> --taskbook <同一个路径>", section)
        self.assertEqual({"--taskbook-client-checked": 1}, DEGRADABLE_OPTIONS)

    def test_r3_other_errors_are_left_untouched(self):
        from tools.tickets.ticket import _explain_missing_ticket

        channel = channel_config.resolve(True, self.environ())
        self.assertEqual("工单号格式不对：abc，应为 T-000001。", _explain_missing_ticket("工单号格式不对：abc，应为 T-000001。", channel))
        self.assertEqual("找不到工单 T-000001。", _explain_missing_ticket("找不到工单 T-000001。", None))


class PackageTreeGateTests(unittest.TestCase):
    """闸②在服务器上跑的是「只有 tools/tickets 与 tools/browser」的上服包树,不是完整仓树。

    包树里读不到 tools/ 以外的文件,那几条用例必须**干净跳过而不是红**——否则闸把每一次上服都拦死。
    但只钉这一半,等于给自己开了个「跳光了也算绿」的口子,所以下面第二条反过来钉:
    同样那几条在仓树上必须**真跑**。两条缺一不可,少哪条都能把 R1 变成放水。
    """

    REPOSITORY_ONLY_TEST = (
        "tools/tickets/tests/test_ticket_system.py"
        "::FixtureAndInterfaceTests::test_move_verify_fixture_has_equal_hashes"
    )

    @staticmethod
    def run_pytest(working_directory: Path, node_id: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pytest", node_id, "-q", "-rs", "-p", "no:cacheprovider"],
            cwd=working_directory, env=clean_environment(working_directory / "tickets"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def package_tree(self) -> Path:
        """照上服脚本(扩展 server_deploy)的口径造一棵只有核心代码的包树:tools/tickets 与 tools/browser。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        package = Path(temporary.name) / "pkg"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        for part in ("tickets", "browser"):
            shutil.copytree(ROOT / "tools" / part, package / "tools" / part, ignore=ignore)
        self.assertFalse((package / "review").exists(), "包树里不该有 tools/ 以外的东西")
        return package

    def test_repository_only_tests_are_skipped_not_failed_in_the_package_tree(self):
        done = self.run_pytest(self.package_tree(), self.REPOSITORY_ONLY_TEST)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("1 skipped", done.stdout)
        self.assertNotIn("failed", done.stdout)
        self.assertIn(PACKAGE_TREE_SKIP_PREFIX, done.stdout)     # -rs 把人话理由打出来了

    def test_the_same_tests_really_run_in_the_repository_tree(self):
        """★这一条是防放水的那一条:R1 的跳过条件一旦变成「永远跳过」,这里立刻红。

        它自己也只在仓树上成立——包树里根本没有仓树可跑。但这里**不能**走 repository_file_or_skip:
        把那个跳过条件改成「永远跳过」的那次变异,会连这条守门用例一起跳掉,守门就白守了。
        所以它自己直接看文件在不在,不经过被守的那段代码。
        真正把这条守住的是仓树上的每一次全量(交板闸、本机全量),不是服务器上那一趟。
        """
        if not (ROOT / "review" / "ticket-system" / "MOVE-VERIFY.md").is_file():
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX},这一条要在仓树上真跑一次,包树里没有仓树。")
        done = self.run_pytest(ROOT, self.REPOSITORY_ONLY_TEST)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("1 passed", done.stdout)
        self.assertNotIn("skipped", done.stdout)
        self.assertNotIn(PACKAGE_TREE_SKIP_PREFIX, done.stdout)


class TicketSeventeenRegressionTests(unittest.TestCase):
    """ 的六条回归：一条一条来，坏了要能一眼看出坏在哪一条。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SqliteStore(self.root / "db" / "tickets.sqlite")
        self.service = TicketService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def rows_in_threads_table(self, slot: str) -> list[tuple]:
        connection = sqlite3.connect(self.store.database)
        try:
            return connection.execute(
                "SELECT position, payload FROM threads WHERE slot=? ORDER BY position", (slot,)
            ).fetchall()
        finally:
            connection.close()

    def test_r1_mark_read_lands_in_the_threads_table_not_on_disk(self):
        # 服务器上 /srv/ticket-desk/threads/ 根本不存在，这里也刻意不建。
        self.assertFalse(self.store.threads_dir.exists())
        self.service.say(SLOT, "总编", "第一条")
        self.service.say(SLOT, "设计者", "第二条")
        self.assertEqual(2, len(self.service.inbox(SLOT, SLOT)))

        self.assertEqual(2, len(self.service.inbox(SLOT, SLOT, mark_read=True)))

        self.assertEqual([], self.service.inbox(SLOT, SLOT))
        rows = self.rows_in_threads_table(SLOT)
        self.assertEqual([1, 2], [row[0] for row in rows])
        for _, payload in rows:
            self.assertIn(SLOT, json.loads(payload)["已读标记"])
        self.assertFalse(self.store.threads_dir.exists(), "标已读不许偷偷落磁盘")

    def test_r1_say_and_inbox_work_on_a_slot_with_no_thread_yet(self):
        fresh = "内容·文案"
        self.assertEqual([], self.rows_in_threads_table(fresh))
        self.service.say(fresh, "总编", "新位第一句")
        self.assertEqual(1, len(self.service.inbox(fresh, fresh)))
        self.service.inbox(fresh, fresh, mark_read=True)
        self.assertEqual([], self.service.inbox(fresh, fresh))
        self.assertEqual(1, len(self.rows_in_threads_table(fresh)))
        self.assertEqual([], self.service.inbox(SLOT, SLOT), "别位的对话线不该被带出来")

    def test_r2_question_owner_slot_can_answer_but_decision_still_cannot(self):
        question = self.service.create_question("疑问", SLOT, "跨位提事", "远程标已读报错", initiator=OTHER_SLOT)
        self.assertEqual(SLOT, question["所属总监位"])
        answered = self.service.answer(question["编号"], "已确认是服务端写回绕过 store。", SLOT)
        self.assertEqual("已答", answered["状态"])

        decision = self.service.create_question("拍板", SLOT, "要不要统一话术", VALID_DECISION_BODY, initiator=OTHER_SLOT)
        with self.assertRaisesRegex(TicketError, "设计者或总编"):
            self.service.answer(decision["编号"], "我来拍", SLOT)
        self.assertEqual("待答", self.service.store.load_ticket(decision["编号"])["状态"])

    def test_r4_new_slot_gets_its_staff_bucket_and_nothing_raises_keyerror(self):
        fake = "测试·加位不炸"
        original = model.SLOTS
        try:
            for module in (model, store_module, service_module):
                module.SLOTS = original + (fake,)
            store = SqliteStore(self.store.database)
            service = TicketService(store)
            self.assertIn(fake, store.load_staff()["总监位"])
            self.assertEqual([], service.list_staff(fake))
            self.assertEqual(f"{fake}-01", service.staff_new(fake, "opus")["员工名"])
            self.assertEqual([f"{fake}-01"], [row["员工名"] for row in service.list_staff(fake)])
        finally:
            for module in (model, store_module, service_module):
                module.SLOTS = original


class TicketSeventeenRemoteTests(TicketTestCase):
    """R3 与 R5：都要真的起一个服务端、真的走一遍命令行才算数。"""

    def setUp(self) -> None:
        super().setUp()
        self.sqlite = SqliteStore(self.root / "server" / "db" / "tickets.sqlite")
        self.remote_service = TicketService(self.sqlite)
        self.remote_worker = self.remote_service.staff_new(SLOT, "sol")["员工名"]
        self.service_token = secrets.token_urlsafe(32)
        handler = partial(TicketRequestHandler, directory=str(ROOT / "tools" / "browser"))
        self.server = TicketHTTPServer(
            ("127.0.0.1", 0), handler, self.remote_service, self.service_token, AccountManager(self.sqlite.database)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token_file = self.root / "remote.token"
        self.token_file.write_text(self.service_token, encoding="utf-8")
        self.command = CLI

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def environment(self, remote: str, **extra) -> dict[str, str]:
        return clean_environment(
            self.service.store.root, TICKET_REMOTE=remote, TICKET_TOKEN_FILE=str(self.token_file), **extra,
        )

    def run_cli(self, argv: list[str], environment: dict[str, str]):
        return subprocess.run(
            self.command + argv, cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8"
        )

    def claimed_remote_ticket(self, title: str, deliverable: Path) -> str:
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        created = self.run_cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", title, "--source", "AGENTS.md:1",
            "--consumer", "工单台", "--assign", self.remote_worker, "--tier", "乙",
            "--deliverable", str(deliverable), "--internal", "--json",
        ], environment)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = json.loads(created.stdout)["result"]["编号"]
        self.assertEqual(0, self.run_cli(["claim", ticket_id, "--by", self.remote_worker], environment).returncode)
        return ticket_id

    def test_r3_unreachable_remote_exits_non_zero_and_only_shows_stale_data_on_request(self):
        self.dispatch("本机快照里的旧单")
        environment = self.environment("http://127.0.0.1:1")
        default = self.run_cli(["list"], environment)
        self.assertEqual(3, default.returncode)
        self.assertIn("远程不可达", default.stderr)
        self.assertIn("快照时间", default.stderr)
        self.assertNotIn("T-0000", default.stdout)

        stale = self.run_cli(["list"], self.environment("http://127.0.0.1:1", TICKET_ALLOW_STALE="1"))
        self.assertEqual(3, stale.returncode, "给了快照也不许退 0")
        self.assertIn("不是服务器上的数据", stale.stdout)
        self.assertIn("不是服务器上的数据", stale.stderr)
        self.assertIn("T-0000", stale.stdout)

    def test_t108_r2_ticket_env_file_alone_connects_the_cli_to_the_server(self):
        env_file = self.root / "remote.env"
        env_file.write_text(
            f"TICKET_REMOTE=http://127.0.0.1:{self.server.server_address[1]}\n"
            f"TICKET_TOKEN_FILE={str(self.token_file).replace(chr(92), '/')}\n",
            encoding="utf-8",
        )
        environment = clean_environment(self.service.store.root, TICKET_ENV=str(env_file))
        listed = self.run_cli(["list"], environment)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual("", listed.stderr)
        self.assertEqual(self.remote_service.list_tickets(), [], "服务端库里还没有单")
        self.assertIn("没有符合条件的工单", listed.stdout)

    def test_t108_r3_missing_ticket_in_remote_mode_blames_the_server_not_the_local_store(self):
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        result = self.run_cli(["show", "T-000999"], environment)
        self.assertEqual(2, result.returncode)
        self.assertEqual(
            f"拦下:远程模式:服务器 127.0.0.1:{self.server.server_address[1]} 上没有这张单 T-000999。",
            result.stderr.strip(),
        )
        self.assertNotIn("本机", result.stderr)

    def test_remote_export_fetches_the_ticket_then_writes_the_client_out_path(self):
        ticket = self.remote_service.create_dispatch(
            SLOT, "远程丙档导出", ["D:/source.txt:1-20"], "D:/output.csv", self.remote_worker,
            notes="只生成一张 CSV", task_tier="丙", context_lines=20,
            deliverables=[str(self.deliverable)], internal=False,
        )
        out = self.root / "client-only" / "任务书.md"
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        result = self.run_cli(["export", ticket["编号"], "--out", str(out)], environment)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(out.resolve()), result.stdout.strip())
        self.assertTrue(out.is_file())
        self.assertIn("任务档:丙", out.read_text(encoding="utf-8"))

    def test_r5_remote_submit_checks_deliverables_on_the_submitting_machine(self):
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        # 交付项写成一个只有本机有、服务端根目录里绝对没有的路径。
        local_only = self.root / "提交端独有交付物.md"
        local_only.write_text("# 交付物\n", encoding="utf-8")
        self.assertFalse((self.sqlite.root / local_only.name).exists())

        ticket_id = self.claimed_remote_ticket("远程交板·交付项在本机", local_only)
        submitted = self.run_cli([
            "submit", ticket_id, "--evidence", "内部工具单验证完成",
            "--verify-command", "python -m pytest tools/tickets/tests -q", "--raw-output", "61 passed",
        ], environment)
        self.assertEqual(0, submitted.returncode, submitted.stderr)
        stored = self.remote_service.store.load_ticket(ticket_id)
        self.assertEqual("待判", stored["状态"])
        self.assertIn("交付项本机核验:1/1 齐", stored["接线证据"]["文字"])

    def test_r5_missing_deliverable_is_refused_on_the_submitting_machine(self):
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        ghost = self.root / "缺失交付物.md"
        ghost.write_text("# 建单时存在\n", encoding="utf-8")
        ticket_id = self.claimed_remote_ticket("远程交板·交付项缺一个", ghost)
        ghost.unlink()
        refused = self.run_cli([
            "submit", ticket_id, "--evidence", "内部工具单验证完成",
            "--verify-command", "python -m pytest tools/tickets/tests -q", "--raw-output", "61 passed",
        ], environment)
        self.assertEqual(2, refused.returncode)
        self.assertIn("以下交付项找不到对应文件", refused.stderr)
        self.assertIn(str(ghost), refused.stderr)
        self.assertIn("以提交端的文件系统为准", refused.stderr)
        self.assertEqual("已认领", self.remote_service.store.load_ticket(ticket_id)["状态"])

    def _six_item_submit_failure(self):
        environment = self.environment(f"http://127.0.0.1:{self.server.server_address[1]}")
        deliverables = [self.root / f"deliverable-{index}.md" for index in range(1, 7)]
        for path in deliverables:
            path.write_text(f"# {path.stem}\n", encoding="utf-8")
        command = [
            "new", "--type", "派单", "--slot", SLOT, "--title", "六项交付改单提示",
            "--source", "AGENTS.md:1", "--consumer", "工单台", "--assign", self.remote_worker,
            "--tier", "乙", "--internal", "--json",
        ]
        for path in deliverables:
            command.extend(["--deliverable", str(path)])
        created = self.run_cli(command, environment)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = json.loads(created.stdout)["result"]["编号"]
        self.assertEqual(0, self.run_cli(["claim", ticket_id, "--by", self.remote_worker], environment).returncode)
        missing = deliverables[3]
        missing.unlink()
        refused = self.run_cli([
            "submit", ticket_id, "--evidence", "内部工具单验证完成",
            "--verify-command", "python -m pytest tools/tickets/tests -q", "--raw-output", "tests passed",
        ], environment)
        self.assertEqual(2, refused.returncode)
        set_command = next(line for line in refused.stderr.splitlines() if line.startswith("python "))
        return refused, set_command, deliverables, missing

    def test_t267_r2_submit_error_has_complete_set_command_with_all_six_items(self):
        refused, set_command, deliverables, _ = self._six_item_submit_failure()
        self.assertIn("交付项只能由本位总监或总编改", refused.stderr)
        self.assertIn(f"原样发给 {SLOT}", refused.stderr)
        self.assertIn(str((ROOT / "tools" / "tickets" / "ticket.py").resolve()), set_command)
        self.assertEqual(6, set_command.count("--deliverable"))
        for path in deliverables:
            self.assertIn(str(path), set_command)
        self.assertIn(f"--by '{SLOT}'", set_command)

    def test_t267_r2_only_missing_item_is_listed_below_the_copyable_command(self):
        refused, set_command, _, missing = self._six_item_submit_failure()
        self.assertNotIn("<#", set_command)
        marker = "核不到的交付项（请在上面的命令里改这几条）：\n"
        self.assertIn(marker, refused.stderr)
        missing_section = refused.stderr.rsplit(marker, 1)[1].split("\n（以上是在本机", 1)[0]
        self.assertEqual(f"- {missing}", missing_section.strip())


class TicketSeventyFiveEditableTests(TicketTestCase):
    """一张单建完之后还能改什么。"""

    def cli(self, arguments: list[str], root: Path):
        return run_local_cli(arguments, root)

    def test_r1_dispatch_body_is_stored_not_silently_dropped(self):
        ticket = self.service.create_dispatch(
            SLOT, "派单也要收正文", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], body="abc", internal=False,
        )
        self.assertEqual("abc", ticket["正文"])
        self.assertEqual("abc", self.service.store.load_ticket(ticket["编号"])["正文"])

    def test_r1_cli_new_dispatch_body_survives_show(self):
        root = self.root / "cli-body"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行派单带正文",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--body", "abc", "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        shown = self.cli(["show", ticket_id], root)
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertEqual("abc", json.loads(shown.stdout)["正文"])

    def test_r2_taskbook_is_a_real_field_and_defaults_to_empty(self):
        ticket = self.dispatch("没填任务书路径")
        self.assertEqual("", ticket["任务书路径"])
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["任务书路径"])

    def test_r2_ticket_placeholder_is_replaced_with_the_real_id(self):
        ticket = self.service.create_dispatch(
            SLOT, "占位符替换", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
            taskbook=r"D:\project\_office\美术·视觉三\任务书\{ticket}_六库清点.md",
        )
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertNotIn("{ticket}", stored["任务书路径"])
        self.assertEqual(
            r"D:\project\_office\美术·视觉三\任务书" + "\\" + ticket["编号"] + "_六库清点.md",
            stored["任务书路径"],
        )

    def test_r2_cli_taskbook_placeholder_survives_show(self):
        root = self.root / "cli-taskbook"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行占位符",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--player-facing",
            "--taskbook", TASKBOOK_DIRECTORY + os.sep + "{ticket}_建完就改不了.md",
            "--taskbook-unchecked",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        shown = json.loads(self.cli(["show", ticket_id], root).stdout)
        self.assertEqual(
            TASKBOOK_DIRECTORY + os.sep + ticket_id + "_建完就改不了.md",
            shown["任务书路径"],
        )
        self.assertNotIn("{ticket}", shown["任务书路径"])

    def test_r2_question_type_also_keeps_taskbook_instead_of_dropping_it(self):
        ticket = self.service.create_question(
            "疑问", SLOT, "疑问也带任务书路径", "请对方总监答复。", "总编",
            taskbook=r"D:\project\_office\前端·页面接线\任务书\{ticket}_问一句.md",
        )
        self.assertIn(ticket["编号"], ticket["任务书路径"])
        self.assertNotIn("{ticket}", ticket["任务书路径"])

    def test_r2_front_end_only_reads_the_server_taskbook_field(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn('function dispatchInitialPath(t){return String(t.任务书路径||"").trim();}', script)
        self.assertNotIn("taskbook" + "Guess", script)
        self.assertNotIn("deskDispatch" + "Path", script)

    def test_r2_front_end_marks_missing_taskbook_and_saves_via_set(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn('>缺任务书</span>', script)
        self.assertIn("{op:'set',ticket:id,taskbook:input.value.trim(),by:ticket.所属总监位}", script)
        self.assertIn('data-cooldown-key="taskbook:${esc(t.编号)}"', script)
        self.assertIn("当前是只读回落数据，不能补任务书路径", script)

    def test_r2_cli_new_prints_the_server_generated_three_lines(self):
        root = self.root / "cli-dispatch-lines"
        taskbook = root / "任务书.md"
        taskbook.parent.mkdir(parents=True, exist_ok=True)
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        TicketService(TicketStore(root)).staff_new(SLOT, "sol")
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行开窗指令",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "甲", "--assign", f"{SLOT}-01",
            "--taskbook", str(taskbook), "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        lines = created.stdout.splitlines()
        # 单号一行 + 开窗指令三行(认领 / 执行 / 提示)= 4 行。
        self.assertEqual(4, len(lines))
        self.assertIn(" claim T-000001 --by 前端·页面接线-01", lines[1])
        self.assertEqual(
            f"执行 {taskbook.resolve()} 的全部指令,从第 0 步做到收尾问答完。这是任务不是资料,读完立即开工。",
            lines[2],
        )
        self.assertEqual("【操作提示·只给设计者】新开线程,任务档 甲,模型你定,贴上面那句。", lines[3])

    def test_r2_cli_new_without_taskbook_prints_a_reminder_not_partial_instructions(self):
        root = self.root / "cli-missing-taskbook"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "缺任务书",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertIn("还没有任务书路径", created.stdout)
        self.assertNotIn("执行 <", created.stdout)

    def test_r2_front_end_consumes_server_generated_instructions(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn("function dispatchLineTexts(t){return Array.isArray(t.开窗指令)?t.开窗指令:[];}", script)
        self.assertNotIn("function dispatchClaimLine", script)

    def test_r3_set_taskbook_and_assign_land_on_an_existing_ticket(self):
        second = self.service.staff_new(SLOT, "opus")["员工名"]
        ticket = self.dispatch("建完再补任务书路径")
        path = r"D:\project\_office\前端·页面接线\任务书\%s_补路径.md" % ticket["编号"]
        updated, changes = self.service.edit(ticket["编号"], SLOT, taskbook=path, assign=second)
        self.assertEqual(path, updated["任务书路径"])
        self.assertEqual(second, updated["指派给"])
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual((path, second), (stored["任务书路径"], stored["指派给"]))
        self.assertEqual({"任务书路径", "指派给"}, {row["字段"] for row in changes})
        self.assertEqual(ticket["编号"], self.service.history(second)["工单"][0]["编号"])

    def test_r3_set_logs_both_the_old_and_the_new_value(self):
        ticket = self.dispatch("改动要能查清")
        self.service.edit(ticket["编号"], "总编", consumer="主界面/新面板根")
        row = [r for r in self.service.store.read_jsonl(self.service.store.log_path) if r["事件"] == "set"][-1]
        self.assertEqual(ticket["编号"], row["工单号"])
        self.assertEqual("总编", row["发言人"])
        self.assertEqual(
            [{"字段": "实机消费者", "旧值": "主界面/面板根", "新值": "主界面/新面板根"}],
            row["改动"],
        )
        self.assertIn("主界面/面板根 → 主界面/新面板根", row["说明"])

    def test_r3_set_without_any_editable_field_is_refused_in_plain_words(self):
        ticket = self.dispatch("一个可改项都没给")
        with self.assertRaisesRegex(TicketError, "一个可改项都没给"):
            self.service.edit(ticket["编号"], SLOT)

    def test_r3_set_at_judging_only_allows_assign(self):
        ticket = self.to_judging()
        with self.assertRaises(TicketError) as caught:
            self.service.edit(ticket["编号"], SLOT, taskbook="D:/a.md")
        message = str(caught.exception)
        self.assertIn("现在是「待判」", message)
        self.assertIn("待判态只允许改指派给（--assign）", message)
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["任务书路径"])

        second = self.service.staff_new(SLOT, "opus")["员工名"]
        updated, changes = self.service.edit(ticket["编号"], SLOT, assign=second)
        self.assertEqual(second, updated["指派给"])
        self.assertEqual(["指派给"], [row["字段"] for row in changes])

    def test_r3_set_at_judging_can_restore_worker_from_previous_slot(self):
        ticket = self.to_judging()
        ticket["指派给"] = "总编"
        ticket["所属总监位"] = "总编"
        ticket["转交可见位"] = [SLOT]
        self.service.store.atomic_json(self.service.store.item_path(ticket["编号"]), ticket)

        updated, _ = self.service.edit(ticket["编号"], "总编", assign=self.worker)
        self.assertEqual(self.worker, updated["指派给"])

    def test_r3_set_by_a_foreign_slot_or_a_worker_window_is_refused(self):
        ticket = self.dispatch("别位不许改")
        for actor in (OTHER_SLOT, self.worker, "设计者"):
            with self.subTest(actor=actor):
                with self.assertRaises(TicketError) as caught:
                    self.service.edit(ticket["编号"], actor, taskbook="D:/a.md")
                self.assertIn("只有该位总监", str(caught.exception))
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["任务书路径"])

    def test_r3_set_assign_still_requires_an_active_staff_of_this_slot(self):
        ticket = self.dispatch("改派也要过名册")
        self.service.staff_retire(self.worker)
        with self.assertRaisesRegex(TicketError, "已收窗"):
            self.service.edit(ticket["编号"], SLOT, assign=self.worker)
        with self.assertRaisesRegex(TicketError, "员工名格式或所属位不对"):
            self.service.edit(ticket["编号"], SLOT, assign="后端·服务-01")

    def test_r3_set_refuses_to_empty_the_source_pointer(self):
        ticket = self.dispatch("真源指针不许清空")
        with self.assertRaisesRegex(TicketError, "一条有内容的都没有"):
            self.service.edit(ticket["编号"], SLOT, sources=["  "])

    def test_r3_cli_set_then_show_carries_the_new_value(self):
        root = self.root / "cli-set"
        worker = TicketService(TicketStore(root)).staff_new(SLOT, "sol")["员工名"]
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行改单",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--assign", worker, "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        taskbook = root / f"{ticket_id}_命令行改单.md"
        taskbook.write_text("# 已写好\n", encoding="utf-8")
        path = str(taskbook.resolve())
        changed = self.cli(["set", ticket_id, "--taskbook", path, "--by", SLOT], root)
        self.assertEqual(0, changed.returncode, changed.stderr)
        self.assertIn("已改 任务书路径", changed.stdout)
        self.assertIn(f" claim {ticket_id} --by {worker}", changed.stdout)
        self.assertIn("【操作提示·只给设计者】新开线程,任务档 乙,模型你定,贴上面那句。", changed.stdout)
        self.assertEqual(path, json.loads(self.cli(["show", ticket_id], root).stdout)["任务书路径"])

    def test_r1_missing_taskbook_is_blocked_then_the_same_command_passes_after_write(self):
        root = self.root / "taskbook-gate"
        taskbook = root / "任务书" / "待写.md"
        command = [
            "new", "--type", "派单", "--slot", SLOT, "--title", "先写任务书再建单",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT,
            "--taskbook", str(taskbook), "--player-facing",
        ]
        refused = self.cli(command, root)
        self.assertEqual(2, refused.returncode)
        self.assertIn("任务书还没写，先把 md 落盘再建单", refused.stderr)
        self.assertIn(str(taskbook.resolve()), refused.stderr)

        taskbook.parent.mkdir(parents=True)
        taskbook.write_text("# 已写好\n", encoding="utf-8")
        created = self.cli(command, root)
        self.assertEqual(0, created.returncode, created.stderr)
        stored = json.loads(self.cli(["show", created_ticket_id(created)], root).stdout)
        self.assertEqual("已核存在", stored["任务书校验"])

    def test_r1_unchecked_escape_passes_and_logs_actor_time_command_and_path(self):
        root = self.root / "taskbook-unchecked"
        missing = root / "根本不存在.md"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "冒烟跳过任务书",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT,
            "--taskbook", str(missing), "--taskbook-unchecked", "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        rows = [json.loads(line) for line in (root / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        audit = next(row for row in rows if row["事件"] == "taskbook-unchecked")
        self.assertTrue(audit["时间"])
        self.assertEqual(SLOT, audit["发言人"])
        self.assertEqual("new", audit["命令"])
        self.assertEqual(str(missing), audit["任务书绝对路径"])

    def test_r1_placeholder_missing_is_marked_pending_but_no_longer_blocks_the_queue(self):
        """★口径已由改掉:「待回核」不再挡设计者队列。

        这条用例原名 …_stays_out_of_designer_queue_until_rechecked,钉的是旧规矩:
        待回核 → 不进「要你传达的」。设计者 2026-09-08 当面点掉了它——
        待回核的意思只是「建单那台机器当时核不到那个路径」,路径本身写着、员工照常能做,
        这是**账面**不是活。挡在外面反而更糟:单子既不在队列、也没人当回事,那扇窗就一直不开。
        现在:标记照打(还要能看出来),但队列照进,卡片上挂一行黄字提醒补核。
        """
        root = self.root / "taskbook-placeholder"
        pattern = str(root / "任务书" / "{ticket}_稍后写.md")
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "拿号后回核",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT,
            "--taskbook", pattern, "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created.stdout.splitlines()[0]
        stored = json.loads(self.cli(["show", ticket_id], root).stdout)
        # 标记照打:它仍要看得出来「这一格还没核上」,总监才知道要补
        self.assertEqual("待回核", stored["任务书校验"])
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        # 队列不再拦它,「还没准备好开窗」那张清单里也不再列它
        self.assertNotIn('&&t.任务书校验!=="待回核"', script)
        self.assertNotIn('if(t.任务书校验==="待回核")缺.push', script)
        # 但必须换成看得见的提醒,不能就这么静悄悄放行
        self.assertIn("function taskbookPending(t)", script)
        self.assertIn("taskbook-pending-line", script)
        # 员工那一端也要说一句(receipt 尾巴),让他知道不用等
        receipt = self.cli(["receipt", ticket_id], root).stdout
        self.assertIn("待回核", receipt)
        self.assertIn("不影响你开工", receipt)

    def test_r1_placeholder_existing_at_the_final_id_is_checked_after_creation(self):
        root = self.root / "taskbook-placeholder-existing"
        taskbook_dir = root / "任务书"
        taskbook_dir.mkdir(parents=True)
        (taskbook_dir / "T-000001_已经写好.md").write_text("# 已写好\n", encoding="utf-8")
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "拿号后文件已在",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT,
            "--taskbook", str(taskbook_dir / "{ticket}_已经写好.md"), "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertEqual("T-000001", created_ticket_id(created))
        stored = json.loads(self.cli(["show", "T-000001"], root).stdout)
        self.assertEqual("已核存在", stored["任务书校验"])

    def test_r1_set_replaces_placeholder_before_checking_the_existing_file(self):
        root = self.root / "set-placeholder"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "改单替换占位符",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--by", SLOT, "--player-facing",
        ], root)
        ticket_id = created_ticket_id(created)
        taskbook_dir = root / "任务书"
        taskbook_dir.mkdir(parents=True)
        final_path = taskbook_dir / f"{ticket_id}_改单.md"
        final_path.write_text("# 已写好\n", encoding="utf-8")
        changed = self.cli([
            "set", ticket_id, "--taskbook", str(taskbook_dir / "{ticket}_改单.md"), "--by", SLOT,
        ], root)
        self.assertEqual(0, changed.returncode, changed.stderr)
        stored = json.loads(self.cli(["show", ticket_id], root).stdout)
        self.assertEqual(str(final_path), stored["任务书路径"])
        self.assertEqual("已核存在", stored["任务书校验"])
    def test_r4_a_fresh_ticket_can_be_voided_with_a_reason(self):
        ticket = self.dispatch("建错了的单")
        voided = self.service.void(ticket["编号"], "单号猜错了，重开一张", SLOT)
        self.assertEqual("作废", voided["状态"])
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("作废", stored["状态"])
        self.assertIn("单号猜错了", stored["流程提示"])
        row = [r for r in self.service.store.read_jsonl(self.service.store.log_path) if r["事件"] == "void"][-1]
        self.assertEqual((ticket["编号"], SLOT, "单号猜错了，重开一张"), (row["工单号"], row["发言人"], row["说明"]))
        self.assertIn("作废", model.DISPATCH_STATES)

    def test_r4_a_blocked_ticket_can_finally_be_voided_instead_of_hanging_forever(self):
        ticket = self.dispatch("挂着的阻塞单")
        self.service.block(ticket["编号"], "依赖的上游单也建错了")
        self.assertEqual("阻塞", self.service.store.load_ticket(ticket["编号"])["状态"])
        self.assertEqual("作废", self.service.void(ticket["编号"], "上游改了口径，这张不要了", "总编")["状态"])

    def test_r4_a_submitted_ticket_cannot_be_voided(self):
        ticket = self.to_judging()
        with self.assertRaises(TicketError) as caught:
            self.service.void(ticket["编号"], "不想要了", SLOT)
        message = str(caught.exception)
        self.assertIn("现在是「待判」", message)
        self.assertIn("新建 / 已认领 / 阻塞", message)
        self.assertIn("交了板的活不许一笔勾销", message)
        self.assertEqual("待判", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_r4_void_needs_a_reason_and_the_owning_slot_or_the_conductor(self):
        ticket = self.dispatch("作废也有闸")
        with self.assertRaisesRegex(TicketError, "原因必填"):
            self.service.void(ticket["编号"], "   ", SLOT)
        with self.assertRaises(TicketError) as caught:
            self.service.void(ticket["编号"], "别位想废掉", OTHER_SLOT)
        self.assertIn("只有该位总监", str(caught.exception))
        self.assertEqual("新建", self.service.store.load_ticket(ticket["编号"])["状态"])
        self.service.void(ticket["编号"], "本位总监废掉", SLOT)
        with self.assertRaisesRegex(TicketError, "已经是「作废」"):
            self.service.void(ticket["编号"], "再废一次", SLOT)

    def test_r4_a_voided_ticket_cannot_be_edited_any_more(self):
        ticket = self.dispatch("作废之后不许再改")
        self.service.void(ticket["编号"], "建错了", SLOT)
        with self.assertRaisesRegex(TicketError, "现在是「作废」"):
            self.service.edit(ticket["编号"], SLOT, taskbook="D:/a.md")

    def test_r4_cli_void_then_show_and_list(self):
        root = self.root / "cli-void"
        created = self.cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行作废",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        voided = self.cli(["void", ticket_id, "--reason", "建错了", "--by", SLOT], root)
        self.assertEqual(0, voided.returncode, voided.stderr)
        self.assertIn("作废", voided.stdout)
        self.assertEqual("作废", json.loads(self.cli(["show", ticket_id], root).stdout)["状态"])

    def test_r4_front_end_treats_void_as_a_terminal_state_everywhere(self):
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn('const TERMINAL_STATES = ["实机复验过","关闭","作废"];', script)
        self.assertIn('const doneRows=["已答","关闭","已合并","实机复验过","作废"].map', script)
        # 这两处的判据从 TERMINAL_STATES 换成了 isTerminal()(它把 TERMINAL_STATES 全含进去,
        # 另加「内部单已合并即终态」)。本条守的仍是「作废在各处都当终态」,只是判据换了形。
        self.assertIn("if(isTerminal(t))", script)
        self.assertIn("isOpened(t)&&!isTerminal(t)", script)
        self.assertIn('NON_STALE_STATES.has(t.状态)', script)  # 作废仍在 NON_STALE_STATES 里
        self.assertIn("阻塞:-1,作废:-1}", script)
        self.assertIn("已答:['close'],作废:[]}", script)
        self.assertIn("const attach=!['关闭','作废'].includes(t.状态)?", script)
        self.assertIn(".ticket.voided", (ROOT / "tools" / "browser" / "tickets.css").read_text(encoding="utf-8"))
    def test_r5_default_main_model_set_follows_the_v25_roster(self):
        slots = self.service.store.read_json(self.service.store.slots_path)
        self.assertEqual(["sol", "opus", "fable"], slots["主力模型集合"])
        self.assertEqual("", self.service.staff_new(SLOT, "fable")["提示"])

    def test_r5_an_old_store_still_carrying_the_v24_pair_is_topped_up(self):
        root = self.root / "老库"
        store = TicketStore(root)
        store.ensure()
        slots = store.read_json(store.slots_path)
        slots["主力模型集合"] = ["sol", "opus"]
        store.atomic_json(store.slots_path, slots)
        TicketService(TicketStore(root))
        self.assertEqual(["sol", "opus", "fable"], store.read_json(store.slots_path)["主力模型集合"])

    def test_r5_a_hand_edited_main_model_set_is_left_alone(self):
        root = self.root / "手改过的库"
        store = TicketStore(root)
        store.ensure()
        slots = store.read_json(store.slots_path)
        slots["主力模型集合"] = ["opus"]
        store.atomic_json(store.slots_path, slots)
        TicketService(TicketStore(root))
        self.assertEqual(["opus"], store.read_json(store.slots_path)["主力模型集合"])

    def test_r5_non_main_model_notice_says_which_tiers_it_may_take(self):
        notice = self.service.staff_new(OTHER_SLOT, "glm-5.3")["提示"]
        self.assertIn("不在主力模型名册里", notice)
        self.assertIn("当前主力是 sol、opus、fable", notice)
        self.assertIn("只吃丙档", notice)
        self.assertIn("2000 行", notice)
        self.assertNotIn("['opus', 'sol']", notice)
        self.assertEqual("在岗", self.service.find_staff(f"{OTHER_SLOT}-01")[1]["状态"])
    def old_ticket_without_taskbook(self, title: str = "老单没有任务书路径"):
        """造一张之前建的单：文件里根本没有「任务书路径」这个键。"""
        ticket = self.dispatch(title)
        path = self.service.store.item_path(ticket["编号"])
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored.pop("任务书路径")
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        return ticket["编号"]

    def test_r6_an_old_ticket_without_the_taskbook_key_still_reads_and_writes(self):
        ticket_id = self.old_ticket_without_taskbook()
        loaded = self.service.store.load_ticket(ticket_id)
        self.assertNotIn("任务书路径", loaded)
        self.assertEqual("", loaded.get("任务书路径", ""))
        self.assertEqual(ticket_id, self.service.claim(ticket_id, self.worker)["编号"])
        path = r"D:\project\_office\前端·页面接线\任务书\%s_补路径.md" % ticket_id
        updated, changes = self.service.edit(ticket_id, SLOT, taskbook=path)
        self.assertEqual(path, updated["任务书路径"])
        self.assertEqual([{"字段": "任务书路径", "旧值": "", "新值": path}], changes)
        self.assertEqual(path, self.service.store.load_ticket(ticket_id)["任务书路径"])

    def test_r6_an_old_ticket_without_the_taskbook_key_can_still_be_voided_and_bundled(self):
        ticket_id = self.old_ticket_without_taskbook("老单也要能作废")
        self.assertEqual("作废", self.service.void(ticket_id, "老单建错了", "总编")["状态"])
        bundle = self.service.build_bundle(self.root / "bundle.js")
        payload = json.loads(bundle.read_text(encoding="utf-8").split(" = ", 1)[1].rstrip(";\n"))
        row = next(item for item in payload["items"] if item["编号"] == ticket_id)
        self.assertEqual("作废", row["状态"])
        self.assertEqual("", row.get("任务书路径", ""))


class PlayerFacingFlagEditTests(TicketTestCase):
    """建单标错「玩家可感知」之后,总监要能就地改回来。

     实撞:一张只改 UiP0Tests.cs 两行断言的内部单,建单时漏了 --internal,
    员工把活全干完、提交也推了,交板却被「必须附 真登录图」的闸拦死。
    这个标记以前只能在 new 时定,set 改不了 —— 于是只剩两条路:作废重建(活白干一轮),
    或者拿一张无关截图糊弄闸(比卡住还坏)。两条都不该走。
    现在可感知单附真登录图已改为选填,不附也能交板;但标记仍要能就地改——两支交板要的东西不同
    (内部单必须给验证命令与原样输出,可感知单两字段选填、附了的图照挂)。
    """

    def test_marking_internal_lets_the_same_submit_through_without_any_picture(self):
        # 改之前:可感知单不附图也能交板(附图选填)——另起一张单实跑为证,
        # 因为交过板的单到了待判就不能再 set 了。
        facing = self.dispatch("可感知单不附图")
        self.service.claim(facing["编号"], self.worker)
        passed = self.service.submit(facing["编号"], "只改测试断言", "dotnet test", "49/49 绿")
        self.assertEqual("待判", passed["状态"])
        self.assertFalse(passed["非玩家可感知"])
        self.assertEqual([], passed["接线证据"]["图片列表"])

        ticket = self.dispatch("标错玩家可感知的内部单")
        number = ticket["编号"]
        self.service.claim(number, self.worker)
        _, changes = self.service.edit(number, SLOT, internal=True)
        self.assertEqual(
            [{"字段": "非玩家可感知", "旧值": False, "新值": True}], changes
        )

        # 改之后走内部单那一支:缺验证命令与原样输出照样拦;两样都给、不补任何截图,直接过。
        with self.assertRaisesRegex(TicketError, "验证命令与原样输出"):
            self.service.submit(number, "只改测试断言")
        submitted = self.service.submit(number, "只改测试断言", "dotnet test", "49/49 绿")
        self.assertEqual("待判", submitted["状态"])
        self.assertEqual([], submitted["图片列表"])

    def test_marking_player_facing_again_puts_the_player_facing_submit_back(self):
        ticket = self.dispatch("改回玩家可感知")
        number = ticket["编号"]
        self.service.claim(number, self.worker)
        self.service.edit(number, SLOT, internal=True)
        # 内部单那一支:不给验证命令与原样输出就拦。
        with self.assertRaisesRegex(TicketError, "验证命令与原样输出"):
            self.service.submit(number, "改回来了")
        self.service.edit(number, SLOT, internal=False)

        # 改回可感知:两字段不给也能交;附了的真登录图照挂,来源照记。
        self.service.attach(number, str(self.picture("back.png")), "world", self.worker)
        submitted = self.service.submit(number, "改回来了")
        self.assertEqual("待判", submitted["状态"])
        self.assertFalse(submitted["非玩家可感知"])
        self.assertEqual(["真登录"], [row["来源标注"] for row in submitted["接线证据"]["图片列表"]])

    def test_only_the_owning_director_or_conductor_can_flip_the_flag(self):
        ticket = self.dispatch("员工不许自己改标记")
        number = ticket["编号"]
        self.service.claim(number, self.worker)
        with self.assertRaises(TicketError) as refused:
            self.service.edit(number, self.worker, internal=True)
        self.assertIn("只有该位总监", str(refused.exception))
        # 总编可以。
        _, changes = self.service.edit(number, "总编", internal=True)
        self.assertTrue(changes)

    def test_flipping_the_flag_is_written_to_the_event_line_with_both_values(self):
        """总编点名要的:谁、何时、从什么改成什么,一条都不能少。"""
        ticket = self.dispatch("事件线要记全")
        number = ticket["编号"]
        self.service.edit(number, SLOT, internal=True)
        rows = [
            json.loads(line)
            for line in (self.service.store.root / "log.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        entry = next(
            row for row in rows
            if row.get("工单号") == number and "非玩家可感知" in str(row.get("说明", ""))
        )
        self.assertEqual("set", entry["事件"])
        self.assertEqual(SLOT, entry["发言人"])
        self.assertIn("False → True", entry["说明"])
        self.assertTrue(entry["时间"])

    def test_the_cli_rejects_both_switches_at_once(self):
        root = self.root / "facing-cli"
        deliverable = root / "a.cs"
        deliverable.parent.mkdir(parents=True, exist_ok=True)
        deliverable.write_text("// x\n", encoding="utf-8")
        run_local_cli(["staff", "new", "--slot", SLOT, "--tool", "待定"], root)
        created = run_local_cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "互斥验证",
            "--assign", f"{SLOT}-01", "--consumer", "主界面/面板根",
            "--source", "DECISIONS.md:测试", "--tier", "乙",
            "--deliverable", str(deliverable), "--by", SLOT, "--player-facing",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        number = created_ticket_id(created)
        both = run_local_cli(["set", number, "--internal", "--player-facing", "--by", SLOT], root)
        self.assertEqual(2, both.returncode)

        only_internal = run_local_cli(["set", number, "--internal", "--by", SLOT], root)
        self.assertEqual(0, only_internal.returncode, only_internal.stderr)
        self.assertIn("非玩家可感知", only_internal.stdout)


class SetTierEditTests(TicketTestCase):
    """set --tier 甲|乙|丙(丙升乙实撞)。

    任务书重写换了档,档位字段却改不了——卡片与开窗指令还按旧档走,
    设计者照旧档选模型。档位是总监的判断,允许就地改,记进改动日志。
    """

    def test_1_owner_changes_tier_and_it_lands_with_a_change_row(self):
        ticket = self.dispatch("任务书从丙重写成乙的单")
        self.service.claim(ticket["编号"], self.worker)
        # 建单夹具本来就是乙档;先把档真的改一次到丙,再验证能改回来,顺带拿到非平凡的旧值。
        updated, changes = self.service.edit(ticket["编号"], SLOT, task_tier="丙")
        self.assertEqual([{"字段": "任务档", "旧值": "乙", "新值": "丙"}], changes)
        updated, changes = self.service.edit(ticket["编号"], "总编", task_tier="乙")
        self.assertEqual([{"字段": "任务档", "旧值": "丙", "新值": "乙"}], changes)
        self.assertEqual("乙", self.service.store.load_ticket(ticket["编号"])["任务档"])

    def test_2_other_slots_and_bad_values_are_refused(self):
        ticket = self.dispatch()
        with self.assertRaisesRegex(TicketError, "任务档只能是"):
            self.service.edit(ticket["编号"], SLOT, task_tier="丁")
        with self.assertRaises(TicketError):
            self.service.edit(ticket["编号"], OTHER_SLOT, task_tier="甲")
        # 被拦下时档位原样。
        self.assertEqual("乙", self.service.store.load_ticket(ticket["编号"])["任务档"])

    def test_3_judging_state_still_only_allows_assign(self):
        ticket = self.to_judging()
        with self.assertRaisesRegex(TicketError, "待判"):
            self.service.edit(ticket["编号"], SLOT, task_tier="甲")

    def test_4_no_editable_item_message_lists_tier(self):
        ticket = self.dispatch()
        with self.assertRaisesRegex(TicketError, "--tier 任务档"):
            self.service.edit(ticket["编号"], SLOT)


class DeliverableTicketPlaceholderTests(TicketTestCase):
    """交付项 {ticket} 占位替换(实撞,任务书同款教训)。

    预写 T-0018xx 这类占位会被别位插队占号,拿到真号就对不上,单子卡在交板闸
    ——后端位为此报过非业务阻塞,只能等总监手工 set 改交付项。任务书早支持
    {ticket},交付项同一个待遇:服务端取号后替换,库里永远不留未替换的占位。
    """

    def test_1_new_substitutes_the_assigned_number(self):
        ticket = self.service.create_dispatch(
            SLOT, "占位交付项的单", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="乙", deliverables=["review/map/{ticket}_回执.md"], internal=False,
        )
        number = ticket["编号"]
        self.assertEqual([f"review/map/{number}_回执.md"], ticket["交付项"])
        stored = self.service.store.load_ticket(number)["交付项"]
        self.assertFalse(any("{ticket}" in str(row) for row in stored), stored)

    def test_2_set_substitutes_with_this_tickets_number(self):
        ticket = self.dispatch()
        updated, changes = self.service.edit(ticket["编号"], SLOT, deliverables=["x/{ticket}_a.md"])
        self.assertEqual([f"x/{ticket['编号']}_a.md"], updated["交付项"])


class BlockingNewBodyTests(TicketTestCase):
    """阻塞型 new 的 --body 曾被静默丢弃(前端·交互 实撞)。

    员工写完长正文、以为已落,建出来却是默认句;疑问分支一直是 body or notes,
    阻塞分支漏了 --body 兜底。与疑问同形:body 优先、notes 兜底、default 垫底。
    """

    def test_1_body_wins_then_notes_then_default(self):
        body = run_local_cli(
            ["new", "--type", "阻塞", "--slot", SLOT, "--title", "带正文的阻塞",
             "--body", "长正文在此:具体事实与影响面", "--by", SLOT],
            self.service.store.root,
        )
        self.assertEqual(0, body.returncode, body.stderr)
        self.assertEqual(
            "长正文在此:具体事实与影响面",
            self.service.store.load_ticket(created_ticket_id(body))["正文"],
        )
        notes = run_local_cli(
            ["new", "--type", "阻塞", "--slot", SLOT, "--title", "只给备注", "--notes", "备注内容", "--by", SLOT],
            self.service.store.root,
        )
        self.assertEqual(0, notes.returncode, notes.stderr)
        self.assertEqual("备注内容", self.service.store.load_ticket(created_ticket_id(notes))["正文"])
        blank = run_local_cli(
            ["new", "--type", "阻塞", "--slot", SLOT, "--title", "都不给", "--by", SLOT],
            self.service.store.root,
        )
        self.assertEqual(0, blank.returncode, blank.stderr)
        self.assertEqual("需要总编处理阻塞。", self.service.store.load_ticket(created_ticket_id(blank))["正文"])


class ShotBlockedSubmitTests(TicketTestCase):
    """取图受阻的合法 submit 路径。

    员工窗够不着真实运行环境 时原来只有两条烂路:硬拦(活卡死)、改标内部单(错标签,复检不认)。
    现在第三条:--shot-blocked + 机器闸原样输出,如实记账「欠真登录图」,
    复检席现网补图(attach world)即清,再 live。
    """

    def blocked_ticket(self):
        ticket = self.dispatch("取图受阻单")
        self.service.claim(ticket["编号"], self.worker)
        return ticket

    def test_1_shot_blocked_submits_with_marker_and_record(self):
        ticket = self.blocked_ticket()
        submitted = self.service.submit(
            ticket["编号"], "", "dotnet test", "0 failed",
            shot_blocked="员工窗在服务器上,够不着真实运行环境的真登录",
        )
        self.assertEqual("待判", submitted["状态"])
        marker = submitted["欠真登录图"]
        self.assertIn("够不着真实运行环境", marker["说明"])
        self.assertEqual(self.worker, marker["提交人"])
        events = self.service.store.read_jsonl(self.service.store.log_path)
        submit_events = [row for row in events if row.get("事件") == "submit" and row.get("工单号") == ticket["编号"]]
        self.assertIn("欠真登录图", submit_events[-1]["说明"])
        from tools.tickets.ticket import compact_ticket
        self.assertIn("欠真登录图", compact_ticket(submitted))

    def test_2_requires_machine_output_and_is_refused_for_internal(self):
        ticket = self.blocked_ticket()
        with self.assertRaisesRegex(TicketError, "必须同时带"):
            self.service.submit(ticket["编号"], "", "", "", shot_blocked="只说一句")
        # 内部单根本不要图,--shot-blocked 套上去是误用;给明确拒绝,不静默吞掉。
        fresh = self.service.create_dispatch(
            SLOT, "内部误用", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(fresh["编号"], self.worker)
        with self.assertRaisesRegex(TicketError, "只给玩家可感知单"):
            self.service.submit(fresh["编号"], "验证完成", "pytest", "all passed", shot_blocked="误用")

    def test_3_live_refuses_until_world_image_clears_the_marker(self):
        ticket = self.blocked_ticket()
        self.service.submit(
            ticket["编号"], "", "dotnet test", "0 failed", shot_blocked="够不着真实运行环境",
        )
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.merged_ticket(ticket["编号"], "独立复检")   # 判过 ∧ 复验过 → 已合并
        # 标记未清:live 拒,并指到现网补图那一步。
        with self.assertRaisesRegex(TicketError, "欠真登录图"):
            self.service.live(ticket["编号"], str(self.picture()), "独立复检", "同图")
        # 复检席现网补图:attach 一张 world 图即清(事件线留痕)。
        self.service.attach(ticket["编号"], str(self.picture("onsite-fix.png")), "world", "独立复检")
        self.assertNotIn("欠真登录图", self.service.store.load_ticket(ticket["编号"]))
        events = self.service.store.read_jsonl(self.service.store.log_path)
        attach_events = [row for row in events if row.get("事件") == "attach" and row.get("工单号") == ticket["编号"]]
        self.assertIn("欠真登录图已清", attach_events[-1]["说明"])
        # 清后 live 走正常路。
        lived = self.service.live(ticket["编号"], str(self.picture("onsite-2.png")), "独立复检", "同图")
        self.assertEqual("实机复验过", lived["状态"])

    def test_4_no_picture_submits_without_relabeling_or_marker(self):
        """附真登录图改为选填:不附图直接交板,不用翻内部单标签,也不打「欠真登录图」标记。"""
        ticket = self.blocked_ticket()
        submitted = self.service.submit(ticket["编号"], "无图交板")
        self.assertEqual("待判", submitted["状态"])
        self.assertFalse(submitted["非玩家可感知"])
        self.assertNotIn("欠真登录图", submitted)
        self.assertEqual([], submitted["接线证据"]["图片列表"])
        events = self.service.store.read_jsonl(self.service.store.log_path)
        submit_events = [row for row in events if row.get("事件") == "submit" and row.get("工单号") == ticket["编号"]]
        self.assertNotIn("欠真登录图", submit_events[-1]["说明"])

    def test_5_the_dispatch_card_stays_three_lines_and_keeps_step_zero_out(self):
        """★开窗指令恒为三行;任务书第 0 步那一步不许再挤进卡片。

         曾把它加成五行,设计者 2026-09-14 拿队列页截图当窗定了
        那两行根本不该出现在卡片上——不是排版坏了,是位置错了:
         早就把那一步定在**每份任务书的第 0 步**,
        写进卡片等于每张单重复一遍任务书里已经有的东西。

        ★这条闸只管「别再挤进卡片」。那一步本身的两个 shell 写法都还在
        `templates/丙档任务书模板.md` 的 §0 与 README 里,由 test_6 守着,删不掉。
        """
        taskbook = self.root / "tb-three-lines.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        ticket = self.service.create_dispatch(
            SLOT, "开窗指令三行", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="丙", context_lines=50, deliverables=[str(self.deliverable)],
            taskbook=str(taskbook), internal=True,
        )
        lines = self.service.dispatch_instructions(ticket)
        self.assertEqual(3, len(lines), lines)
        self.assertIn(f" claim {ticket['编号']} --by {self.worker}", lines[0])
        self.assertTrue(lines[1].startswith("执行 "), lines[1])
        self.assertTrue(lines[2].startswith("【操作提示·只给设计者】"), lines[2])
        # 逐行拦第 0 步的特征:谁再把它塞回来,这里当场红。
        for row in lines:
            for mark in ("remote.env", "set -a", "Get-Content", "Set-Item", "ForEach-Object"):
                self.assertNotIn(mark, row, f"任务书第 0 步的东西又跑进开窗指令了:{row}")

    def test_6_step_zero_keeps_both_shells_where_it_actually_lives(self):
        """撤掉卡片那两行不等于把 PowerShell 那条路删了——它的家在任务书 §0 与 README。

         的起因是真的(默认 PowerShell 的窗走不通 bash 那一句,五张单实撞),
        所以撤位置可以,撤内容不行。这条闸盯着真正该放它的两处。
        """
        template = ROOT / "tools" / "tickets" / "templates" / "丙档任务书模板.md"
        section = template.read_text(encoding="utf-8").split("## §0", 1)[1].split("\n## ", 1)[0]
        self.assertIn("set -a; source <remote.env>; set +a", section)
        self.assertIn("Get-Content <remote.env>", section)
        self.assertIn("Set-Item", section)

        readme = (ROOT / "tools" / "tickets" / "README.md").read_text(encoding="utf-8")
        connect = readme.split("## 员工窗怎么接通道", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Get-Content ", connect)
        self.assertIn("remote.env", connect)
        self.assertIn("Set-Item", connect)

    def test_6_shot_blocked_is_refused_when_the_ticket_already_has_a_world_image(self):
        """已有真登录图还带 --shot-blocked = 与事实相反,而且会把单锁死。

        标记只有「再附一张 world 图」这一条清法,可图早就在单上了——
        原来这一支静默打标记,单卡挂上「欠真登录图」、live 被挡,复检席只能再传一张
        一模一样的图去骗过闸。内部单误用那一支给的是明确拒绝,这一支同形。
        """
        ticket = self.blocked_ticket()
        self.service.attach(ticket["编号"], str(self.picture("world-ok.png")), "world", self.worker)
        with self.assertRaisesRegex(TicketError, "已经有 1 张真登录图"):
            self.service.submit(
                ticket["编号"], "已附真登录图", "dotnet test", "0 failed", shot_blocked="手滑带上了",
            )
        # 拦下不动状态:去掉那个参数原样重跑就能过。
        self.assertEqual("已认领", self.service.store.load_ticket(ticket["编号"])["状态"])
        self.assertNotIn("欠真登录图", self.service.store.load_ticket(ticket["编号"]))
        submitted = self.service.submit(ticket["编号"], "已附真登录图", "dotnet test", "0 failed")
        self.assertEqual("待判", submitted["状态"])
        self.assertNotIn("欠真登录图", submitted)


class RefusalCommandsMustParseTests(TicketTestCase):
    """★闸:回话里给人跑的 `ticket.py …` 命令,必须真能被本工具的 parser 吃下去。

     实撞:欠真登录图的 live 拒绝语写的是
    `ticket.py attach <单号> <图> world`,而 attach 的来源是 `--origin world`。
    照那句原样跑回的是「缺少必填参数：--origin」——出路指到了一条死路,
    而「欠真登录图」的标记只有 attach 这一条清法,于是整张单锁死。

    单测原来只调 service.attach(...),走的是 API 那条路,永远碰不到这句话里的字面
    (同题见「钉源码文本的用例挡不住行为坏掉」的反面:文案自己也是要跑的东西)。
    这条闸把话里的命令抠出来喂给真 parser,不判语义只判「能不能跑起来」。
    """

    #: 占位符一律换成一个不含空格的词,再按中文句读切断命令。
    PLACEHOLDER_RE = re.compile(r"<[^<>]*>")
    COMMAND_RE = re.compile(r"ticket\.py\s+(.+?)(?=——|[；;。，,）)]|$)")

    def commands_in(self, text: str) -> list[list[str]]:
        flat = self.PLACEHOLDER_RE.sub("占位", str(text))
        found = []
        for row in self.COMMAND_RE.findall(flat):
            try:
                found.append(shlex.split(row.strip()))
            except ValueError as error:          # 引号不成对也是坏文案
                self.fail(f"这句命令连引号都不成对,拆不开:{row!r}（{error}）")
        return found

    def assert_runnable(self, text: str) -> int:
        commands = self.commands_in(text)
        self.assertTrue(commands, f"这段话里没找到 ticket.py 命令,闸抓空了:{text!r}")
        for argv in commands:
            with contextlib.redirect_stderr(io.StringIO()) as noise:
                try:
                    cli_parser().parse_args(argv)
                except SystemExit:
                    self.fail(
                        f"回话里让人跑的命令,本工具自己吃不下去:\n"
                        f"  ticket.py {' '.join(argv)}\n"
                        f"  parser 说:{noise.getvalue().strip()}\n"
                        f"  出处:{text!r}"
                    )
        return len(commands)

    def test_1_live_refusal_hands_over_a_runnable_attach(self):
        ticket = self.dispatch("取图受阻单")
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "", "dotnet test", "0 failed", shot_blocked="够不着真实运行环境")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.merged_ticket(ticket["编号"], "独立复检")
        with self.assertRaises(TicketError) as ctx:
            self.service.live(ticket["编号"], str(self.picture()), "独立复检", "同图")
        self.assertEqual(1, self.assert_runnable(str(ctx.exception)))

    def test_2_submit_refusal_hands_over_a_runnable_submit(self):
        # 无图交板已放行(附真登录图选填);交板侧带命令的拒绝语是 --shot-blocked 缺机器输出那一条。
        ticket = self.dispatch("取图受阻缺输出单")
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as ctx:
            self.service.submit(ticket["编号"], "无图交板", shot_blocked="够不着真实运行环境")
        self.assertEqual(1, self.assert_runnable(str(ctx.exception)))
        self.assertEqual("submit", self.commands_in(str(ctx.exception))[0][0])

    def test_3_every_help_text_that_shows_a_command_shows_a_runnable_one(self):
        """帮助文里的命令同样是给人照抄的——`--shot-blocked` 那条当初就抄坏了。"""
        checked = 0
        for action in cli_parser()._subparsers._group_actions:       # noqa: SLF001 - argparse 没有公开入口
            for sub in action.choices.values():
                for option in sub._actions:                          # noqa: SLF001
                    help_text = option.help or ""
                    if "ticket.py " not in help_text:
                        continue
                    checked += self.assert_runnable(help_text)
        self.assertGreater(checked, 0, "一条带命令的帮助文都没扫到,闸抓空了")


class SubmitHygieneTests(TicketTestCase):
    """交板侧仓库卫生自查:报而不拦,清单入交板记录与单卡。

    复检实撞:两张甲档因测试日志入仓只在并线侧被拦,判卷人没看见——
    规矩只长在最后一道,员工重开窗重推支的代价全白付。
    """

    def test_1_filter_matches_only_repo_hygiene_paths(self):
        from tools.tickets.ticket import hygiene_hits
        rows = [
            "artifacts/run.log", "docs/evidence/raw.zip", "docs/evidence/a.trx",
            "artifacts/pack.7z", "docs/evidence/b.rar", "artifacts/Upper.LOG",
            "artifacts/c.png", "docs/evidence/d.jpg",   # ≤204,800 字节的判据图不进扫描
            "tools/x.log", "src/artifacts2/e.log", "artifacts/notes.md",
        ]
        self.assertEqual(
            [
                "artifacts/run.log", "docs/evidence/raw.zip", "docs/evidence/a.trx",
                "artifacts/pack.7z", "docs/evidence/b.rar", "artifacts/Upper.LOG",
            ],
            hygiene_hits(rows),
        )

    def test_2_real_git_tree_scan_finds_and_clears(self):
        """真跑 git:基线后加了 artifacts/run.log 命中;清掉并忽略后零命中。"""
        from tools.tickets.ticket import _branch_hygiene_scan
        repo = self.root / "hygiene-repo"
        repo.mkdir()

        def git(*argv: str):
            subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (repo / "README.md").write_text("base", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "base")
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", base], cwd=repo, check=True)
        (repo / "artifacts").mkdir()
        (repo / "artifacts" / "run.log").write_text("x", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "dirty")
        self.assertEqual(["artifacts/run.log"], _branch_hygiene_scan(repo))
        git("rm", "-q", "--cached", "artifacts/run.log")
        (repo / ".gitignore").write_text("artifacts/\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "clean")
        self.assertEqual([], _branch_hygiene_scan(repo))
        # 不在 git 树里:返回 None(调用方跳过,不卡交板)。
        plain = self.root / "not-a-repo"
        plain.mkdir()
        self.assertIsNone(_branch_hygiene_scan(plain))

    def test_3_submit_records_hits_on_ticket_and_in_the_event(self):
        ticket = self.dispatch("卫生命中单")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        submitted = self.service.submit(
            ticket["编号"], "登录后界面已出现", hygiene_hits=["artifacts/run.log", "docs/evidence/raw.zip"],
        )
        self.assertEqual(["artifacts/run.log", "docs/evidence/raw.zip"], submitted["仓库卫生"]["命中"])
        view = self.service.card_view(self.service.store.load_ticket(ticket["编号"]))
        self.assertIn("仓库卫生", view, "单卡数据要带上命中清单,判卷人才看得见")
        events = self.service.store.read_jsonl(self.service.store.log_path)
        submit_events = [row for row in events if row.get("事件") == "submit" and row.get("工单号") == ticket["编号"]]
        self.assertIn("仓库卫生命中 2 件", submit_events[-1]["说明"])

    def test_4_clean_submit_notes_zero_hits_and_leaves_no_field(self):
        ticket = self.to_judging()
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertNotIn("仓库卫生", stored)
        events = self.service.store.read_jsonl(self.service.store.log_path)
        submit_events = [row for row in events if row.get("事件") == "submit" and row.get("工单号") == ticket["编号"]]
        self.assertIn("仓库卫生:上述后缀零命中", submit_events[-1]["说明"])


class UnrecognizedOptionHintTests(TicketTestCase):
    """「参数不对」多一行版本差提示(总编收时实撞)。"""

    def test_1_unknown_option_error_carries_the_pull_hint(self):
        from tools.tickets.ticket import parser as ticket_parser
        with self.assertRaisesRegex(TicketError, "git pull 主检出"):
            ticket_parser().parse_args(["set", "T-000001", "--no-such-option", "--by", "x"])

    def test_4_the_message_names_the_offending_option_and_a_real_help_command(self):
        """★原来这句话把 argparse 自己那句整个吞掉,只留「多半是本机检出旧」一种解释。

        本位实撞:给 submit 写了个它根本没有的 --by,照那句提示去换检出、核服务端协议,白跑两趟——
        而 argparse 早就说了「unrecognized arguments: --by」。名字写错与检出旧是两种情形,
        报错必须让人当场分开。
        """
        from tools.tickets.ticket import parser as ticket_parser
        with self.assertRaises(TicketError) as ctx:
            ticket_parser().parse_args(["submit", "T-000001", "--by", "某位-01"])
        message = str(ctx.exception)
        self.assertIn("--by", message, "要说得出是哪个参数不对")
        self.assertIn("ticket.py submit --help", message, "要指到那条命令自己的参数名单")
        self.assertIn("名字写错", message, "两种情形都要说,不能只说检出旧")
        self.assertIn("git pull 主检出", message)

    def test_5_the_help_command_it_points_at_really_runs(self):
        """★它指的那条出路必须真能跑(同 RefusalCommandsMustParseTests 那一族)。

        原来那句话没有可跑的出路;新版指到「ticket.py <子命令> --help」,
        这里把它从报错里抠出来、真喂给本工具的 parser,只判能不能跑起来。
        """
        from tools.tickets.ticket import parser as ticket_parser
        with self.assertRaises(TicketError) as ctx:
            # ★必填参数要给齐:argparse 先报缺参、后报 unrecognized,缺参那条走的是另一个分支。
            ticket_parser().parse_args([
                "judge", "T-000001", "--pass", "--by", "某位",
                "--verdict", "玩家怎么打开它:略", "--no-such-thing",
            ])
        quoted = re.search(r"请跑:ticket\.py (\S+) --help", str(ctx.exception))
        self.assertIsNotNone(quoted, str(ctx.exception))
        self.assertEqual(
            "judge", quoted.group(1),
            "★子命令要从正在解析的 argv 里取。读 sys.argv 在远程(/api/cli)与单测里都是别人的 argv,"
            "本位实撞时打出了「ticket.py tools/tickets/tests --help」;"
            "而 self.prog 在子解析器上已含子命令名,再补一次会打出「ticket.py judge T-000001」",
        )
        with self.assertRaises(SystemExit) as helped:   # --help 正常退出是 SystemExit(0)
            ticket_parser().parse_args([quoted.group(1), "--help"])
        self.assertEqual(0, helped.exception.code)

    def test_7_the_stale_checkout_hint_splits_merged_from_not_yet_merged(self):
        """★(后端总监 2026-09-16 实撞):「git pull 主检出后再试」对最常见的那个情形是错建议。

        「本机检出较旧」有两种子形态,而工单台的东西**一律先上服、后并 main**,所以更常见的是
        **参数还没进 main** ⇒ 主检出已经是最新的了,`git pull` 什么也拉不到。
        后端 0 号为此清不掉两笔记账(settle 零命中);本位上一轮也照这句话白跑两趟换检出。
        ⇒ 报错必须把两种子形态分开,并给出真能走的那条出路 + 一行判据。
        """
        from tools.tickets.ticket import parser as ticket_parser
        with self.assertRaises(TicketError) as ctx:
            ticket_parser().parse_args(["show", "T-000001", "--no-such-option"])
        message = str(ctx.exception)
        # 三种情形都要说到,不能只剩「多半是检出旧」一条。
        self.assertIn("名字写错", message)
        self.assertIn("已并进 main", message)
        self.assertIn("还没并进 main", message)
        # ★要点:后一种情形必须明说 git pull 治不了,否则人会反复 pull。
        self.assertIn("git pull 治不了", message)
        # ★出路要真能走,而且要给一行判据,不能只说「等并 main」。
        self.assertIn("grep -c", message, "要给一行自己就能跑的判据")
        self.assertIn("ticket.py", message)
        self.assertIn("remote.env", message, "换用别的检出那份 CLI 时推算不到远程配置,要提醒手动带上")

    def test_8_the_diagnostic_it_hands_out_really_works(self):
        """★它给的那行判据必须真管用(同 RefusalCommandsMustParseTests 那一族):

        拿本仓自己的 ticket.py 跑一遍 `grep -c`,已有的参数必须 >0、编出来的必须 =0。
        判据本身是假的,比不给判据更坏。
        """
        source = (ROOT / "tools" / "tickets" / "ticket.py").read_text(encoding="utf-8")
        self.assertGreater(source.count("--mark-read"), 0, "真有的参数应当数得出来")
        self.assertEqual(0, source.count("--definitely-not-a-real-option"), "没有的应当是 0")

    def test_6_a_missing_required_option_is_not_blamed_on_a_stale_checkout(self):
        """★「二选一必填」的缺参错误里也带 --,原来照样发「多半是检出旧」,把人往 git pull 上引。

        判据是 argparse 说的 unrecognized,不是消息里有没有出现 --。
        """
        from tools.tickets.ticket import parser as ticket_parser
        with self.assertRaises(TicketError) as ctx:
            ticket_parser().parse_args([
                "judge", "T-000001", "--by", "某位", "--verdict", "玩家怎么打开它:略",
            ])
        message = str(ctx.exception)
        self.assertIn("--pass", message, "要说得出缺的是哪两个之一")
        self.assertNotIn("git pull", message, "这不是检出旧,别把人往那条路上引")
        self.assertNotIn("--help", message)

    def test_2_missing_required_argument_error_does_not_carry_the_hint(self):
        from tools.tickets.ticket import parser as ticket_parser
        with self.assertRaises(TicketError) as ctx:
            ticket_parser().parse_args(["show"])
        self.assertIn("缺少必填参数", str(ctx.exception))
        self.assertNotIn("git pull", str(ctx.exception))

    def test_3_degradation_matcher_accepts_both_old_and_new_error_formats(self):
        from tools.tickets.remote import RemoteClient
        old_format = "ticket.py 参数不对，请检查命令写法。"
        new_format = (
            "ticket.py 参数不对，请检查命令写法。"
            "★若是新参数而本机检出较旧：工单台参数常先上服后并 main，服务端可能已认"
            "(env --probe 可核服务端协议)，git pull 主检出后再试。"
        )
        self.assertTrue(RemoteClient._is_unrecognized_option_error(old_format))
        self.assertTrue(RemoteClient._is_unrecognized_option_error(new_format))
        self.assertFalse(RemoteClient._is_unrecognized_option_error("ticket.py 缺少必填参数：ticket。"))

class ModelBanNotifyOnlyTests(TicketTestCase):
    """ 乙 / 2026-09-05 加急:自动停用关掉,到阈值只通知。

    今天工具两次把 sol 全项目停掉:8 次判退里 2 次判语明写出题责任、3 次是 sol-high/sol-ultra
    变体被归并——停用是误判,而一停就是所有 sol 员工窗全停。总编答: 的「停用」是
    「总编落笔」,工具自动写 bans 是实现时加的,关掉不必修宪。
    """

    def rework_once(self, title: str, verdict: str = "模型责任:验收不过") -> dict:
        ticket = self.dispatch(title)
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture(f"{title}.png")), "world", self.worker)
        self.service.submit(ticket["编号"], "登录后界面已出现")
        judged, _ = self.service.judge(ticket["编号"], False, SLOT, "返工", verdict)
        return judged

    def thread_lines(self, slot: str) -> str:
        path = self.service.store.root / "threads" / f"{slot}.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_reaching_threshold_writes_no_ban_and_notifies_conductor_and_owner(self):
        # R1.5之后主力模型(sol/opus/fable)到线只作质量提示;这里用非主力模型钉「原样措辞」。
        worker = self.service.staff_new(SLOT, "glm-5.3")["员工名"]
        for index in range(3):  # 同位阈值 3
            ticket = self.dispatch(f"判退{index}", assign=worker)
            self.service.claim(ticket["编号"], worker)
            self.service.attach(ticket["编号"], str(self.picture(f"判退{index}.png")), "world", worker)
            self.service.submit(ticket["编号"], "登录后界面已出现")
            self.service.judge(ticket["编号"], False, SLOT, "返工", "模型责任:验收不过")
        staff = self.service.store.load_staff()
        bans = staff.get("模型停用", {})
        self.assertEqual([], bans.get("全项目", []))
        self.assertEqual([], bans.get("按位", {}).get(SLOT, []))
        # 记分照记,只是不再写停用
        # 实际模型没填 → 记成 <工具>-未标,不并进工具名
        self.assertEqual(3, staff["模型记分"]["glm-5.3-未标"][SLOT])
        for slot in ("总编", SLOT):
            text = self.thread_lines(slot)
            self.assertIn("再判退 1 次就到停用线", text)
            self.assertIn("已到停用线", text)
            self.assertIn("自动停用已关", text)
        # 开窗仍然不被拦
        ticket = self.dispatch("停用线之后仍能开窗", assign=worker)
        self.service.open_window(ticket["编号"], "设计者", "glm-5.3")

    def test_verdict_headed_by_authoring_fault_is_not_counted_against_the_model(self):
        judged = self.rework_once("出题责任的判退", "出题责任:任务书把键名写错了,执行方照做无误")
        staff = self.service.store.load_staff()
        model = self.service.find_staff(self.worker)[1]["工具/窗类型"].strip().lower()
        self.assertNotIn(model, staff.get("模型记分", {}))
        self.assertEqual("出题", judged["返工原因列表"][-1]["责任"])

    def test_void_on_rework_state_needs_the_follow_up_ticket_number(self):
        judged = self.rework_once("母单")
        with self.assertRaises(TicketError):
            self.service.void(judged["编号"], "不要了", SLOT)
        voided = self.service.void(judged["编号"], "窗已关,续单 T-000999 接手", SLOT)
        self.assertEqual("作废", voided["状态"])
        self.assertEqual(1, voided["返工次数"])  # 母单返工次数保留,计入模型合格率

class ManualStaffBanTests(TicketTestCase):
    """ 乙口径缺的后半截——到停用线只通知,停不停由人手工落 staff ban。

    自动停用关掉之后，bans 里只会有人手工写的项；没有这条命令，「停用」这一半就只是嘴上说说。
    R1.5之后主力模型(sol/opus/fable)只有设计者能 ban,所以本类里总编落笔的
    场景一律改用非主力模型 glm-5.3;主力模型的闸本身由 BanLineCountTests.test_r3_1 钉。
    """

    def thread_lines(self, slot: str) -> str:
        path = self.service.store.root / "threads" / f"{slot}.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def events(self, name: str) -> list[dict]:
        return [row for row in self.service.store.read_jsonl(self.service.store.log_path) if row.get("事件") == name]

    def test_1_conductor_ban_blocks_open_window_and_staff_new(self):
        """①总编 ban 非主力模型之后,两条开窗路径都被拦。"""
        self.service.staff_ban("glm-5.3", "总编", reason="核过责任归属,确属模型责任")
        ticket = self.dispatch("停用后不该能开窗")
        with self.assertRaises(TicketError) as caught:
            self.service.open_window(ticket["编号"], "设计者", "glm-5.3")
        self.assertIn("已被手工停用（全项目）", str(caught.exception))
        self.assertIn("staff unban --tool glm-5.3", str(caught.exception))
        with self.assertRaises(TicketError) as caught_new:
            self.service.staff_new(SLOT, "glm-5.3")
        self.assertIn("已被手工停用（全项目）", str(caught_new.exception))

    def test_1b_slot_scoped_ban_only_blocks_that_slot(self):
        self.service.staff_ban("sol", "设计者", SLOT, "这一位连续判退,先只停这一位")
        with self.assertRaises(TicketError) as caught:
            self.service.staff_new(SLOT, "sol")
        self.assertIn(f"已被手工停用（限“{SLOT}”）", str(caught.exception))
        # 别位不受影响
        self.assertTrue(self.service.staff_new(OTHER_SLOT, "sol")["员工名"])

    def test_2_staff_and_other_directors_cannot_ban(self):
        """②员工/别位总监 ban 被拒——停用是人的动作,而且只有那两个人能落笔。"""
        for actor in (self.worker, SLOT, "美术·视觉三", "工单台"):
            with self.assertRaises(TicketError) as caught:
                self.service.staff_ban("sol", actor, reason="我觉得该停")
            self.assertIn("只有设计者或总编可以停用模型", str(caught.exception))
        self.assertEqual([], self.service.store.load_staff()["模型停用"]["全项目"])

    def test_3_ban_writes_one_event_line_and_one_conductor_line(self):
        """③事件线与总编线各一条,原因与解禁命令都写在里面。"""
        detail = self.service.staff_ban("Glm  5.3 Middle", "总编", reason="三次判退全是模型责任")
        self.assertIn("模型 glm-5.3-middle 已手工停用（全项目）", detail)
        self.assertIn("原因：三次判退全是模型责任", detail)
        self.assertIn("解禁 staff unban --tool glm-5.3-middle --by 总编", detail)
        rows = self.events("staff-ban")
        self.assertEqual(1, len(rows))
        self.assertEqual("总编", rows[0]["发言人"])
        self.assertEqual(detail, rows[0]["说明"])
        conductor = [line for line in self.thread_lines("总编").splitlines() if "已手工停用" in line]
        self.assertEqual(1, len(conductor))
        # 模型名先 normalize:写进 bans 的是记账键,不是用户敲的原样
        self.assertEqual(["glm-5.3-middle"], self.service.store.load_staff()["模型停用"]["全项目"])

    def test_3b_ban_without_reason_is_rejected(self):
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("glm-5.3", "总编", reason="   ")
        self.assertIn("必须写原因", str(caught.exception))
        self.assertEqual([], self.events("staff-ban"))

    def test_4_banning_twice_is_rejected(self):
        """④重复 ban 被拒,且不写第二条事件线。"""
        self.service.staff_ban("glm-5.3", "总编", reason="第一次")
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("GLM-5.3", "总编", reason="第二次")
        self.assertIn("已在停用中（全项目）", str(caught.exception))
        # 全项目已停时,再按位停也拦下:范围更大的那一条已经生效
        with self.assertRaises(TicketError) as narrower:
            self.service.staff_ban("glm-5.3", "总编", SLOT, "再按位停一次")
        self.assertIn("已在停用中（全项目）", str(narrower.exception))
        self.assertEqual(["glm-5.3"], self.service.store.load_staff()["模型停用"]["全项目"])
        self.assertEqual(1, len(self.events("staff-ban")))

    def test_5_unban_restores_both_paths(self):
        """⑤unban 之后 staff new 与 open_window 都恢复。主力模型由设计者落 ban(R1.5)。"""
        self.service.staff_ban("sol", "设计者", reason="先停")
        self.service.staff_unban("sol", "总编")
        self.assertTrue(self.service.staff_new(SLOT, "sol")["员工名"])
        ticket = self.dispatch("解禁后应能开窗")
        self.service.open_window(ticket["编号"], "设计者", "sol")
        self.assertEqual("sol", self.service.store.load_ticket(ticket["编号"])["实际模型"])

    def test_6_unknown_slot_is_rejected(self):
        with self.assertRaises(TicketError) as caught:
            self.service.staff_ban("glm-5.3", "总编", "不存在的位", "理由")
        self.assertIn("总监位不在名册里", str(caught.exception))

    def test_7_cli_exposes_ban_with_the_same_permission_gate(self):
        """命令行这一层也要通:总编能停非主力模型,员工被拒,--reason 必填。"""
        root = self.root / "cli-ban"
        ok = run_local_cli(
            ["staff", "ban", "--tool", "glm-5.3", "--by", "总编", "--reason", "核过责任归属,确属模型责任"], root,
        )
        self.assertEqual(0, ok.returncode, ok.stderr)
        self.assertIn("已手工停用（全项目）", ok.stdout)
        refused = run_local_cli(["staff", "ban", "--tool", "glm-5.3", "--by", "UI总监", "--reason", "我要停"], root)
        self.assertNotEqual(0, refused.returncode)
        missing = run_local_cli(["staff", "ban", "--tool", "glm-5.3", "--by", "总编"], root)
        self.assertNotEqual(0, missing.returncode)
        self.assertIn("--reason", missing.stderr + missing.stdout)


class TicketDeliverableCreationGateTests(TicketTestCase):
    """建单/改单时就在操作人的检出里核交付项。"""

    def cli(self, arguments: list[str], root: Path):
        return run_local_cli(arguments, root)

    def new_command(self, deliverables: list[str], *extra: str) -> list[str]:
        command = [
            "new", "--type", "派单", "--slot", SLOT, "--title", "交付项前置闸",
            "--source", "AGENTS.md:工单制", "--consumer", "工单台", "--tier", "乙", "--by", SLOT,
            "--player-facing",
        ]
        for row in deliverables:
            command.extend(["--deliverable", row])
        return [*command, *extra]

    def test_r1_narrative_deliverable_is_blocked(self):
        root = self.root / "narrative-deliverable"
        refused = self.cli(self.new_command(["交付报告已经写完了"]), root)
        self.assertEqual(2, refused.returncode)
        self.assertIn("交付项要写成仓内相对路径或真实文件路径,叙述句永远交不了板", refused.stderr)
        self.assertIn("这一条:交付报告已经写完了", refused.stderr)

    def test_r2_absent_path_is_only_a_reminder_and_the_ticket_still_gets_built(self):
        """派单的交付项按定义就是还没产出的东西,建单时不许拦。

        旧行为是退 2 拦死,于是各位只能造空占位文件绕过;而占位件一存在,
        submit 那条真闸就永远核得过——闸被绕成了摆设,副作用比闸本身还坏。
        """
        root = self.root / "branch-only-deliverable"
        branch_only = root / "deploy" / "branch-only.sh"
        created = self.cli(self.new_command([str(branch_only)]), root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertTrue(created_ticket_id(created))
        self.assertIn("以下交付项现在还不存在", created.stderr)
        self.assertIn(str(branch_only.resolve()), created.stderr)
        # 只在分支上的部署件那半句要留着：就是这么栽的，提醒里必须还说得出。
        self.assertIn("如果它只会存在于某个分支上(比如只推到部署远端的部署件)", created.stderr)

    def test_r2_absent_chinese_path_with_narrative_marker_is_not_called_a_narrative(self):
        """ 缺陷B:带「的」的合法中文路径,在派单场景 100% 被误判成叙述句。

        旧写法里「文件存在就跳过叙述词启发式」的保护对派单永远不成立——
        派单交付项本来就还没产出。两条路径只差一个「的」字，报错话术却完全不同：
        「…还没做出来的表.csv」报叙述句(错方向)，「…尚未产出.csv」报路径不存在(对方向)。
        各位办公目录清一色中文，这条是人人会撞。
        """
        root = self.root / "absent-chinese-deliverable"
        with_marker = root / "资产清单" / "还没做出来的表.csv"
        without_marker = root / "资产清单" / "尚未产出.csv"

        created = self.cli(self.new_command([str(with_marker)]), root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertNotIn("叙述句永远交不了板", created.stderr)
        self.assertIn("以下交付项现在还不存在", created.stderr)

        # 只差一个「的」字的对照组，两条必须走同一档。
        sibling = self.cli(self.new_command([str(without_marker)]), root)
        self.assertEqual(0, sibling.returncode, sibling.stderr)
        self.assertNotIn("叙述句永远交不了板", sibling.stderr)

    def test_r1_narrative_with_extension_but_no_separator_is_still_blocked(self):
        """放宽只到「带分隔符的当路径」为止;没有分隔符的仍按叙述标记硬拦。"""
        root = self.root / "narrative-with-extension"
        refused = self.cli(self.new_command(["做出对照表,并核对.csv"]), root)
        self.assertEqual(2, refused.returncode)
        self.assertIn("交付项要写成仓内相对路径或真实文件路径,叙述句永远交不了板", refused.stderr)

    def test_r2_existing_deliverable_prints_no_reminder_at_all(self):
        root = self.root / "existing-deliverable-quiet"
        deliverable = root / "review" / "result.md"
        deliverable.parent.mkdir(parents=True)
        deliverable.write_text("# result\n", encoding="utf-8")
        created = self.cli(self.new_command([str(deliverable)]), root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertNotIn("以下交付项现在还不存在", created.stderr)

    def test_r1_same_command_keeps_working_before_and_after_the_file_appears(self):
        root = self.root / "real-deliverable"
        deliverable = root / "review" / "result.md"
        command = self.new_command([str(deliverable)])
        # 做出来之前：建得出来，但要有提醒。
        before = self.cli(command, root)
        self.assertEqual(0, before.returncode, before.stderr)
        self.assertIn("以下交付项现在还不存在", before.stderr)

        deliverable.parent.mkdir(parents=True)
        deliverable.write_text("# result\n", encoding="utf-8")
        created = self.cli(command, root)
        self.assertEqual(0, created.returncode, created.stderr)
        self.assertNotIn("以下交付项现在还不存在", created.stderr)

        replacement = root / "review" / "replacement.md"
        replacement.write_text("# replacement\n", encoding="utf-8")
        changed = self.cli([
            "set", created_ticket_id(created), "--deliverable", str(replacement), "--by", SLOT,
        ], root)
        self.assertEqual(0, changed.returncode, changed.stderr)

    def test_r1_real_path_with_narrative_marker_is_not_misclassified(self):
        root = self.root / "real-chinese-deliverable"
        deliverable = root / "真源" / "设计者的意见.md"
        deliverable.parent.mkdir(parents=True)
        deliverable.write_text("# 真文件\n", encoding="utf-8")

        created = self.cli(self.new_command([str(deliverable)]), root)
        self.assertEqual(0, created.returncode, created.stderr)

    def test_r1_unchecked_escape_passes_and_logs_every_skipped_row(self):
        root = self.root / "deliverable-unchecked"
        rows = ["叙述句交付项", str(root / "missing" / "ghost.md")]
        created = self.cli(self.new_command(rows, "--deliverable-unchecked"), root)
        self.assertEqual(0, created.returncode, created.stderr)
        log_rows = [json.loads(line) for line in (root / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        audit = next(row for row in log_rows if row["事件"] == "deliverable-unchecked")
        self.assertTrue(audit["时间"])
        self.assertEqual(SLOT, audit["发言人"])
        self.assertEqual("new", audit["命令"])
        self.assertEqual(rows, audit["跳过的交付项"])
        self.assertEqual(2, len(audit["核验绝对路径"]))


class WakeNotifyTests(TicketTestCase):
    """跨位动作要往对方对话线写一行,否则设计者队列的唤醒段不亮。

    设计者只看那一段;不亮 = 那扇窗永远不知道有事等它(总监位的窗口不会自己醒)。
    """

    OTHER = "平台·工单台"
    REVIEW = "复检·合并"

    def thread(self, slot):
        return self.service.store.read_jsonl(self.service.store.thread_path(slot))

    def test_new_dispatch_writes_one_unread_line_into_the_owner_thread(self):
        before = len(self.thread(SLOT))
        ticket = self.dispatch("建单要能唤醒")
        rows = self.thread(SLOT)
        self.assertEqual(before + 1, len(rows))
        self.assertIn(ticket["编号"], rows[-1]["文字"])
        self.assertIn("建单要能唤醒", rows[-1]["文字"])
        self.assertEqual([], rows[-1]["已读标记"])
        self.assertEqual(ticket["编号"], rows[-1]["引用工单号"])

    def test_every_ask_type_writes_a_line_into_the_owner_thread(self):
        for kind in ("疑问", "需求", "阻塞", "拍板"):
            with self.subTest(kind=kind):
                before = len(self.thread(SLOT))
                body = VALID_DECISION_BODY if kind == "拍板" else f"{kind}正文"
                ticket = self.service.create_question(kind, SLOT, f"{kind}要能唤醒", body, self.OTHER)
                rows = self.thread(SLOT)
                self.assertEqual(before + 1, len(rows))
                self.assertIn(ticket["编号"], rows[-1]["文字"])
                self.assertEqual([], rows[-1]["已读标记"])

    def test_submit_wakes_the_owner_slot_to_judge(self):
        """员工交板 = 球传给总监。2026-09-03 设计者当场撞到:3 张待判、0 个唤醒提示。"""
        ticket = self.dispatch("交板要能唤醒总监")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        before = len(self.thread(SLOT))
        self.service.submit(ticket["编号"], "登录后可见")
        rows = self.thread(SLOT)
        self.assertEqual(before + 1, len(rows))
        self.assertIn("交板", rows[-1]["文字"])
        self.assertIn("等你判卷", rows[-1]["文字"])
        self.assertEqual(self.worker, rows[-1]["发言人"])
        self.assertNotEqual(SLOT, rows[-1]["发言人"])

    def test_judge_pass_wakes_the_review_slot(self):
        ticket = self.to_judging()
        before = len(self.thread(self.REVIEW))
        self.service.judge(ticket["编号"], True, SLOT, verdict="玩家怎么打开它:登录后在主城能看到")
        rows = self.thread(self.REVIEW)
        self.assertEqual(before + 1, len(rows))
        self.assertIn("判过", rows[-1]["文字"])
        self.assertIn("等你复验", rows[-1]["文字"])

    def test_merge_wakes_whoever_deploys(self):
        ticket = self.to_judging()
        self.service.judge(ticket["编号"], True, SLOT, verdict="玩家怎么打开它:登录后在主城能看到")
        before_owner, before_review = len(self.thread(SLOT)), len(self.thread(self.REVIEW))
        self.merged_ticket(ticket["编号"], self.REVIEW)
        self.assertEqual(before_owner + 1, len(self.thread(SLOT)))
        self.assertEqual(before_review + 1, len(self.thread(self.REVIEW)))
        self.assertIn("等上服", self.thread(SLOT)[-1]["文字"])

    def test_block_wakes_the_conductor(self):
        ticket = self.dispatch("阻塞要能唤醒总编")
        before = len(self.thread("总编"))
        self.service.block(ticket["编号"], "等素材", SLOT)
        rows = self.thread("总编")
        self.assertEqual(before + 1, len(rows))
        self.assertIn("挂起", rows[-1]["文字"])

    def test_answer_writes_a_line_back_to_the_slot_that_asked(self):
        ticket = self.service.create_question("疑问", SLOT, "问一句", "正文", self.OTHER)
        before = len(self.thread(SLOT))
        self.service.answer(ticket["编号"], "答一句", SLOT)
        rows = self.thread(SLOT)
        self.assertEqual(before + 1, len(rows))
        self.assertIn("答了", rows[-1]["文字"])
        self.assertIn(ticket["编号"], rows[-1]["文字"])

    def test_transfer_still_writes_exactly_one_line_per_side(self):
        ticket = self.dispatch("转交回归")
        before_here, before_there = len(self.thread(SLOT)), len(self.thread(self.OTHER))
        self.service.transfer(ticket["编号"], self.OTHER, "转过去", SLOT)
        self.assertEqual(before_here + 1, len(self.thread(SLOT)))
        self.assertEqual(before_there + 1, len(self.thread(self.OTHER)))
        self.assertIn("转交", self.thread(SLOT)[-1]["文字"])

    def test_transfer_to_the_same_slot_is_not_written_twice(self):
        ticket = self.dispatch("转给自己")
        before = len(self.thread(SLOT))
        self.service.transfer(ticket["编号"], SLOT, "原地转交", SLOT)
        self.assertEqual(before + 1, len(self.thread(SLOT)))

    def test_the_speaker_is_the_actor_so_the_target_slot_counts_it_as_unread(self):
        """前端 slotUnreadForOwner 的过滤条件是「发言人 !== 该位」。

        通知的发言人若写成目标位自己,这一行永远不算它的未读,等于没写。
        """
        ticket = self.service.create_question("疑问", SLOT, "别位来的单", "正文", self.OTHER)
        row = self.thread(SLOT)[-1]
        self.assertEqual(self.OTHER, row["发言人"])
        self.assertNotEqual(SLOT, row["发言人"])
        self.assertEqual(ticket["所属总监位"], SLOT)
        # 自己给自己位建单:发言人本来就等于该位,那一行不计未读是对的,不需要唤醒自己。
        self.service.create_dispatch(
            SLOT, "自己给自己建", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            initiator=SLOT, task_tier="乙", deliverables=[str(self.deliverable)], internal=False,
        )
        self.assertEqual(SLOT, self.thread(SLOT)[-1]["发言人"])


class SubmitEvidenceTests(TicketTestCase):
    """玩家可感知派单的验证命令与原样输出以前被静默丢弃。

    有员工窗实撞过:传了两个参数、submit 成功、状态到待判,
    show 出来两字段却是空的——判卷人看不到执行方跑了什么。
    """

    def test_player_facing_ticket_keeps_verify_command_and_raw_output(self):
        ticket = self.dispatch("玩家可感知也要留住证据")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        ticket = self.service.submit(
            ticket["编号"], "登录后主城可见",
            verify_command="python -m pytest -q", raw_output="3 passed",
        )
        self.assertEqual("python -m pytest -q", ticket["接线证据"]["验证命令"])
        self.assertEqual("3 passed", ticket["接线证据"]["原样输出"])
        self.assertEqual("待判", ticket["状态"])

    def test_player_facing_ticket_without_a_world_image_submits_and_keeps_evidence(self):
        """附真登录图改为选填:缺图也能交板,两字段照存;一句话说明仍是必填。"""
        ticket = self.dispatch("缺图也能交")
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaisesRegex(TicketError, "一句话说明接线证据"):
            self.service.submit(ticket["编号"], "", verify_command="x", raw_output="y")
        submitted = self.service.submit(ticket["编号"], "登录后主城可见", verify_command="x", raw_output="y")
        self.assertEqual("待判", submitted["状态"])
        self.assertEqual("x", submitted["接线证据"]["验证命令"])
        self.assertEqual("y", submitted["接线证据"]["原样输出"])
        self.assertEqual([], submitted["接线证据"]["图片列表"])

    def test_player_facing_ticket_without_the_two_fields_still_passes(self):
        """不传两字段不该报错:那是内部工具单的闸,不是玩家可感知单的。"""
        ticket = self.dispatch("不传两字段也能交板")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        ticket = self.service.submit(ticket["编号"], "登录后主城可见")
        self.assertEqual("待判", ticket["状态"])
        self.assertEqual("", ticket["接线证据"]["验证命令"])

    def test_internal_ticket_still_requires_both_fields(self):
        ticket = self.service.create_dispatch(
            SLOT, "内部工具单闸不变", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError):
            self.service.submit(ticket["编号"], "验证完成", verify_command="only-one")


class WindowHintTests(TicketTestCase):
    """开窗工具建议写在派单标题开头的【claude】【codex】【vscode】【zcode】里。

    起因:图标位有一张单在 claude 窗认领之后才发现那个窗生不了图,整轮白跑。
    ★建议不是硬闸——填错才拒,留空、写不认识的标签一律照常建单;
    ★真源只有标题一处,单上的「建议窗口」是解析出来的派生值,读一次就按标题重算一次;
    ★开窗指令三行一个平台名都不许出现(把往操作提示那一行注入建议的做法撤了)。
    """

    # 开窗指令头一行贴的是本机 ticket.py 的真实路径,路径里可能恰好含平台名(目录名里带 claude 之类)。
    # 所以逐名 assertNotIn 之前必须先把这两截与建议窗口无关的固定文本换成占位符,否则用例会假红。
    CLI_LITERAL = service_module.CLI_PATH
    ENV_LITERAL = service_module._derived_env_hint()  # 留着:任务书路径等处同样可能含平台名

    def dispatch_with_taskbook(self, title: str, window: str = "", internal: bool = True):
        # 任务书文件名故意不跟标题走:标题里带平台名时,路径会原样进开窗指令第二行,
        # 把「三行不许出现平台名」的用例判成假红。
        self.serial = getattr(self, "serial", 0) + 1
        taskbook = self.root / f"tb-{self.serial}.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)],
            taskbook=str(taskbook), window=window, internal=internal,
        )

    def scrubbed_lines(self, ticket) -> list[str]:
        """把与建议窗口无关的固定文本换成占位符,剩下的才是这些行自己写的字。"""
        lines = self.service.dispatch_instructions(ticket)
        taskbook = str(ticket["任务书路径"])
        return [
            line.replace(self.CLI_LITERAL, "<CLI>").replace(self.ENV_LITERAL, "<ENV>").replace(taskbook, "<任务书>")
            for line in lines
        ]

    def test_r4_1_each_of_the_four_tags_is_parsed_off_the_title(self):
        """四个合法标签各建一张单,解析出来的建议窗口都对。"""
        for platform in model.WINDOW_PLATFORMS:
            with self.subTest(platform=platform):
                ticket = self.dispatch_with_taskbook(f"【{platform}】接入角色面板")
                self.assertEqual(f"【{platform}】接入角色面板", ticket["标题"])
                self.assertEqual(platform, ticket["建议窗口"])
                self.assertEqual(platform, self.service.store.load_ticket(ticket["编号"])["建议窗口"])
        # 「两处存同一件事必然会漂」的那道防线:盘上的「建议窗口」只是派生缓存,
        # 手工塞一个跟标题对不上的值进去,读一次就被标题重新算平——真源永远只有标题。
        ticket = self.dispatch_with_taskbook("【zcode】只认标题")
        poisoned = self.service.store.load_ticket(ticket["编号"])
        poisoned["建议窗口"] = "codex"
        self.service.store.save_ticket(poisoned, "set", SLOT, "手工塞一个漂掉的派生值")
        self.assertEqual("zcode", self.service.store.load_ticket(ticket["编号"])["建议窗口"])

    def test_r4_2_notepad_and_cursor_are_refused_and_the_message_lists_all_four(self):
        """--window 给不认识的值要拒,报错里列得出四个合法值;cursor 单独钉一遍。"""
        for bad in ("notepad", "cursor"):
            with self.subTest(bad=bad):
                with self.assertRaises(TicketError) as caught:
                    self.dispatch_with_taskbook(f"拒非法值-{bad}", bad)
                message = str(caught.exception)
                self.assertIn(bad, message)
                for platform in model.WINDOW_PLATFORMS:
                    self.assertIn(platform, message)
        # 把 cursor 换成了 vscode:取值表本身也钉住,免得哪天又被悄悄加回去。
        self.assertNotIn("cursor", model.WINDOW_PLATFORMS)
        self.assertIn("vscode", model.WINDOW_PLATFORMS)
        # 标题里手写【cursor】已经不是合法标签,但建议不是硬闸:解析不出来,也不许拦住建单。
        legacy = self.dispatch_with_taskbook("【cursor】老写法照样建得出来")
        self.assertEqual("", legacy["建议窗口"])
        self.assertEqual("新建", legacy["状态"])

    def test_r4_3_a_title_without_a_tag_creates_fine_with_an_empty_hint(self):
        """标题不带前缀的单,标签为空且照常建得出来。"""
        for title in ("没有前缀的普通标题", "【notepad】不认识的标签也不拦"):
            with self.subTest(title=title):
                ticket = self.dispatch_with_taskbook(title)
                self.assertEqual(title, ticket["标题"])
                self.assertEqual("", ticket["建议窗口"])
                self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["建议窗口"])
                self.assertEqual("新建", ticket["状态"])

    def test_r4_4_window_writes_the_prefix_into_the_title_and_replaces_it(self):
        """--window 是「替你把前缀写进标题」的快捷方式;已有前缀是替换不是叠加。"""
        ticket = self.dispatch_with_taskbook("接入角色面板", "codex")
        self.assertEqual("【codex】接入角色面板", ticket["标题"])
        self.assertEqual("codex", ticket["建议窗口"])
        updated, _ = self.service.edit(ticket["编号"], SLOT, window="zcode")
        self.assertEqual("【zcode】接入角色面板", updated["标题"])
        self.assertEqual(1, updated["标题"].count("【"))
        self.assertEqual("zcode", updated["建议窗口"])
        # 建单时标题里已经带了一个前缀,--window 又给一个:仍然只留一个,以 --window 为准。
        both = self.dispatch_with_taskbook("【claude】两处都给了", "vscode")
        self.assertEqual("【vscode】两处都给了", both["标题"])
        self.assertEqual(1, both["标题"].count("【"))
        # 给空串就是撤回建议:前缀连标签一起去掉,标题回到没有标签的样子。
        cleared, _ = self.service.edit(ticket["编号"], SLOT, window="")
        self.assertEqual("接入角色面板", cleared["标题"])
        self.assertEqual("", cleared["建议窗口"])

    def test_r4_5_the_three_lines_never_carry_a_platform_name(self):
        """开窗指令各行一个字都不含工具名——工具名只在标题开头这一处。"""
        for platform in model.WINDOW_PLATFORMS:
            with self.subTest(platform=platform):
                ticket = self.dispatch_with_taskbook(f"【{platform}】三行不许带平台名")
                scrubbed = self.scrubbed_lines(ticket)
                self.assertEqual(3, len(scrubbed))
                for line in scrubbed:
                    for name in model.WINDOW_PLATFORMS:
                        self.assertNotIn(name, line, line)
                    self.assertNotIn("建议窗口", line, line)
                # 对照组:同一张单把标签清成空再生成一遍,各行必须逐字相等。
                # 标题前缀和派生字段两头都清掉,注入不管从哪一头读都会在这里露馅;
                # 单号、员工名、任务档、任务书路径全都不动,差异只可能来自建议窗口。
                cleared = dict(ticket, 标题=model.strip_window_prefix(ticket["标题"]), 建议窗口="")
                self.assertEqual(
                    self.service.dispatch_instructions(cleared),
                    self.service.dispatch_instructions(ticket),
                )

    def test_r4_6_set_window_changes_the_tag_and_stays_shut_while_judging(self):
        """set --window 能改已建单的标签;待判态被现有可改态闸拦下。"""
        ticket = self.dispatch_with_taskbook("【claude】改标签")
        updated, changes = self.service.edit(ticket["编号"], SLOT, window="zcode")
        self.assertEqual("zcode", updated["建议窗口"])
        self.assertEqual("zcode", self.service.store.load_ticket(ticket["编号"])["建议窗口"])
        # 真源是标题,所以改动清单上写的也是标题——不再有第二个字段各记一笔。
        self.assertEqual(
            [{"字段": "标题", "旧值": "【claude】改标签", "新值": "【zcode】改标签"}], changes,
        )
        # 待判态照旧只放行 --assign:判卷人正看着这张单,别替别人改口径。
        pending = self.dispatch_with_taskbook("【claude】待判态不放开", internal=False)
        self.service.claim(pending["编号"], self.worker)
        self.service.attach(pending["编号"], str(self.picture()), "world", self.worker)
        self.assertEqual("待判", self.service.submit(pending["编号"], "登录后主城可见")["状态"])
        with self.assertRaises(TicketError) as blocked:
            self.service.edit(pending["编号"], SLOT, window="codex")
        self.assertIn("待判", str(blocked.exception))
        # 命令行那一头也要真的认这个开关:argparse 少写一行,服务端做对了也用不上。
        root = self.root / "cli-window"
        worker = TicketService(TicketStore(root)).staff_new(SLOT, "sol")["员工名"]
        created = run_local_cli([
            "new", "--type", "派单", "--slot", SLOT, "--title", "命令行建议窗口",
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根",
            "--deliverable", str(self.deliverable), "--tier", "乙", "--assign", worker,
            "--window", "codex", "--internal",
        ], root)
        self.assertEqual(0, created.returncode, created.stderr)
        ticket_id = created_ticket_id(created)
        changed = run_local_cli(["set", ticket_id, "--window", "vscode", "--by", SLOT], root)
        self.assertEqual(0, changed.returncode, changed.stderr)
        shown = json.loads(run_local_cli(["show", ticket_id], root).stdout)
        self.assertEqual("【vscode】命令行建议窗口", shown["标题"])
        self.assertEqual("vscode", shown["建议窗口"])
        refused = run_local_cli(["set", ticket_id, "--window", "cursor", "--by", SLOT], root)
        self.assertEqual(2, refused.returncode)
        for platform in model.WINDOW_PLATFORMS:
            self.assertIn(platform, refused.stdout + refused.stderr)

class DispatchFacingRequiredTests(TicketTestCase):
    """建派单必须二选一标可感知;可感知单交板附图选填;设计者可单独翻这一个开关。"""

    def new_dispatch_argv(self, title: str, *extra: str) -> list[str]:
        return [
            "new", "--type", "派单", "--slot", SLOT, "--title", title,
            "--source", "DECISIONS.md:测试", "--consumer", "主界面/面板根", "--tier", "乙",
            "--deliverable", str(self.deliverable), *extra,
        ]

    def test_r1_dispatch_without_facing_flag_is_rejected_explaining_both(self):
        """两个开关都不给 → 拒,报错里两个开关的含义都要出现。"""
        result = run_local_cli(self.new_dispatch_argv("可感知漏标"), self.root / "facing-missing")
        self.assertEqual(2, result.returncode)
        self.assertIn("内部工具单", result.stderr)
        self.assertIn("验证命令与原样输出", result.stderr)
        self.assertIn("玩家可感知单", result.stderr)
        self.assertIn("一张真登录图", result.stderr)
        self.assertIn("选填", result.stderr)
        self.assertIn("二选一", result.stderr)

    def test_r1_dispatch_with_both_facing_flags_is_rejected(self):
        """两个都给 → 拒(argparse 互斥组当场拦)。"""
        result = run_local_cli(
            self.new_dispatch_argv("可感知都给", "--internal", "--player-facing"), self.root / "facing-both",
        )
        self.assertEqual(2, result.returncode)

    def test_r1_single_facing_flag_creates_dispatch_with_matching_flag(self):
        """只给 --internal → 非玩家可感知=True;只给 --player-facing → False。"""
        root = self.root / "facing-single"
        created = run_local_cli(self.new_dispatch_argv("内部单", "--internal"), root)
        self.assertEqual(0, created.returncode, created.stderr)
        internal_id = created_ticket_id(created)
        self.assertTrue(json.loads(run_local_cli(["show", internal_id], root).stdout)["非玩家可感知"])
        created = run_local_cli(self.new_dispatch_argv("玩家单", "--player-facing"), root)
        self.assertEqual(0, created.returncode, created.stderr)
        player_id = created_ticket_id(created)
        self.assertFalse(json.loads(run_local_cli(["show", player_id], root).stdout)["非玩家可感知"])

    def test_r1_question_and_request_types_are_not_gated(self):
        """非派单类型(疑问/需求)不受这道闸影响,照常建。"""
        question = self.service.create_question("疑问", SLOT, "疑问不设闸", "请对方总监答复。", "总编")
        request = self.service.create_question("需求", SLOT, "需求不设闸", "请总编排期。", "总编")
        self.assertEqual(("疑问", "需求"), (question["类型"], request["类型"]))

    def test_r2_player_facing_submit_without_picture_goes_through_and_a_picture_is_kept(self):
        """附真登录图改为选填:可感知单缺图也能交板,不用改标内部单、不打标记;附了的图照挂成「真登录」。"""
        ticket = self.dispatch("缺图放行")
        self.service.claim(ticket["编号"], self.worker)
        submitted = self.service.submit(ticket["编号"], "登录后界面已出现")
        self.assertEqual("待判", submitted["状态"])
        self.assertFalse(submitted["非玩家可感知"])
        self.assertNotIn("欠真登录图", submitted)

        with_picture = self.dispatch("附图照挂")
        self.service.claim(with_picture["编号"], self.worker)
        self.service.attach(with_picture["编号"], str(self.picture("kept.png")), "world", self.worker)
        submitted = self.service.submit(with_picture["编号"], "登录后界面已出现")
        self.assertEqual("待判", submitted["状态"])
        self.assertEqual(["真登录"], [row["来源标注"] for row in submitted["接线证据"]["图片列表"]])

    def test_r3_designer_can_flip_facing_but_other_slot_director_cannot(self):
        """set --internal --by 设计者 → 过;set --internal --by 别位总监 → 拒。"""
        ticket = self.dispatch("设计者解卡")
        flipped = self.service.edit(ticket["编号"], "设计者", internal=True)[0]
        self.assertTrue(flipped["非玩家可感知"])
        with self.assertRaisesRegex(TicketError, "可感知标记"):
            self.service.edit(ticket["编号"], OTHER_SLOT, internal=False)

    def test_r3_designer_cannot_set_other_fields(self):
        """set --taskbook --by 设计者 → 仍然拒:只放开了可感知开关这一项。"""
        ticket = self.dispatch("设计者越权改任务书")
        with self.assertRaisesRegex(TicketError, "本人或总编能改"):
            self.service.edit(ticket["编号"], "设计者", taskbook=r"D:\office\任务书.md")
        self.assertEqual("", self.service.store.load_ticket(ticket["编号"])["任务书路径"])

    def test_r1_web_dispatch_without_internal_key_is_rejected_by_server(self):
        """网页端建派单:internal 键缺失 → 服务端拒(唯一真闸);前端勾选 true/false 都能过。"""
        handler = partial(TicketRequestHandler, directory=str(ROOT / "tools" / "browser"))
        server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = {"op": "new", "slot": SLOT, "title": "网页漏标", "source": ["DECISIONS.md:测试"],
                    "consumer": "主界面/面板根", "deliverables": [str(self.deliverable)],
                    "tier": "乙", "by": SLOT}
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)

            def post(payload: dict) -> tuple[int, dict]:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                connection.request("POST", "/api/action", body=body,
                                   headers={"Content-Type": "application/json; charset=utf-8"})
                response = connection.getresponse()
                raw = response.read()
                return response.status, json.loads(raw.decode("utf-8"))

            status, payload = post(base)
            self.assertEqual(400, status)
            self.assertIn("二选一", payload["reason"])
            status, payload = post({**base, "title": "网页内部单", "internal": True})
            self.assertEqual(200, status)
            self.assertTrue(payload["result"]["非玩家可感知"])
            status, payload = post({**base, "title": "网页玩家单", "internal": False})
            self.assertEqual(200, status)
            self.assertFalse(payload["result"]["非玩家可感知"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

class SearchButtonTriggerTests(unittest.TestCase):
    """搜索页改成按钮/回车触发(设计者 2026-09-06 当面报)。

    逐键 input 会对八百多张单全量 JSON.stringify + 整页重绘,打字巨卡。
    这里钉四件事:input 绑定一个字不留;按钮/回车两条触发路径都在;
    doSearch 末尾 bindCommon()+hydrateImages() 还在;
    小写全文索引只在数据刷新处算一次,搜索时只查缓存不重算。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")

    def test_1_search_box_has_no_input_binding(self):
        hits = [line for line in self.script.splitlines()
                if "searchBox" in line and "addEventListener('input'" in line]
        self.assertEqual([], hits, f"#searchBox 还绑着 input 事件,逐键全量搜索没摘干净:{hits}")

    def test_2_search_runs_only_from_button_and_enter(self):
        render = re.search(r"function renderSearch\(\)\{.*", self.script)
        self.assertIsNotNone(render, "没找到 renderSearch,钉测锚点漂了")
        self.assertIn("data-search-run", render.group(0), "搜索页没有「搜索」按钮")
        bind = [line for line in self.script.splitlines()
                if "data-search-run" in line and "doSearch" in line]
        self.assertTrue(bind, "搜索按钮没有绑到 doSearch")
        enter = [line for line in self.script.splitlines()
                 if "searchBox" in line and "keydown" in line and "'Enter'" in line]
        self.assertTrue(enter, "搜索框没有回车触发路径")
        self.assertTrue(any("doSearch" in line for line in enter), "回车路径没有落到 doSearch")

    def test_3_dosearch_still_binds_common_and_hydrates_images(self):
        body = re.search(r"function doSearch\(value\)\{.*", self.script)
        self.assertIsNotNone(body, "没找到 doSearch,钉测锚点漂了")
        text = body.group(0)
        for call in ("bindCommon()", "hydrateImages()"):
            self.assertIn(call, text, f"doSearch 里丢了 {call}——搜索页按钮会全部变死,以前就这么坏过")
        self.assertLess(text.index("bindCommon()"), text.index("hydrateImages()"),
                        "顺序必须 bindCommon() 在前、hydrateImages() 在后")
        self.assertGreater(text.index("bindCommon()"), text.index('$("#searchResults").innerHTML='),
                           "bindCommon 必须在塞完结果之后调")

    def test_4_search_index_built_once_at_refresh_not_per_search(self):
        refresh = re.search(r"async function refresh\(\)\s*\{(.*?)\n\}", self.script, re.S)
        self.assertIsNotNone(refresh, "没找到 refresh,钉测锚点漂了")
        self.assertIn("rebuildSearchIndex()", refresh.group(1), "数据刷新处没有重算搜索索引")
        body = re.search(r"function doSearch\(value\)\{.*", self.script)
        self.assertIsNotNone(body, "没找到 doSearch,钉测锚点漂了")
        text = body.group(0)
        self.assertIn("searchTexts.get", text, "doSearch 没有走缓存索引")
        self.assertNotIn("searchTexts.set", text, "doSearch 里在重算索引")
        self.assertNotIn("JSON.stringify", text, "doSearch 里还在逐单序列化(逐键全量搜索的老毛病)")
        rebuild = re.search(r"function rebuildSearchIndex\(\)\s*\{.*?\n\}", self.script, re.S)
        self.assertIsNotNone(rebuild, "没找到 rebuildSearchIndex,钉测锚点漂了")
        self.assertIn("JSON.stringify", rebuild.group(0), "rebuildSearchIndex 里没有算小写全文")
        self.assertIn("searchTexts.set", rebuild.group(0), "rebuildSearchIndex 没有写缓存")
class StaffSayOnOwnTicketTests(TicketTestCase):
    """员工在自己经手的单上留一句话,不该被对话线那道闸卡住。

    复检席员工实撞过:想留一行说明,被「对话线只有三方」拒,
    而 block 会改状态、submit 要等活干完——于是停在半路等人来问。设计者当天定的口径是
    不能因为其他客观原因阻塞员工,所以开了一条很窄的缝,这四条守住那条缝的边界。
    """

    def _own_ticket(self):
        ticket = self.service.create_dispatch(
            SLOT, "员工留言用单", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        return ticket

    def test_staff_can_say_on_the_ticket_assigned_to_them(self):
        ticket = self._own_ticket()
        row = self.service.say(SLOT, self.worker, "第 3 步卡住:缺权限", reference=ticket["编号"])
        self.assertEqual(self.worker, row["发言人"])
        self.assertTrue(row["文字"].startswith("【员工留言】"))
        self.assertEqual(ticket["编号"], row["引用工单号"])

    def test_staff_without_ref_is_still_refused(self):
        with self.assertRaises(TicketError):
            self.service.say(SLOT, self.worker, "没带 ref 就不许进")

    def test_staff_cannot_say_on_someone_elses_ticket(self):
        ticket = self.service.create_dispatch(
            SLOT, "别人的单", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        other = self.service.staff_new(SLOT, "sol")["员工名"]
        self.service.edit(ticket["编号"], SLOT, assign=other)
        with self.assertRaises(TicketError):
            self.service.say(SLOT, self.worker, "不是我的单", reference=ticket["编号"])

    def test_staff_cannot_say_on_another_slot_thread(self):
        ticket = self._own_ticket()
        with self.assertRaises(TicketError):
            self.service.say(OTHER_SLOT, self.worker, "别位的线仍然进不去", reference=ticket["编号"])


class RunningWindowsTests(TicketTestCase):
    """`running` 在跑窗口列表 + `say` 到终态单的提示(T-000026)。

    在跑是纯视图:状态停在「已认领」就算在跑,开工多久按「状态进入时间」,
    平台取名册「工具/窗类型」——所以用例钉的是「claim 之后出现、离开已认领之后消失」
    这条线,以及 say 的提示只挂在回执、对话线里写的就是原话。
    """

    def test_running_lists_claimed_ticket_with_roster_platform(self):
        ticket = self.dispatch()
        self.assertEqual([], self.service.running_windows(), "没认领过的单不算在跑")
        claimed = self.service.claim(ticket["编号"], self.worker)
        self.assertEqual("已认领", claimed["状态"])
        rows = self.service.running_windows()
        self.assertEqual([ticket["编号"]], [row["编号"] for row in rows])
        row = rows[0]
        self.assertEqual(SLOT, row["所属总监位"])
        self.assertEqual(self.worker, row["员工"])
        self.assertEqual("0分", row["开工多久"], "刚认领按「状态进入时间」算就是 0 分")
        self.assertEqual("sol", row["平台"], "平台取名册「工具/窗类型」那一格")
        # 设计者登记实际模型会写回名册同一格,列表跟着变——同一份名册,不另算。
        self.service.open_window(ticket["编号"], "设计者", "glm-5.3")
        self.assertEqual("glm-5.3", self.service.running_windows()[0]["平台"])
        # --slot 筛选照 list 的口径:别的位看不到这张单。
        self.assertEqual([], self.service.running_windows(OTHER_SLOT))

    def test_running_摘除_after_submit_and_void(self):
        first, second = self.dispatch("交板后摘除"), self.dispatch("作废后摘除")
        self.service.claim(first["编号"], self.worker)
        self.service.claim(second["编号"], self.worker)
        self.assertEqual(2, len(self.service.running_windows()))
        self.service.attach(first["编号"], str(self.picture()), "world", self.worker)
        self.service.submit(first["编号"], "登录后界面已出现")
        self.assertEqual(
            [second["编号"]],
            [row["编号"] for row in self.service.running_windows()],
            "交板落「待判」,离开「已认领」即摘除",
        )
        self.service.void(second["编号"], "建错了", SLOT)
        self.assertEqual([], self.service.running_windows(), "作废是终态,同样摘除")

    def test_running_摘除_after_transfer_and_back_after_reclaim(self):
        # 序列①(审计复现路径):派单在「已认领」被转交——transfer 不改派单状态,
        # 只把「指派给」写成位名;名册里没有叫这位的员工,单子不得再占原窗口的在跑行。
        claimed = self.dispatch("已认领态被转走")
        self.service.claim(claimed["编号"], self.worker)
        self.assertIn(claimed["编号"], [row["编号"] for row in self.service.running_windows()])
        moved = self.service.transfer(claimed["编号"], OTHER_SLOT, "转给后端接手", SLOT)
        self.assertEqual("已认领", moved["状态"], "transfer 不改派单状态——摘除只能靠名册口径")
        self.assertEqual(OTHER_SLOT, moved["指派给"], "转交后指派给是位名,不是在册员工")
        self.assertNotIn(
            claimed["编号"],
            [row["编号"] for row in self.service.running_windows()],
            "指派给不在名册的单摘除,即使状态还停在「已认领」",
        )
        # 序列②(回列):新建态转交 → 目标位员工认领,指派给回到名册员工名,单子回列。
        fresh = self.dispatch("新建态转交后由目标位认领")
        self.service.transfer(fresh["编号"], OTHER_SLOT, "归后端做", SLOT)
        self.assertEqual([], self.service.running_windows(), "转交后目标位未认领,谁都不在跑")
        worker2 = self.service.staff_new(OTHER_SLOT, "opus")["员工名"]
        self.service.claim(fresh["编号"], worker2)
        rows = [row for row in self.service.running_windows() if row["编号"] == fresh["编号"]]
        self.assertEqual(1, len(rows), "目标位员工认领后重新入列")
        self.assertEqual(worker2, rows[0]["员工"], "员工列即认领的新员工")
        self.assertEqual("opus", rows[0]["平台"], "平台取新员工名册「工具/窗类型」那一格")

    def test_say_提示_on_terminal_ticket_and_running_output_unchanged(self):
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        # 在跑(非终态)的 say:回执不带提示键,CLI 输出与改动前逐字相同。
        running_row = self.service.say(SLOT, "设计者", "在跑单上留言", reference=ticket["编号"])
        self.assertNotIn("终态提示", running_row)
        result = run_local_cli(
            ["say", "--slot", SLOT, "--by", "设计者", "--ref", ticket["编号"], "在跑单上留言"],
            self.service.store.root,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        written = self.service.store.read_jsonl(self.service.store.thread_path(SLOT))[-1]
        self.assertEqual(f"已写入 {SLOT} 对话线 · {written['时间']}", result.stdout.strip())
        # 终态单(作废):留言照常写入,回执与 CLI 输出都带同一句固定提示。
        self.service.void(ticket["编号"], "建错了", SLOT)
        expected = TicketService.SAY_TERMINAL_HINT.format(state="作废")
        row = self.service.say(SLOT, "设计者", "收口后补一句", reference=ticket["编号"])
        self.assertEqual(expected, row["终态提示"])
        stored = self.service.store.read_jsonl(self.service.store.thread_path(SLOT))[-1]
        self.assertEqual("收口后补一句", stored["文字"], "提示只挂在回执,对话线里写的就是原话")
        self.assertNotIn("终态提示", stored)
        terminal = run_local_cli(
            ["say", "--slot", SLOT, "--by", "设计者", "--ref", ticket["编号"], "收口后再补一句"],
            self.service.store.root,
        )
        self.assertEqual(0, terminal.returncode, terminal.stderr)
        self.assertIn(f"已写入 {SLOT} 对话线 · ", terminal.stdout)
        self.assertIn(expected, terminal.stdout)


class BrowserWakeListTests(unittest.TestCase):
    """把 tools/browser 那个**真执行**的用例接进套件。

    2026-09-08 本位把「要你去唤醒的窗口」从 10 格改塌成 2 格,而当时那一批用例**全绿**——
    因为它们只断言源码里有没有某一行,从没真的调用过 wakeList()。
    病根:blankData() 给每一位预填空数组 `[]`,而「有没有全文」用 Array.isArray 判断,
    空数组也是数组,于是除当前那一位外全被算成 0 条未读筛掉。
    ★只看形状的钉测抓不到这种事。这条用例真跑 readApi() 再看唤醒段,
      把 fix 撤掉它会报「实际 2、期望 13」——正是设计者当场看到的那个数。
    """

    def run_probe(self, name: str):
        script = ROOT / "tools" / "browser" / "tests" / name
        if not script.is_file():
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX},这条要读 {script}")
        node = shutil.which("node")
        if not node:
            self.skipTest("这台机器上没有 node;网页用例由上服闸 ③ 那台跑")
        result = subprocess.run(
            [node, str(script)], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("全过", result.stdout)

    def test_the_browser_wake_list_probe_passes(self):
        self.run_probe("wake-list.mjs")

    def test_the_browser_stage_counts_probe_passes(self):
        """件数条与设计者三段的数必须对得上。

        2026-09-08 设计者看着队列页问,为什么实际要开窗的是 5 个、上面却显示 3 个。
        两边都没算错,是两个维度:件数条按派单七步分流程,「建单」只数「新建」;
        而「要你传达的」还含「已认领/返工但他还没点已开窗」的单。
        ⇒ 加了「等你开窗/等你答」两格放最前 + 每格 title 写清口径。
        这条用例真跑 stageGroups(),钉死「第一格 == 下面那一段」。
        """
        self.run_probe("stage-counts.mjs")

    def test_the_browser_stale_banner_probe_passes(self):
        """红条的两个数永远不许是 undefined(实撞)。

        2026-09-09 设计者屏上待复检卡片红条是「已 undefined 小时,超过 undefined 小时线」,
        他据此误判复检卡了二十多单。这条探针真执行 waitingCard/staleInfo:
        数取不到(含 stale 是无键真值的极端形态)必须整条不渲染。
        """
        self.run_probe("stale-banner.mjs")

    def test_the_browser_staff_name_probe_passes(self):
        """三位员工号(-100 起)在网页上要看得见(漏,设计者实撞)。

        服务端正则放宽了,网页端 STAFF_NAME/activeStaff 还写死两位——三位员工的单
        被整张挡出「要你传达的」,设计者看不到、开不了窗。这条真跑那几条路。
        """
        self.run_probe("staff-name.mjs")

    def test_the_browser_submit_hygiene_probe_passes(self):
        """单卡「交板·仓库卫生」块要真渲染。

        命中逐件显示并按改法指路;零命中与老单不渲染、也不许炸。
        """
        self.run_probe("submit-hygiene.mjs")

    def test_the_browser_shot_blocked_badge_probe_passes(self):
        """单卡「欠真登录图」徽标要真渲染。

        取图受阻的单一眼睛看得出,悬停带受阻来由;没有标记的不许冒出这四个字。
        """
        self.run_probe("shot-blocked-badge.mjs")

    def test_every_browser_probe_is_wired_into_this_suite(self):
        """★新加的网页用例必须有人跑。

        tools/browser/tests 下的 .mjs 不会被 pytest 自动发现,全靠上面逐条手写接进来。
        漏接一个就是「写了用例但从来没跑过」——比没写更坏,因为它看着像有覆盖。
        这一条比对目录与上面接住的名单,漏了立刻红。
        """
        directory = ROOT / "tools" / "browser" / "tests"
        if not directory.is_dir():
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX},这条要读 {directory}")
        on_disk = {path.name for path in directory.glob("*.mjs")}
        wired = {
            name
            for method in dir(self)
            if method.startswith("test_")
            for name in re.findall(r'run_probe\("([^"]+)"\)',
                                   inspect.getsource(getattr(type(self), method)))
        }
        self.assertEqual(on_disk, wired, "有网页用例没被接进套件(或名单里写了不存在的文件)")


class ThreadSummaryTests(TicketTestCase):
    """对话线摘要。设计者 2026-09-08 报刷新还要 5 秒。

    量出来:那 5 秒里 **4.93 秒是对话线**(13 条线全文,压后约 1.01 MB),
    工单那半在增量之后只剩 0.29 秒 / 100 字节。
    页面真正要从对话线上算的只有几个数,全都能在服务端现算。
    ★摘要**故意不缓存**:已读标记是被回头改的(inbox --mark-read 改已有行),
      缓存它就会出现「标了已读、角标还亮着」的旧数据。现算就没这问题。
    """

    def test_1_the_summary_carries_exactly_what_the_page_needs(self):
        """未读条数(按人分)、最新一条未读的摘要、总行数——页面只用得到这些。"""
        self.service.say(SLOT, "设计者", "设计者说的第一句")
        self.service.say(SLOT, "总编", "总编说的第二句")
        summary = self.service.thread_summaries()[SLOT]
        self.assertEqual(2, summary["总行数"])
        # 设计者自己说的那句不算他未读;总编那句算
        self.assertEqual(1, summary["未读"]["设计者"])
        # 本位两句都没读过
        self.assertEqual(2, summary["未读"][SLOT])
        self.assertEqual("总编", summary["最新未读"]["发言人"])
        self.assertIn("第二句", summary["最新未读"]["摘要"])

    def test_2_marking_read_shows_up_immediately(self):
        """★现算的意义:标完已读,下一次拿摘要就该降下来——这正是缓存做不到的那一点。"""
        self.service.say(SLOT, "总编", "等你查收")
        self.assertEqual(1, self.service.thread_summaries()[SLOT]["未读"][SLOT])
        self.service.inbox(SLOT, SLOT, True)
        self.assertEqual(0, self.service.thread_summaries()[SLOT]["未读"][SLOT])
        # ★注意行数一个没变——所以任何「按行数切片」的缓存都发现不了这次改动
        self.assertEqual(1, self.service.thread_summaries()[SLOT]["总行数"])

    def test_3_every_slot_is_present_even_when_silent(self):
        """13 位都要有一行,空线也要有——页面按位取值,缺一位就是 undefined。"""
        summary = self.service.thread_summaries()
        self.assertEqual(set(model.SLOTS), set(summary))
        quiet = summary["美术·视觉三"]
        self.assertEqual(0, quiet["总行数"])
        self.assertIsNone(quiet["最新未读"])

    def test_4_the_summary_is_tiny_compared_with_the_full_threads(self):
        """摘要必须比全文小一个量级,否则这一单白做。"""
        for index in range(40):
            self.service.say(SLOT, "总编", f"第 {index} 句 " + "很长的一段话" * 40)
        summary_bytes = len(json.dumps(self.service.thread_summaries(), ensure_ascii=False).encode("utf-8"))
        full_bytes = sum(
            len(json.dumps(self.service.store.read_jsonl(self.service.store.thread_path(s)), ensure_ascii=False).encode("utf-8"))
            for s in model.SLOTS
        )
        self.assertLess(summary_bytes * 5, full_bytes, f"摘要 {summary_bytes} 相对全文 {full_bytes} 省得不够多")

    def test_5_the_page_reads_counts_from_the_summary_not_from_full_rows(self):
        """网页那三个数改走摘要;当前这一位仍按全文算(最准)。"""
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn('function unread(slot) { return unreadFor(slot, "设计者"); }', script)
        self.assertIn("function slotUnreadForOwner(slot){ return unreadFor(slot, slot); }", script)
        self.assertIn("function unreadFor(slot,actor)", script)
        # 只拉当前这一位的全文
        self.assertIn("data.threads[app.slot]=await loadThread(app.slot);", script)
        # 切位、看全文、搜索三条路都要先把那一位补上,否则会读到空数组当成「没有对话」
        self.assertIn("await ensureThread(app.slot);", script)
        self.assertIn("async function showWakeFull(slot){", script)
        self.assertIn("await ensureAllThreads();", script)


class IncrementalRefreshTests(TicketTestCase):
    """增量刷新。设计者 2026-09-08 提出关闭的工单没必要每次都重新请求广播。

    查实过:进过「关闭」的 254 张里,**0 张**再离开过关闭态。所以他的判断对。
    但实现**不按「是不是已关闭」缓存**——那条规矩是靠约定成立的,不是靠机制:
    工具里没有任何东西拦着一张关闭单被改(今天刚加的 rework 就是判过之后还能退回)。
    改用服务端流水行号当书签:关闭的单不变 ⇒ 永远不出现在增量里 ⇒ 自动不重传,
    **同样的省流,少赌一件事**;哪天真有人重开了一张关闭单,它会自己回来。
    """

    def internal(self, title: str):
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )

    def test_1_a_quiet_refresh_carries_no_tickets_at_all(self):
        """没动静时增量是空的——这就是省下来的那 2.45 MB。"""
        self.internal("先建一张")
        first = self.service.changes_since(0)
        self.assertEqual(1, len(first["工单"]))
        quiet = self.service.changes_since(first["游标"])
        self.assertEqual([], quiet["工单"])
        self.assertEqual(first["游标"], quiet["游标"])
        # 空增量必须小到可以忽略
        self.assertLess(len(json.dumps(quiet, ensure_ascii=False).encode("utf-8")), 200)

    def test_2_only_the_touched_ticket_comes_back(self):
        """动过哪张就只回哪张;没动的(含已关闭的)一张都不回。"""
        kept = self.internal("不会再动的那张")
        self.service.claim(kept["编号"], self.worker)
        self.service.submit(kept["编号"], "内部验证", "python -m pytest", "44 passed")
        cursor = self.service.changes_since(0)["游标"]
        moved = self.internal("待会儿要动的那张")
        delta = self.service.changes_since(cursor)
        self.assertEqual([moved["编号"]], [t["编号"] for t in delta["工单"]])
        self.assertNotIn(kept["编号"], [t["编号"] for t in delta["工单"]])

    def test_3_a_closed_ticket_that_does_change_still_comes_back(self):
        """★这正是不写死「关闭的不读」的理由:真变了,增量会把它带回来。

        按状态缓存的话,这一张会在页面上安静地停在旧状态——而且不报错。
        """
        ticket = self.internal("关闭之后又被动过")
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "内部验证", "python -m pytest", "44 passed")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        closed = self.service.close(ticket["编号"], SLOT, not_merged=True, reason="判过但不并线")
        self.assertEqual("关闭", closed["状态"])
        cursor = self.service.changes_since(0)["游标"]
        self.assertEqual([], self.service.changes_since(cursor)["工单"])  # 不动就不传
        # 有人回头动了它(这里用改备注模拟任何一种「关闭后仍被改」)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["备注"] = "关闭之后又补了一句"
        self.service.store.save_ticket(stored, "note", SLOT, "补记")
        back = self.service.changes_since(cursor)
        self.assertEqual([ticket["编号"]], [t["编号"] for t in back["工单"]])
        # 这一条要的是「关闭之后仍被改,增量照样把它传出来」——上面那张单确实回来了,已经验到了。
        # 不再断言备注的**内容**: 之后备注属于列表页不下发的键(网页零消费端),
        # 增量与整份都不带它。改在这里顺手钉一下,免得哪天有人把它偷偷加回下发集又没人发现。
        self.assertNotIn("备注", back["工单"][0], "备注是列表页不下发的键,增量也不该带")
        self.assertIn("备注", self.service.store.load_ticket(ticket["编号"]),
                      "不下发不等于不落库——盘上那份必须还在")

    def test_3b_a_delta_row_is_shaped_exactly_like_a_full_list_row(self):
        """★★增量与整份必须回**完全一样形状**的行,差一个键都不行。

        2026-09-08 实撞:增量少过了一道 ticket_view,于是「开窗指令」这个**派生字段**
        在增量来的单上没有——而**新建的单必然走增量**,结果设计者队列里那张卡的
        「开窗指令」整栏是空的,他一眼就看见了。
        单测某个字段没用,这里比**整个键集合**:以后 ticket_view 再加派生字段,
        只要增量那条路忘了跟上,这条立刻红。
        """
        # 开窗指令要真生成得出来,才谈得上比内容:派单 + 指派到合法员工 + 有任务书路径。
        taskbook = self.root / "形状用例任务书.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        ticket = self.service.create_dispatch(
            SLOT, "形状要一致", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
            taskbook=str(taskbook),
        )
        # ★整份那一趟是 /api/tickets → list_cards(之后不再是 list_tickets):
        #   要比的是**网页真正拿到的那两条路**,拿别的路来比等于没比。
        full = {row["编号"]: row for row in self.service.list_cards()}[ticket["编号"]]
        delta = {row["编号"]: row for row in self.service.changes_since(0)["工单"]}[ticket["编号"]]
        self.assertEqual(sorted(full), sorted(delta), "增量行与整份行的键集合不一致")
        # 派生字段的**内容**也要一样,不能只是键在
        self.assertEqual(full["开窗指令"], delta["开窗指令"])
        self.assertTrue(delta["开窗指令"], "派单必须有开窗指令,空的说明没过 ticket_view")
        # ★两道派生各有一个专属键,两个都要钉:
        #   「开窗指令」只有 ticket_view 会加,「未发送字段」只有 card_view 会加。
        #   增量少过任何一道,这里立刻红——这正是 2026-09-08 那次生产事故的形状。
        self.assertEqual(full["未发送字段"], delta["未发送字段"])
        # ★而全文那条路(CLI list / build_bundle 离线包 / 服务端搜索)必须**照旧带全**。
        #   两条路各司其职:少了这一条,哪天有人把精简做进 list_tickets,
        #   离线包和搜索会一起哑掉,而上面那些断言全是绿的。
        whole = {row["编号"]: row for row in self.service.list_tickets()}[ticket["编号"]]
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, whole, f"全文那条路不该摘掉 {key}")
            self.assertNotIn(key, full, f"列表那一趟不该下发 {key}")

    def test_4_a_bad_cursor_falls_back_to_everything(self):
        """书签越界(换过库、回滚过)一律整份重取,绝不能把客户端永远停在旧数据上。"""
        self.internal("甲"); self.internal("乙")
        total = self.service.changes_since(0)["总数"]
        for bad in (999999999, -5):
            with self.subTest(书签=bad):
                self.assertEqual(total, len(self.service.changes_since(bad)["工单"]))

    def test_5_the_reply_carries_the_total_so_deletions_cannot_hide(self):
        """★护栏:回总单数。单确实会消失过——线上 T-000001~008 就只剩流水、表里没有了。

        客户端拿本地条数与它一对,对不上就整份重取,鬼单活不过一次刷新。
        """
        self.internal("甲"); self.internal("乙"); self.internal("丙")
        delta = self.service.changes_since(0)
        self.assertEqual(3, delta["总数"])
        self.assertEqual(3, len(delta["工单"]))
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn("if(merged.length===delta.总数)", script)

    def test_6_the_maintenance_command_forces_a_full_reload(self):
        """★唯一绕过 save_ticket 的那条维护命令,必须让缓存整份重取,否则它对页面隐形。"""
        self.internal("维护前建的")
        cursor = self.service.changes_since(0)["游标"]
        self.assertFalse(self.service.changes_since(cursor)["整份重取"])
        self.service.store.backfill_state_times(force=True)
        after = self.service.changes_since(cursor)
        self.assertTrue(after["整份重取"], "维护命令改了盘却没让客户端重取,缓存会一直显示旧的进入时间")

    def test_7_threads_are_deliberately_not_cached(self):
        """★对话线故意不缓存:它看着只追加,其实 mark-read 会回头改已有行的「已读标记」,
        而那一格正是「要你去唤醒的窗口」的判据。按行数切片会让唤醒名单安静地变旧。"""
        slot = SLOT
        self.service.say(slot, "设计者", "第一句")
        rows_before = self.service.store.read_jsonl(self.service.store.thread_path(slot))
        self.service.inbox(slot, slot, True)          # 别位标已读:行数不变,内容变了
        rows_after = self.service.store.read_jsonl(self.service.store.thread_path(slot))
        self.assertEqual(len(rows_before), len(rows_after), "行数没变——所以按行数切片发现不了这次改动")
        self.assertNotEqual(rows_before, rows_after, "但内容确实变了")
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        # 之后实现变了(全文只拉当前一位、其余走服务端现算的摘要),
        # 但**要守的事实没变**:任何按行数切片的缓存都发现不了 mark-read 这种改动。
        self.assertIn("★摘要故意不进缓存", script)
        self.assertIn("data.threadSummary=await api(\"/api/thread-summary\")", script)

    def test_9_the_same_rules_hold_on_the_sqlite_backend(self):
        """★线上跑的是 SQLite,不是文件后端——增量的两条判据必须在**它**身上也成立。

        这条是变异检验逼出来的:第一版只测文件后端,把 SQLite 那边的
        「坏书签整份回退」改坏了,八条用例**一条都没红**。而线上正是 SQLite,
        一旦书签越界,客户端会永远收到空增量、永远停在旧数据上——最难查的那种。
        """
        store = SqliteStore(self.root / "增量-db" / "tickets.sqlite")
        service = TicketService(store)
        worker = service.staff_new(SLOT, "sol")["员工名"]
        first = service.create_dispatch(
            SLOT, "库里第一张", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        cursor = service.changes_since(0)["游标"]
        self.assertEqual([], service.changes_since(cursor)["工单"])          # 不动就不传
        service.claim(first["编号"], worker)
        self.assertEqual(                                                    # 动了就回来
            [first["编号"]], [t["编号"] for t in service.changes_since(cursor)["工单"]],
        )
        # ★坏书签必须整份回退,不许夹到边界后回空
        self.assertEqual(1, len(service.changes_since(999999999)["工单"]))
        self.assertEqual(1, len(service.changes_since(-5)["工单"]))
        self.assertEqual(1, service.changes_since(0)["总数"])
        # 维护命令那条信号在 SQLite 上同样要发得出来
        after = service.changes_since(service.changes_since(0)["游标"])
        self.assertFalse(after["整份重取"])
        store.backfill_state_times(force=True)
        self.assertTrue(service.changes_since(after["游标"])["整份重取"])

    def test_8_the_page_offers_a_way_out(self):
        """任何缓存都该有一条一键回到干净状态的退路。"""
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        page = (ROOT / "tools" / "browser" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="forceReload"', page)
        self.assertIn("function dropCache()", script)
        self.assertIn("forceButton.onclick", script)


class ReviewInParallelTests(TicketTestCase):
    """复检简化三条。设计者 2026-09-08 要求必须简化复检,没必要复检的地方直接通过。

    ① 判卷与复验**并行**:交板即可复验,不再等总监判过;并线 = 判过 ∧ 复验过。
    ② 内部单六项机器闸全绿即视为复验过,复检只看闸输出。
    ③ 上服与取证分离:上服记录由脚本自动建、免判;取证另开单,取不到图不挡上服。
    ④ 日览加「待复验」「可并」两队列。
    """

    def internal(self, title: str = "内部单"):
        self.keep_window_open()
        ticket = self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        return ticket

    def submitted(self, title: str = "内部单", gate_report: str = ""):
        ticket = self.internal(title)
        return self.service.submit(
            ticket["编号"], "验证完成", "python -m pytest", "444 passed", gate_report=gate_report,
        )

    # ── ① 判卷与复验并行 ──────────────────────────────────────────────
    def test_1_verify_works_before_the_judge_has_even_looked(self):
        """★并行的本体:交板之后、判卷之前就能复验。这一条红 = 复检席又被串在总监后面。"""
        ticket = self.submitted("先复验后判卷")
        self.assertEqual("待判", ticket["状态"])
        verified, hint = self.service.verify(ticket["编号"], "独立复检", "过", gates="四工程全绿")
        self.assertEqual("过", verified["复验"]["结论"])
        self.assertEqual("独立复检", verified["复验"]["复验人"])
        # 复验**不改状态**:状态归 judge 管,两件事各走各的
        self.assertEqual("待判", verified["状态"])
        self.assertIn("还没判卷", hint)
        # 判过之后两道齐,直接可并
        judged, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("待复检", judged["状态"])
        self.assertEqual("已合并", self.service.merge(ticket["编号"], "独立复检")["状态"])

    def test_2_merge_needs_both_gates_not_just_the_judge(self):
        """★并线前置 = 判过 ∧ 复验过。少了复验那一道要拒,且报错要说清怎么补。"""
        ticket = self.submitted("只判过没复验")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        with self.assertRaises(TicketError) as blocked:
            self.service.merge(ticket["编号"], "独立复检")
        message = str(blocked.exception)
        self.assertIn("还没复验", message)
        self.assertIn("verify", message)          # 报错要给出下一条命令,不能只说不行
        self.assertIn("--gate-report", message)   # 也要提内部单那条捷径
        self.service.verify(ticket["编号"], "独立复检", "过", gates="六项全绿")
        self.assertEqual("已合并", self.service.merge(ticket["编号"], "独立复检")["状态"])

    def test_2b_verified_alone_is_not_enough_either(self):
        """★另一半:复验过了、总监还没判,同样不能并。

        并线是「判过 ∧ 复验过」,两个合取项都要有闸守着。
        只钉「缺复验要拒」是钉了一半——把「∧ 判过」那一半拿掉,上一条照样绿。
        """
        ticket = self.submitted("只复验没判过")
        self.service.verify(ticket["编号"], "独立复检", "过", gates="六项全绿")
        self.assertEqual("待判", self.service.store.load_ticket(ticket["编号"])["状态"])
        with self.assertRaises(TicketError) as blocked:
            self.service.merge(ticket["编号"], "独立复检")
        self.assertIn("待复检", str(blocked.exception))
        # 机器闸那条路也一样:全绿只抵复验那一道,抵不了判卷
        green = self.submitted("机器闸绿但没判", gate_report=self.GREEN)
        self.assertEqual("过", green["复验"]["结论"])
        with self.assertRaisesRegex(TicketError, "待复检"):
            self.service.merge(green["编号"], "独立复检")

    def test_3_the_two_gates_may_arrive_in_either_order(self):
        """判过→复验 与 复验→判过,两条顺序都要能走到并线。"""
        first = self.submitted("先判后验")
        self.service.judge(first["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.service.verify(first["编号"], "独立复检", "过", gates="闸绿")
        self.assertEqual("已合并", self.service.merge(first["编号"], "独立复检")["状态"])
        second = self.submitted("先验后判")
        self.service.verify(second["编号"], "独立复检", "过", gates="闸绿")
        self.service.judge(second["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("已合并", self.service.merge(second["编号"], "独立复检")["状态"])

    def test_4_a_verify_rejection_leaves_the_state_alone_and_says_where_to_go(self):
        """复验判退**不自己改状态**:退回照旧走 rework/退回单,那两条路才记责任与返工次数。"""
        ticket = self.submitted("复验退回")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        rejected, hint = self.service.verify(ticket["编号"], "独立复检", "退", gates="体积闸不过")
        self.assertEqual("待复检", rejected["状态"])   # 状态没动
        self.assertEqual("退", rejected["复验"]["结论"])
        self.assertIn("rework", hint)
        # 退了就并不了
        with self.assertRaisesRegex(TicketError, "还没复验"):
            self.service.merge(ticket["编号"], "独立复检")

    def test_5_verify_refuses_the_worker_and_the_wrong_states(self):
        """执行员工不能复验自己的活;没交板/已并线的单也不收。"""
        ticket = self.internal("状态闸")
        with self.assertRaisesRegex(TicketError, "待判.*待复检|只有"):
            self.service.verify(ticket["编号"], "独立复检", "过", gates="x")
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "444 passed")
        with self.assertRaisesRegex(TicketError, "不能与执行员工"):
            self.service.verify(ticket["编号"], self.worker, "过", gates="x")
        with self.assertRaisesRegex(TicketError, "只可填"):
            self.service.verify(ticket["编号"], "独立复检", "也许", gates="x")
        with self.assertRaisesRegex(TicketError, "必须写清哪一条不过"):
            self.service.verify(ticket["编号"], "独立复检", "退")

    def test_6_a_self_owned_ticket_is_still_exempt(self):
        """★工单台自有单免复检(边界)。

        不留这个例外,平台位与复检席自己的单会**永久**卡在待复检:
        它们的复验人也只能是本位总监,「另一双眼睛」在这里数学上无解——
        与三方互斥闸那条例外是同一个道理(实撞过一次)。
        """
        self.keep_window_open()
        ticket = self.service.create_dispatch(
            SLOT, "本位自有单", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "444 passed")
        self.service.judge(ticket["编号"], True, SLOT, verdict=PASS_VERDICT)
        merged = self.service.merge(ticket["编号"], SLOT)   # 没跑过 verify,照样能并
        self.assertEqual("已合并", merged["状态"])
        self.assertIn("自记", merged["复检人"])

    # ── ② 内部单机器闸全绿即并 ────────────────────────────────────────
    GREEN = "\n".join(f"{item}: 过" for item in service_module.GATE_REPORT_ITEMS)

    def test_7_six_green_gates_count_as_verified(self):
        """六项全绿 ⇒ 自动记复验过,判过之后不必再跑 verify。"""
        ticket = self.submitted("六项全绿", gate_report=self.GREEN)
        self.assertTrue(ticket["机器闸"]["全绿"])
        self.assertEqual("过", ticket["复验"]["结论"])
        self.assertEqual("机器闸", ticket["复验"]["复验人"])
        self.assertIn("六项机器闸全绿", ticket["机器闸提示"])
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("已合并", self.service.merge(ticket["编号"], "独立复检")["状态"])

    def test_8_one_bad_or_missing_gate_is_not_green(self):
        """★任一不过、或缺一项,都不置标——这是这一条最要紧的地方。

        置错标的后果比没有闸更坏:台面上写着「机器闸绿」,复检席就不会再去看,
        而那张单其实没过闸。
        """
        one_red = self.GREEN.replace("体积: 过", "体积: 不过")
        red = self.submitted("一项不过", gate_report=one_red)
        self.assertFalse(red["机器闸"]["全绿"])
        self.assertEqual({}, red["复验"])
        self.assertIn("体积不过", red["机器闸提示"])
        # 缺项 ≠ 全绿:只写五行也不行
        short = "\n".join(f"{item}: 过" for item in service_module.GATE_REPORT_ITEMS[:5])
        missing = self.submitted("缺一项", gate_report=short)
        self.assertFalse(missing["机器闸"]["全绿"])
        self.assertEqual({}, missing["复验"])
        self.assertIn("缺", missing["机器闸提示"])
        # 没置标的单照旧要人复验才能并
        self.service.judge(red["编号"], True, "UI总监", verdict=PASS_VERDICT)
        with self.assertRaisesRegex(TicketError, "还没复验"):
            self.service.merge(red["编号"], "独立复检")

    def test_9_gate_report_is_internal_only(self):
        """玩家可感知单不许拿机器闸抵复验:真登录那一眼是明写不简化的。"""
        self.keep_window_open()
        ticket = self.dispatch("玩家可感知")
        self.service.claim(ticket["编号"], self.worker)
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        with self.assertRaisesRegex(TicketError, "只用于内部单"):
            self.service.submit(ticket["编号"], "看得见", gate_report=self.GREEN)

    def test_10_the_parser_only_trusts_lines_it_actually_understands(self):
        """读不懂的行不算过。宽松解析在这里等于把闸拆了。"""
        report = self.service.parse_gate_report(
            "合并树构建: 过\n四工程: 大概吧\n体积: 过\n号面: 过\ncore-ref 逐字: 过\n交付项: 过"
        )
        self.assertFalse(report["全绿"])
        self.assertEqual("读不懂", {row["项"]: row["结论"] for row in report["报告"]}["四工程"])


    # ── ④ 日览与队列 ─────────────────────────────────────────────────
    def test_14_the_digest_shows_both_new_queues(self):
        waiting = self.submitted("等复验的")
        ready = self.submitted("两道齐的")
        self.service.judge(ready["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.service.verify(ready["编号"], "独立复检", "过", gates="闸绿")
        digest = self.service.digest()
        self.assertTrue(any("待复验 " in line and "可并 " in line for line in digest), digest[:4])
        self.assertTrue(any(line.startswith(f"[可并] {ready['编号']}") for line in digest))
        pending_ids = [row["编号"] for row in self.service.pending_verify()]
        self.assertIn(waiting["编号"], pending_ids)
        self.assertNotIn(ready["编号"], pending_ids)
        self.assertEqual([ready["编号"]], [row["编号"] for row in self.service.ready_to_merge()])

    def test_15_a_ticket_waiting_too_long_for_verify_is_reported(self):
        """老化按「待复验超 24 小时」单独报:它与按状态计时那套口径不同。"""
        ticket = self.submitted("等太久")
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["状态进入时间"] = (datetime.now().astimezone() - timedelta(hours=30)).isoformat()
        # 直写盘:save_ticket 会按本次事件把「状态进入时间」重刷回现在,那样这条永远造不出超时。
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        digest = self.service.digest()
        self.assertTrue(any(line.startswith(f"[待复验超时] {ticket['编号']}") for line in digest), digest[:6])

    def test_16_the_judge_stops_telling_the_reviewer_to_verify_twice(self):
        """复验先做完了,判过时那句唤醒要改口——否则复检席会白开一次窗。"""
        ticket = self.submitted("先验过了")
        self.service.verify(ticket["编号"], "独立复检", "过", gates="闸绿")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        rows = self.service.store.read_jsonl(
            self.service.store.thread_path(service_module.REVIEW_SLOT))
        self.assertIn("可以并线了", rows[-1]["文字"])
        self.assertNotIn("等你复验", rows[-1]["文字"])


class DeployedMergedIsNotStalledTests(TicketTestCase):
    """已上服的「已合并」单不算卡住,改报「欠 live 记账」(总编 2026-09-08)。

    ★为什么要分开:「卡住了」那一段的意思是**有人该动手却没动**。
    一张代码早就在线上跑着、只差一笔 live 记账的单报成「卡 24 小时」,
    会让人去催一个根本不存在的活,而真正该做的只是补一笔账。
    """

    def deployed_merged(self, head: str = "abc1234de", record_head: str | None = None):
        # ★必须用**玩家可感知**单:内部单一并线就是终态(is_terminal),本来就不进老化告警,
        #   拿它测「不再报卡住」等于什么都没测——真正会卡在「已合并」上的正是这一类。
        ticket = self.to_merged("已上服的玩家可感知单", internal=False)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["判语"] = f"判过。判的是提交 {head}。设计者怎么打开它:照旧。"
        # 挪到 25 小时前:不这么做它根本越不过「已合并 24 小时」那条线,测不到本条
        stored["状态进入时间"] = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        if record_head is not None:
            self.service.state_set("deploy_head_server", record_head, service_module.REVIEW_SLOT)
        return self.service.store.load_ticket(stored["编号"])

    def test_1_a_merged_ticket_already_on_the_server_is_not_stalled(self):
        ticket = self.deployed_merged(record_head="abc1234de")
        self.assertTrue(self.service.awaiting_live_record(ticket))
        self.assertIsNone(self.service.stale_info(ticket), "已上服的单不该进「卡住了」")
        digest = self.service.digest()
        self.assertTrue(any(line.startswith(f"[欠 live 记账] {ticket['编号']}") for line in digest), digest[:8])
        self.assertFalse(any(line.startswith(f"[停滞] {ticket['编号']}") for line in digest))

    def test_2_a_merged_ticket_not_yet_deployed_is_still_stalled(self):
        """★反面:没上服的照旧按卡住报。放宽成「已合并一律不报」会把真卡住的单藏起来。"""
        ticket = self.deployed_merged(record_head="99999999")   # 值面里是别的头
        self.assertFalse(self.service.awaiting_live_record(ticket))
        self.assertIsNotNone(self.service.stale_info(ticket))
        # 值面一个头都没填时也照旧按卡住报(没有证据说明它上服了)
        blank = TicketService(TicketStore(self.root / "blank"))
        self.assertEqual(set(), blank.deployed_heads())

    def test_3_short_and_long_hashes_both_match(self):
        """值面写 9 位短号、判语写 40 位全号是常态,直接相等几乎永远不成立。"""
        long_hash = "abc1234de" + "0" * 31
        ticket = self.deployed_merged(head=long_hash, record_head="abc1234de")
        self.assertTrue(self.service.awaiting_live_record(ticket), "短号该匹配得上长号")


class CloseNotDeployedTests(TicketTestCase):
    """「已合并」单的未上服结案出口(总编报实撞)。

    ★这条边存在的原因:上服失败已回滚、内容随后来的单上服——这样的单并过线,
    却走不到实机复验过;close/免独图/live/rework 四条路全堵,单永远挂在「已合并」。
    它与 close --not-merged 是一对:那边「判过不并线」,这边「并过没上服」。
    """

    def merged_not_deployed(self):
        # 玩家可感知单:正是会卡在「已合并」上老化的那一类(内部单一并线就是终态)。
        ticket = self.to_merged("回滚未上服的玩家可感知单", internal=False)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["状态进入时间"] = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        # 值面里放一个与判语无关的头:证明它没上服,不欠 live 记账。
        self.service.state_set("deploy_head_server", "99999999", service_module.REVIEW_SLOT)
        return self.service.store.load_ticket(stored["编号"])

    def test_1_owner_closes_a_rolled_back_merged_ticket(self):
        ticket = self.merged_not_deployed()
        closed = self.service.close(
            ticket["编号"], SLOT, not_deployed=True, reason="部署单#15 失败已回滚,内容随 #16 上服",
        )
        self.assertEqual("关闭", closed["状态"])
        record = closed["未上服结案"]
        self.assertEqual(SLOT, record["结案人"])
        self.assertIn("部署单#15", record["原因"])
        self.assertIn("未上服结案:", closed["备注"])
        self.assertTrue(service_module.is_terminal(closed), "结案后不该再进老化告警")
        self.assertIsNone(self.service.stale_info(closed))
        from tools.tickets.ticket import compact_ticket
        self.assertIn("已合并·未上服·已结案", compact_ticket(closed))

    def test_2_only_merged_tickets_can_take_this_path(self):
        """没并过的单各有各的出路,不该混进这条路:待复检走 --not-merged,待判走 judge。"""
        ticket = self.to_judging()
        with self.assertRaisesRegex(TicketError, "只给并过线"):
            self.service.close(ticket["编号"], SLOT, not_deployed=True, reason="x")

    def test_3_reason_is_required(self):
        ticket = self.merged_not_deployed()
        with self.assertRaisesRegex(TicketError, "--reason 必填"):
            self.service.close(ticket["编号"], SLOT, not_deployed=True, reason="  ")
        # 拦下时状态不动,还停在「已合并」。
        self.assertEqual("已合并", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_4_actor_gate_owner_conductor_designer_yes_review_no(self):
        ticket = self.merged_not_deployed()
        with self.assertRaisesRegex(TicketError, "只有该单所属位"):
            self.service.close(
                ticket["编号"], service_module.REVIEW_SLOT, not_deployed=True, reason="复检席不该能结这个",
            )
        # 复检席被拦下后单子原样;换成总编就能结——「确认没上服」他说得清。
        self.assertEqual("已合并", self.service.store.load_ticket(ticket["编号"])["状态"])
        closed = self.service.close(ticket["编号"], "总编", not_deployed=True, reason="回滚未上服")
        self.assertEqual("关闭", closed["状态"])
        again = self.to_merged("设计者也可以", internal=False)
        designer_closed = self.service.close(again["编号"], "设计者", not_deployed=True, reason="设计者收口")
        self.assertEqual("关闭", designer_closed["状态"])

    def test_5_already_deployed_ticket_is_refused_and_sent_to_live(self):
        """提交号已在部署头里的单欠的只是记账,结成「未上服」等于把线上跑着的活说成没上。"""
        ticket = self.to_merged("已上服只欠记账", internal=False)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["判语"] = "判过。判的是提交 abc1234de。设计者怎么打开它:照旧。"
        self.service.store.atomic_json(self.service.store.item_path(stored["编号"]), stored)
        self.service.state_set("deploy_head_server", "abc1234de", service_module.REVIEW_SLOT)
        with self.assertRaisesRegex(TicketError, "补记账"):
            self.service.close(stored["编号"], SLOT, not_deployed=True, reason="不该结这张")

    def test_6_not_deployed_and_not_merged_are_mutually_exclusive(self):
        ticket = self.merged_not_deployed()
        with self.assertRaisesRegex(TicketError, "二选一"):
            self.service.close(
                ticket["编号"], SLOT, not_merged=True, not_deployed=True, reason="两个都给",
            )

    def test_7_digest_lists_it_as_not_deployed_and_not_stalled(self):
        ticket = self.merged_not_deployed()
        digest_before = self.service.digest()
        self.assertTrue(
            any(line.startswith(f"[停滞] {ticket['编号']}") for line in digest_before),
            "未结案前,未上服的已合并单就该按卡住报",
        )
        self.service.close(ticket["编号"], SLOT, not_deployed=True, reason="回滚未上服,原命题由 #16 落地")
        digest = self.service.digest()
        self.assertTrue(any(line.startswith(f"[未上服结案] {ticket['编号']}") for line in digest), digest[:10])
        self.assertIn("未上服结案 1 张", "\n".join(digest))
        self.assertIn("不算上服、不算卡住", "\n".join(digest))
        self.assertFalse(any(line.startswith(f"[停滞] {ticket['编号']}") for line in digest))


class ServerTimeEnvelopeTests(RemoteClientTests):
    """每个响应信封带服务器本地真时刻:核时区/_clock 从此一条命令。"""

    def test_1_client_captures_server_time_from_the_envelope(self):
        self.client.execute(["list"])
        self.assertRegex(
            self.client.server_time,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
        )

    def test_2_env_probe_prints_the_server_time(self):
        environment = clean_environment(
            self.service.store.root,
            TICKET_REMOTE=f"http://127.0.0.1:{self.server.server_address[1]}",
            TICKET_TOKEN_FILE=str(self.token_file),
        )
        probed = subprocess.run(
            [*CLI, "env", "--probe"], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(0, probed.returncode, probed.stderr)
        self.assertIn("服务器时刻", probed.stdout)
        self.assertRegex(probed.stdout, r"服务器时刻 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")


DISPATCH_SLOT = "分发·规则"


class DecisionRelaySlotTests(TicketTestCase):
    """第十三位「分发·规则」。

    它是设计者在后端/数值/物品/真源类事务上的**唯一对接人**:
    逐字记录裁定 → 送总编落 D9 号 → 按影响面用需求/疑问单广播。
    **不派实现单、不判卷、不 merge、不 live**——让传话的人给活打分是这条的反面。
    """

    def test_1_the_slot_is_in_the_roster_everywhere(self):
        """位名要真进 SLOTS,而不是只在某一处硬编码。"""
        self.assertIn(DISPATCH_SLOT, model.SLOTS)
        self.assertEqual(15, len(model.SLOTS), "十二个干活的位 + 第十三位 + 第十四位 + 总编 = 15 个位名")
        # ★禁派名单**逐字钉死**,不只是「遍历它自己」——
        #   test_3/test_4 都是 for slot in DISPATCH_FORBIDDEN_SLOTS,
        #   谁把某一位从名单里删掉,那两条就跟着少测一位、照样全绿(自指的闸等于没有闸)。
        self.assertEqual(("分发·规则", "分发·视觉"), model.DISPATCH_FORBIDDEN_SLOTS)
        # 两个对接位都必须真在 SLOTS 里:只进 DISPATCH_FORBIDDEN_SLOTS 而没进 SLOTS,
        # 结果是「建单说位名不合法、可闸又拦着它建派单」,两头堵死。
        for slot in model.DISPATCH_FORBIDDEN_SLOTS:
            self.assertIn(slot, model.SLOTS, f"{slot} 在禁派名单里却不在 SLOTS 里")
        # 建单/转交/ask 的位名校验都走同一个 SLOTS,所以进了它就三处都认
        asked = self.service.create_question("需求", DISPATCH_SLOT, "归它的需求", "正文")
        self.assertEqual(DISPATCH_SLOT, asked["所属总监位"])
        moved = self.service.transfer(asked["编号"], DISPATCH_SLOT, "归口对接", "总编")
        self.assertEqual(DISPATCH_SLOT, moved["所属总监位"])
        # 名册也要能给它开编号
        member = self.service.staff_new(DISPATCH_SLOT, "sol")
        self.assertTrue(member["员工名"].startswith(f"{DISPATCH_SLOT}-"))

    def test_2_it_may_send_the_three_question_types(self):
        """需求、疑问、拍板三类照发。"""
        for kind, body in (("需求", "要什么"), ("疑问", "问什么"), ("拍板", VALID_DECISION_BODY)):
            with self.subTest(kind=kind):
                ticket = self.service.create_question(
                    kind, SLOT, f"{kind}单", body, initiator=DISPATCH_SLOT)
                self.assertEqual(kind, ticket["类型"])

    def test_3_it_refuses_to_create_a_dispatch(self):
        """★变异点:去掉这道闸须红。

        它不派实现单——实现单由收到广播的那一位自己的总监派。
        报错要给出下一步(ask --type 需求),不能只说不行:拦下不是终点。
        """
        # ★逐位跑: 之后禁派名单有两位,只测第十三位会让新加的位裸奔。
        for slot in model.DISPATCH_FORBIDDEN_SLOTS:
            with self.subTest(slot=slot):
                with self.assertRaises(TicketError) as blocked:
                    self.service.create_dispatch(
                        slot, "它不该能派的活", ["DECISIONS.md:x"], "主界面",
                        task_tier="乙", deliverables=[str(self.deliverable)], internal=True)
                message = str(blocked.exception)
                self.assertIn("本位不派实现单", message)
                self.assertIn("ask --type 需求", message)
                self.assertIn(slot, message, "报错要点名是哪一位被拦")
                # ★拒绝语描述的那一半必须是**这一位**管的。实撞:
                #   原来后半句写死「后端/数值/物品/真源」,新位点名对了、描述照旧是第十三位的,
                #   对着一个只管画面与声音的位说它管数值,人照那句话反而更糊涂。
                self.assertIn(model.RELAY_SLOT_SCOPE[slot], message, f"{slot} 的拒绝语描述串位了")
                for other, scope in model.RELAY_SLOT_SCOPE.items():
                    if other != slot:
                        self.assertNotIn(scope, message, f"{slot} 的拒绝语里混进了 {other} 的描述")
        # ★拦在取号之前:不能白烧一个单号(那一课)
        self.assertEqual("T-000001", self.service.store.next_ticket_id())

    def test_4_it_neither_judges_nor_merges_nor_lives(self):
        """判卷 / 并线 / 实机复验三条都拒,位名与它的员工编号都要拦住。"""
        ticket = self.to_judging()
        # 位名与员工编号两种形态都要拦;★两个对接位都跑(之后不止一位)。
        actors = [form for slot in model.DISPATCH_FORBIDDEN_SLOTS for form in (slot, f"{slot}-01")]
        for actor in actors:
            with self.subTest(actor=actor), self.assertRaisesRegex(TicketError, "不判卷"):
                self.service.judge(ticket["编号"], True, actor, verdict=PASS_VERDICT)
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.verified(ticket["编号"])
        with self.assertRaisesRegex(TicketError, "不并线"):
            self.service.merge(ticket["编号"], DISPATCH_SLOT)
        merged = self.service.merge(ticket["编号"], "独立复检")
        self.assertEqual("已合并", merged["状态"])
        with self.assertRaisesRegex(TicketError, "不做实机复验"):
            self.service.live(ticket["编号"], str(self.picture("relay.png")), DISPATCH_SLOT, "独图")

    def test_5_the_charter_path_is_registered_as_the_window_source(self):
        """章程路径要能读出来,没登记时按办公目录约定推算,不留「未填」。"""
        self.assertEqual(
            f"_office/{DISPATCH_SLOT}/章程.md",
            self.service.slot_charter_path(DISPATCH_SLOT))
        charters = self.service.slot_charters()
        self.assertEqual(set(model.SLOTS), set(charters))
        self.assertTrue(all(charters.values()), "每一位都该有章程路径,不能有空的")
        # 登记了就以登记为准
        slots = self.service.store.read_json(self.service.store.slots_path, {})
        for row in slots.get("总监位", []):
            if row.get("名字") == DISPATCH_SLOT:
                row["章程"] = r"D:\另一处\章程.md"
        self.service.store.atomic_json(self.service.store.slots_path, slots)
        self.assertEqual(r"D:\另一处\章程.md", self.service.slot_charter_path(DISPATCH_SLOT))

    def test_6_the_digest_lists_it_separately(self):
        """日览把它的待答单列:积在这一位手上 = 设计者那一侧的问题没归并。"""
        self.service.create_question("需求", DISPATCH_SLOT, "等它归并的一条", "正文")
        digest = self.service.digest()
        self.assertTrue(any(line.startswith(f"{DISPATCH_SLOT} 待答 1 张") for line in digest), digest[:8])

    def test_6b_the_digest_counts_each_relay_slot_under_its_own_name(self):
        """★日览的对接位待答必须**逐位分开数**,不许两位合一个标题。

         加第十四位时实撞的坑:原来的写法是「`所属总监位 in DISPATCH_FORBIDDEN_SLOTS`
        一把抓 + 标题写死『分发·规则』」。名单里只有一位时看不出毛病,
        一加位就把别位的待答算到它头上,**而且不报错**——
        往这种元组里加成员,等于悄悄改了下游「按规则现算」的那一处,grep 字面根本找不到。
        """
        self.assertGreaterEqual(len(model.DISPATCH_FORBIDDEN_SLOTS), 2, "这条要两位以上才有意义")
        first, second = model.DISPATCH_FORBIDDEN_SLOTS[0], model.DISPATCH_FORBIDDEN_SLOTS[1]
        self.service.create_question("需求", first, "归第一位的", "正文")
        self.service.create_question("需求", first, "也归第一位的", "正文")
        self.service.create_question("需求", second, "归第二位的", "正文")
        text = "\n".join(self.service.digest(1))
        self.assertIn(f"{first} 待答 2 张", text, text)
        self.assertIn(f"{second} 待答 1 张", text, text)

    def test_7_the_page_hides_the_dispatch_form_for_it(self):
        """网页那一侧也不给建派单入口(服务端才是真闸,这里只是少让人白填一次)。"""
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        html = (ROOT / "tools" / "browser" / "index.html").read_text(encoding="utf-8")
        # 网页不另抄位表:两份名单都从服务端下发的位表配置里取——抄一份就会漂,
        # 少一位时离线/服务两种模式下那一位整个看不见,而且不报错。
        self.assertIn("const NO_DISPATCH_SLOTS = new Set(DESK_CONFIG.只分发不派单位 || []);", script)
        self.assertIn("const DEFAULT_SLOTS = Array.isArray(DESK_CONFIG.位名) ? DESK_CONFIG.位名.slice() : [];", script)
        self.assertIn("writable()&&!NO_DISPATCH_SLOTS.has(app.slot)?", script)
        # 位表配置必须在 tickets.js 之前同步载入,否则 tickets.js 顶层取到的是空名单。
        self.assertLess(html.index("desk-config.js"), html.index('<script src="tickets.js">'))
        # 服务端真下发的那一份:只发需求的位逐位都在,位名与服务端 SLOTS 逐位同序。
        handler = partial(TicketRequestHandler, directory=str(ROOT / "tools" / "browser"))
        server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("GET", "/desk-config.js")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(200, response.status)
        served = json.loads(body.removeprefix("window.TICKET_DESK_CONFIG = ").rstrip(";\n"))
        for slot in model.DISPATCH_FORBIDDEN_SLOTS:
            self.assertIn(slot, served["只分发不派单位"], f"下发的位表配置缺只发需求位 {slot}")
        self.assertEqual(list(model.SLOTS), served["位名"], "下发的位名单要与服务端 SLOTS 逐位一致、同序")


class ListPayloadSlimmingTests(TicketTestCase):
    """首次进页面那一趟只发网页真读的键。

    背景:稳态刷新已由降到 86 KB,只剩**首次**那一趟 2.62 MB(压后)。
    接管件里原写的下一步是「关闭+作废那 340 张发精简版」,按线上 1309 张真数据量过:
    那样只降 10.7%(2682→2395 KB),首次仍要 2.4 MB——终态单只占压后体积的 15%,
    且那批里仍有卡片必须显示的键(判语全库就 1.05 MB)。⇒ 改按**消费端**裁,不按状态裁。
    实测 2682 KB → 983 KB,降 63.3%。

    摘掉哪四个键是逐个 grep tools/browser/tickets.js 得出的,不是按体积挑的:
    答复/备注在渲染处一次都没出现;接线证据只被 worldImages() 读,而它全仓无调用处;
    正文只有 answerCard() 读。这几条边界就是下面几个用例要钉住的东西。
    """

    def serve(self):
        server = TicketHTTPServer(("127.0.0.1", 0), TicketRequestHandler, self.service, token="t0ken")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # 与 ResponseCompressionTests 同一套顺序:先登记 server_close、后登记 shutdown,
        # addCleanup 后进先出,才能保证先停 serve_forever 再关套接字(Windows 上否则 WinError 10038)。
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def get(self, address, path):
        conn = http.client.HTTPConnection(address[0], address[1], timeout=10)
        conn.request("GET", path, headers={"X-Ticket-Token": "t0ken"})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw.decode("utf-8"))["result"]

    def fatten(self, ticket_id: str, mark: str = "只有全文才找得到的那句"):
        """把一张单填成线上那种样子:四个重键都有真内容。"""
        stored = self.service.store.load_ticket(ticket_id)
        stored["正文"] = f"{mark}·正文 " + "很长的一段交代" * 60
        stored["答复"] = f"{mark}·答复 " + "很长的一段答复" * 60
        stored["接线证据"] = {"文字": f"{mark}·接线 " + "证据" * 60,
                              "验证命令": "python -m pytest", "原样输出": "44 passed", "图片列表": []}
        stored["备注"] = f"{mark}·备注 " + "边界说明" * 30
        self.service.store.save_ticket(stored, "note", SLOT, "填厚")
        return stored

    def test_1_the_list_drops_exactly_the_keys_the_page_never_shows(self):
        """整份那一趟不发这四个键,而且**如实报出**摘掉了哪几个。"""
        ticket = self.dispatch("列表精简")
        self.fatten(ticket["编号"])
        row = {r["编号"]: r for r in self.service.list_cards()}[ticket["编号"]]
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertNotIn(key, row, f"{key} 网页一个字都不显示,不该占首次那一趟的体积")
        # ★空串和「没发过来」必须分得清:不给这个标,人会对着空正文去改单。
        self.assertEqual(["正文", "答复", "接线证据", "备注"], row["未发送字段"])
        # 落库那份一个字都没少——精简只发生在下发这一段。
        stored = self.service.store.load_ticket(ticket["编号"])
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, stored)

    def test_2_the_body_survives_exactly_where_the_page_shows_it(self):
        """正文只在 answerCard() 会渲染的那几张上保留:待答 + 拍板/疑问/需求 + 指派给设计者。

        ★这条是「按消费端裁」的核心。少了它就会出现一种最难查的坏法:
        设计者队列「要你答的」那一段每张卡的正文都是空的,而页面不报任何错。
        """
        asked = self.service.create_question("疑问", SLOT, "要设计者答的", "这一段正文页面上要显示")
        self.assertEqual("待答", asked["状态"])
        self.assertEqual("设计者", asked["指派给"])
        plain = self.dispatch("不显示正文的普通派单")
        self.fatten(plain["编号"])
        rows = {r["编号"]: r for r in self.service.list_cards()}
        self.assertEqual("这一段正文页面上要显示", rows[asked["编号"]]["正文"])
        self.assertNotIn("正文", rows[asked["编号"]]["未发送字段"])
        self.assertNotIn("正文", rows[plain["编号"]])
        # 答完之后就不再显示了,正文也就不必再发
        self.service.answer(asked["编号"], "已排期→T-000001", "设计者")
        answered = {r["编号"]: r for r in self.service.list_cards()}[asked["编号"]]
        self.assertNotIn("正文", answered, "答完的单 answerCard 不再渲染它,正文不该继续下发")

    def test_3_search_still_finds_words_that_only_live_in_dropped_keys(self):
        """★搜索不许静默降级:被摘掉的键里的词,服务端照样要搜得到,并回全文。

        原来 doSearch 是 JSON.stringify(整张单) 的本地索引。列表不发正文之后,
        若搜索还在本地精简行上匹配,搜正文里的词会一条都搜不到,**而且页面不报错**,
        人只会以为「确实没有这张单」——这类安静的少给结果是最坏的一种失败。
        """
        ticket = self.dispatch("搜得到吗")
        self.fatten(ticket["编号"], mark="独角兽暗号")
        address = self.serve()
        status, hits = self.get(address, "/api/tickets?q=" + quote("独角兽暗号"))
        self.assertEqual(200, status)
        self.assertEqual([ticket["编号"]], [row["编号"] for row in hits])
        # 命中的那几张要回**全文**:点进去就能看,不必再多跑一趟
        self.assertIn("独角兽暗号", hits[0]["正文"])
        self.assertIn("独角兽暗号", hits[0]["答复"])
        # 而不带 q 的那一趟仍然是精简的
        status, listing = self.get(address, "/api/tickets")
        self.assertEqual(200, status)
        self.assertNotIn("正文", listing[0])

    def test_4_opening_one_ticket_still_returns_the_whole_thing(self):
        """要看被摘掉的内容,走 /api/ticket/<编号>——那条路照旧回全文。"""
        ticket = self.dispatch("点开看全文")
        self.fatten(ticket["编号"])
        address = self.serve()
        status, row = self.get(address, f"/api/ticket/{ticket['编号']}")
        self.assertEqual(200, status)
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, row, f"点开单张不该缺 {key}")
        self.assertIn("开窗指令", row)

    def test_5_the_cli_and_the_offline_bundle_still_get_everything(self):
        """★精简只做在下发那一段,不能做进 list_tickets。

        离线包(build_bundle)背后没有服务端可以按需取全文,包里少了正文就是**永久**少了,
        而离线模式下的搜索正是靠本地索引——那时候它是唯一的一条路。
        CLI 的 list 与非业务看板也走 list_tickets,一并保持全文。
        """
        ticket = self.dispatch("离线包要全文")
        self.fatten(ticket["编号"])
        whole = {r["编号"]: r for r in self.service.list_tickets()}[ticket["编号"]]
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, whole, f"全文那条路不该摘掉 {key}")
        self.assertNotIn("未发送字段", whole, "全文行没摘过东西,不该挂这个标")
        bundle = json.loads(
            (self.service.build_bundle(self.root / "bundle.js")).read_text(encoding="utf-8")
            .removeprefix("window.TICKET_DESK_BUNDLE = ").rstrip(";\n"))
        # 离线包自带位表配置(file:// 下没有服务端下发 desk-config.js)。
        self.assertEqual(list(model.SLOTS), bundle["位表配置"]["位名"])
        packed = {r["编号"]: r for r in bundle["items"]}[ticket["编号"]]
        for key in ("正文", "答复", "接线证据", "备注"):
            self.assertIn(key, packed, f"离线包不该摘掉 {key}——那边没有服务端可以补")

    def test_6_the_payload_really_shrinks(self):
        """行为判据:整份那一趟真的小下去了,不是只把键名改了改。

        钉的是**比值**不是绝对字节:绝对值会随单数涨,比值不会。
        """
        for index in range(12):
            self.keep_window_open()
            ticket = self.dispatch(f"填厚 {index}")
            self.fatten(ticket["编号"])
        whole = len(json.dumps(self.service.list_tickets(), ensure_ascii=False).encode("utf-8"))
        slim = len(json.dumps(self.service.list_cards(), ensure_ascii=False).encode("utf-8"))
        self.assertLess(slim * 2, whole, f"精简后 {slim} 相对全文 {whole} 省得不够多")

    def test_7_the_page_asks_the_server_to_search(self):
        """网页那一端确实改走服务端了,而且连不上时会**说明白**搜得不全。"""
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn("async function searchTickets(q){", script)
        self.assertIn("await api(`/api/tickets?q=${encodeURIComponent(q)}`)", script)
        self.assertIn("results=q?await searchTickets(q):[]", script)
        # 回落那条路必须出声:静默少给结果比报错更坏
        self.assertIn("正文、答复、接线证据里的词搜不到", script)


class ResponseCompressionTests(TicketTestCase):
    """响应压缩。设计者 2026-09-08 报「刷新工单页要多等十几秒」。

    量下来病根**不在服务端算得慢**:取数 + 序列化只花 1.1 秒;
    而一次刷新下行 11.17 MB(工单 7.48 MB + 13 条对话线 3.59 MB),
    远程线路约 480 KB/s——光传就二十多秒。这些全是中文 JSON,gzip 压到 32%。
    ★命令行那条路(remote.py 用 http.client)默认不发 Accept-Encoding,
      所以一个字节都不受影响——这一条必须钉死,不然哪天有人给 CLI 加了这个头
      却没加解压,整条命令行会突然读到一堆二进制。
    """

    def serve(self):
        service = self.service
        server = TicketHTTPServer(("127.0.0.1", 0), TicketRequestHandler, service, token="t0ken")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # addCleanup 是后进先出:先登记 server_close、后登记 shutdown,
        # 才能保证**先** shutdown 停掉 serve_forever、**再** close 套接字。
        # 反过来会让 select() 拿到一个已经关掉的套接字,Windows 上抛 WinError 10038。
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def fetch(self, address, path, accept_gzip: bool):
        headers = {"X-Ticket-Token": "t0ken"}
        if accept_gzip:
            headers["Accept-Encoding"] = "gzip"
        conn = http.client.HTTPConnection(address[0], address[1], timeout=10)
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response, raw

    def bulk(self, count: int = 40):
        for index in range(count):
            self.service.create_dispatch(
                SLOT, f"压缩用例填数据 {index} " + "长判语" * 60, ["DECISIONS.md:测试"],
                "主界面/面板根", self.worker, task_tier="乙",
                deliverables=[str(self.deliverable)], internal=False,
            )

    def test_1_a_gzip_client_gets_a_much_smaller_body(self):
        """声明能收 gzip 的客户端(浏览器)拿到压缩包,内容解开后与不压时逐字节相同。"""
        self.bulk()
        address = self.serve()
        plain_response, plain = self.fetch(address, "/api/tickets", accept_gzip=False)
        gzip_response, packed = self.fetch(address, "/api/tickets", accept_gzip=True)
        self.assertEqual("gzip", gzip_response.getheader("Content-Encoding"))
        self.assertEqual("Accept-Encoding", gzip_response.getheader("Vary"))
        self.assertIsNone(plain_response.getheader("Content-Encoding"))
        # 解开必须与不压的那份**一模一样**:压缩只许省带宽,不许改内容
        self.assertEqual(plain, gzip.decompress(packed))
        self.assertLess(len(packed), len(plain) // 2, f"压完 {len(packed)} 没有小于原来 {len(plain)} 的一半")
        # Content-Length 必须报压缩后的真实长度,否则客户端会读少或读挂
        self.assertEqual(len(packed), int(gzip_response.getheader("Content-Length")))

    def test_2_the_command_line_client_is_untouched(self):
        """★命令行那条路不发 Accept-Encoding,所以永远拿明文——这一条钉死。"""
        source = (ROOT / "tools" / "tickets" / "remote.py").read_text(encoding="utf-8")
        self.assertNotIn("Accept-Encoding", source)
        self.bulk(5)
        address = self.serve()
        response, raw = self.fetch(address, "/api/tickets", accept_gzip=False)
        self.assertIsNone(response.getheader("Content-Encoding"))
        json.loads(raw.decode("utf-8"))  # 明文能直接解析

    def test_3_small_replies_are_not_compressed(self):
        """几百字节的回执不压:压完可能更大,还白费一次 CPU。

        ★阈值看的是**压之前**的正文长度,不是压之后的——这里用一条错误回执
        (几十字节)来钉,别拿 /api/state 那种看着小、其实正文过 1KB 的当样本。
        """
        address = self.serve()
        response, raw = self.fetch(address, "/api/ticket/T-999999", accept_gzip=True)
        self.assertLess(len(raw), http_server_module.GZIP_MIN_BYTES)
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertIn("找不到工单", json.loads(raw.decode("utf-8"))["reason"])

    def test_4_the_page_builds_the_search_index_only_when_searching(self):
        """搜索索引改成用到才建:绝大多数刷新根本不搜,那一趟十几 MB 的序列化纯属白烧。"""
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn("function ensureSearchIndex()", script)
        self.assertIn("async function doSearch(value){ await ensureAllThreads(); ensureSearchIndex();", script)
        # refresh 那条路只作废索引,不再当场重建
        self.assertIn("function rebuildSearchIndex(){ searchTexts=new WeakMap(); searchIndexReady=false; }", script)


class NonBusinessGatesTests(TicketTestCase):
    """非业务闸不停车。

    设计者 2026-09-08 当面点的病:近两日停过的窗**几乎全是账面闸**——
    交付项写 .jpg 台面存 .webp、目录型交付项、
    工作树路径 vs 主检出路径、任务书待回核、远端镜像滞后、
    本机 CLI 不认新参数。**没有一件是活没做好**,而每一件都停掉了一整扇窗。
    ★这不是把闸拆了,是把闸分两种:拦「活没做好」的一个不动,拦「字没写对」的记一行继续走。
    """

    # ── ① 交付项归一化 ────────────────────────────────────────────────────
    def test_1_extension_differences_do_not_block_submit(self):
        """交付项写 .jpg、台面实际存 .webp:编号对上即同一份产物。

        落哪个扩展名是工单台自己的压缩管线按有没有透明通道决定的,员工建单时根本猜不到。
        """
        ticket = self.service.create_dispatch(
            SLOT, "图片交付项", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="乙", deliverables=[f"{'T-000001'}-01.jpg"], internal=False,
        )
        self.service.claim(ticket["编号"], self.worker)
        # 台面上真正落盘的是 RGBA→webp
        self.service.attach(
            ticket["编号"], str(self.picture("透明.png", mode="RGBA")), "world", self.worker,
        )
        stored = self.service.store.load_ticket(ticket["编号"])
        names = [row["文件名"] for row in stored["图片列表"]]
        self.assertTrue(any(name.endswith(".webp") for name in names), names)
        # 交付项写的是 .jpg,照样交得了板
        submitted = self.service.submit(ticket["编号"], "登录后界面已出现")
        self.assertEqual("待判", submitted["状态"])

    def test_2_directory_and_worktree_paths_are_folded_to_repo_relative(self):
        """目录型交付项、以及工作树路径 vs 主检出路径,都折回仓相对路径再核。"""
        from tools.tickets.model import resolve_under_root

        # 目录型:exists() 而不是 is_file(),目录算数
        (self.root / "产物目录").mkdir()
        self.assertIsNotNone(resolve_under_root("产物目录", self.root))
        self.assertIsNotNone(resolve_under_root("产物目录/", self.root))
        # 前缀不同、尾巴相同:两种绝对前缀都折得回来
        (self.root / "tools" / "x").mkdir(parents=True)
        (self.root / "tools" / "x" / "y.py").write_text("x\n", encoding="utf-8")
        for written in (
            "tools/x/y.py",
            "D:/project/_work/wt-abc/tools/x/y.py",
            r"D:\project\repo\tools\x\y.py",
        ):
            with self.subTest(写法=written):
                self.assertIsNotNone(resolve_under_root(written, self.root))
        # 真的不存在的仍然找不到——这条闸只放行写法,不放行「活没做」
        self.assertIsNone(resolve_under_root("tools/x/根本没有.py", self.root))

    # ── ② 员工可改自己单交付项的写法 ──────────────────────────────────────
    def test_3_worker_may_reshape_only_the_path_form_of_their_own_ticket(self):
        """员工能改自己单交付项的**写法**;换成另一份产物、或改别人的单,仍被原闸拦住。"""
        ticket = self.service.create_dispatch(
            SLOT, "路径写法", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="乙", deliverables=["tools/tickets/service.py"], internal=False,
        )
        self.service.claim(ticket["编号"], self.worker)
        # 同一份产物换个前缀:放行
        changed, _ = self.service.edit(
            ticket["编号"], self.worker,
            deliverables=["D:/project/_work/wt-abc/tools/tickets/service.py"],
        )
        self.assertIn("wt-abc", changed["交付项"][0])
        rows = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("事件") == "worker-reshape-deliverable"
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(self.worker, rows[0]["发言人"])
        # ★换成另一份产物:不是写法问题,落回原来的署名闸
        with self.assertRaises(TicketError) as swapped:
            self.service.edit(ticket["编号"], self.worker, deliverables=["别处/service.py"])
        self.assertIn("改", str(swapped.exception))
        # 别人的单也不行
        other = self.service.create_dispatch(
            SLOT, "别人的单", ["DECISIONS.md:测试"], "主界面/面板根", self.worker,
            task_tier="乙", deliverables=["tools/tickets/service.py"], internal=False,
        )
        with self.assertRaises(TicketError):
            self.service.edit(other["编号"], "前端·页面接线-99", deliverables=["tools/tickets/service.py"])

    # ── ④ 阻塞分业务/非业务 ───────────────────────────────────────────────
    def test_4_a_non_business_block_never_changes_the_state(self):
        """★非业务阻塞一个字都不改状态:员工窗接着做,不等任何人答复。"""
        ticket = self.dispatch("非业务阻塞")
        self.service.claim(ticket["编号"], self.worker)
        before = self.service.store.load_ticket(ticket["编号"])["状态"]
        result = self.service.block(
            ticket["编号"], "交付项后缀写错了", self.worker, service_module.BLOCK_NON_BUSINESS,
        )
        after = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual(before, after["状态"])
        self.assertEqual("已认领", after["状态"])
        self.assertEqual(1, len(after["非业务阻塞"]))
        self.assertIn("接着做", result["流程提示"])
        # 不进老化告警那一段:它根本不是阻塞态
        self.assertNotIn("阻塞", after["状态"])

    def test_5_business_blocks_still_stop_the_car(self):
        """业务阻塞照旧停车——「活没做好」那一类一个都没放松。"""
        ticket = self.dispatch("业务阻塞")
        self.service.claim(ticket["编号"], self.worker)
        blocked = self.service.block(ticket["编号"], "真源指针缺一条", "总编")
        self.assertEqual("阻塞", blocked["状态"])
        self.assertEqual(service_module.BLOCK_BUSINESS, blocked["阻塞类型"])
        with self.assertRaises(TicketError):
            self.service.block(ticket["编号"], "再挂一次", "总编")

    def test_6_the_board_and_the_digest_are_the_only_two_exits(self):
        """非业务不占队列,所以 list --nonbiz 与日览那两个数是它唯一的出口。"""
        ticket = self.dispatch("要清的非业务")
        self.service.claim(ticket["编号"], self.worker)
        self.service.block(
            ticket["编号"], "路径前缀不同", self.worker, service_module.BLOCK_NON_BUSINESS,
        )
        board = self.service.non_business_blocked(SLOT)
        self.assertEqual([ticket["编号"]], [row["编号"] for row in board])
        self.assertEqual(1, board[0]["条数"])
        digest = "\n".join(self.service.digest(24))
        self.assertIn("非业务 1", digest)
        self.assertIn("非业务不停车、不占队列", digest)
        # 命令行那张看板
        listed = run_local_cli(["list", "--nonbiz", "--slot", SLOT], self.service.store.root)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn(ticket["编号"], listed.stdout)
        self.assertIn("路径前缀不同", listed.stdout)

    def test_7_the_kind_must_be_one_of_the_two(self):
        """阻塞类型二选一,填别的当场拒并把两个合法值列出来。"""
        ticket = self.dispatch("类型写错")
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as raised:
            self.service.block(ticket["编号"], "原因", "总编", "随便写的")
        self.assertIn(service_module.BLOCK_BUSINESS, str(raised.exception))
        self.assertIn(service_module.BLOCK_NON_BUSINESS, str(raised.exception))


class ReworkFromReviewTests(TicketTestCase):
    """把「待复检」的单退回原位重做(复检席报)。

    缺的是一条**状态边**:judge 只吃「待判」,于是判过之后才发现要重做的单谁都翻不动——
    2026-09-08 设计者当面否了图标位一批画,图标位说「待复检态只有复检席能翻」,
    复检席手上也没有这个动作,最后只能 close --not-merged 一刀切成终态,单号作废、另开新单。
    这与 close --not-merged 是同一个缺口的两半:那边「到此为止」(终态),这边「还要接着做」(回返工)。
    """

    def to_review(self, title: str = "判过待复检"):
        ticket = self.to_judging()
        ticket, _ = self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertEqual("待复检", ticket["状态"])
        return ticket

    def test_1_review_slot_can_bounce_it_back_to_rework(self):
        """复检席把待复检的单打回「返工」:单号、任务书、断点全留住。"""
        ticket = self.to_review()
        before = ticket["任务书路径"] if ticket.get("任务书路径") else ""
        bounced = self.service.rework_from_review(
            ticket["编号"], "设计者当面否了这一批的画", "复检·合并",
        )
        self.assertEqual("返工", bounced["状态"])
        self.assertEqual(ticket["编号"], bounced["编号"])
        self.assertEqual(before, bounced.get("任务书路径", ""))
        self.assertEqual(1, bounced["返工次数"])
        self.assertEqual("设计者当面否了这一批的画", bounced["返工原因列表"][-1]["原因"])
        # 落库的也是同一份,不是只在返回值上好看
        stored = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("返工", stored["状态"])

    def test_2_the_opened_stamp_is_cleared_so_it_lands_back_in_the_queue(self):
        """与 judge --rework、unblock 同口径:戳记清掉,当场回「要你传达的」,不必等 8 小时线。"""
        # 真实时序:设计者在「已认领」那会儿点的已开窗,一路交板判过之后戳记还留着
        #(judge --pass 不清它,留作历史记录),所以到「待复检」时它仍在。
        ticket = self.dispatch("戳记要被清掉")
        self.service.claim(ticket["编号"], self.worker)
        self.service.open_window(ticket["编号"], "设计者", "opus")
        self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
        self.service.submit(ticket["编号"], "登录后界面已出现")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        self.assertIsNotNone(self.service.store.load_ticket(ticket["编号"])["已开窗"])
        self.service.rework_from_review(ticket["编号"], "口径写错了要重出", "复检·合并")
        self.assertIsNone(self.service.store.load_ticket(ticket["编号"])["已开窗"])

    def test_3_blame_defaults_to_the_question_not_the_model(self):
        """★默认记「出题」账,不是「模型」账。

        触发这条边的典型情形正是复检席报的那种:任务书口径被设计者否了、执行方照做没错。
        默认记模型账 = 凭空给执行方记一次判退,模型合格率就脏了。
        """
        ticket = self.to_review()
        bounced = self.service.rework_from_review(ticket["编号"], "口径被否", "复检·合并")
        self.assertEqual("出题", bounced["判退责任"])
        staff = self.service.store.load_staff()
        self.assertEqual({}, staff.get("模型记分") or {})
        self.assertEqual(1, int((staff.get("出题记分") or {})[SLOT]["合计"]))
        # 要记模型账必须显式写
        other = self.to_review("第二张")
        self.service.rework_from_review(other["编号"], "画得不对", "复检·合并", "模型")
        self.assertTrue(self.service.store.load_staff().get("模型记分"))

    def test_4_all_four_signers_pass_and_a_stranger_is_told_who_can(self):
        """四个署名位都放行——今天这一张正是「本位翻不动、复检席也翻不动」卡住的。"""
        for signer in (SLOT, "复检·合并", service_module.CONDUCTOR_SLOT, "设计者"):
            with self.subTest(署名=signer):
                ticket = self.to_review(f"退回-{signer}")
                self.assertEqual(
                    "返工",
                    self.service.rework_from_review(ticket["编号"], "口径被否", signer)["状态"],
                )
        blocked = self.to_review("别位来退")
        with self.assertRaises(TicketError) as raised:
            self.service.rework_from_review(blocked["编号"], "口径被否", "美术·视觉三")
        # 拒的时候必须说得出谁可以,只回「没有权限」会把人卡在原地
        self.assertIn(SLOT, str(raised.exception))
        self.assertIn("复检·合并", str(raised.exception))

    def test_5_reason_is_required_and_only_review_state_is_accepted(self):
        """--reason 必填;受理态只有「待复检」,待判的仍走 judge --rework。"""
        ticket = self.to_review()
        with self.assertRaises(TicketError) as blank:
            self.service.rework_from_review(ticket["编号"], "   ", "复检·合并")
        self.assertIn("--reason", str(blank.exception))
        judging = self.to_judging()
        with self.assertRaises(TicketError) as wrong_state:
            self.service.rework_from_review(judging["编号"], "口径被否", "复检·合并")
        self.assertIn("待复检", str(wrong_state.exception))
        self.assertIn("judge --rework", str(wrong_state.exception))

    def test_6_the_command_line_and_the_page_offer_the_same_door(self):
        """命令行端到端;网页那条 op 的 blame 默认值必须与 CLI 同一个,否则两处记出两本账。"""
        ticket = self.to_review()
        result = run_local_cli([
            "rework", ticket["编号"], "--reason", "设计者当面否了这一批", "--by", "复检·合并",
        ], self.service.store.root)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("返工", result.stdout)
        self.assertEqual("返工", self.service.store.load_ticket(ticket["编号"])["状态"])
        source = (ROOT / "tools" / "tickets" / "http_server.py").read_text(encoding="utf-8")
        self.assertIn('str(data.get("blame", "") or "出题")', source)

    def test_7_the_event_line_says_where_it_came_from(self):
        """事件线要记 rework-from-review,并把责任与「戳记已清」写进说明。"""
        ticket = self.to_review()
        self.service.rework_from_review(ticket["编号"], "口径被否", "复检·合并")
        rows = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("事件") == "rework-from-review"
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(ticket["编号"], rows[0]["工单号"])
        self.assertIn("出题", rows[0]["说明"])
        self.assertIn("已开窗标记已清", rows[0]["说明"])


class SelfOwnedMergeAndNotMergedCloseTests(TicketTestCase):
    """ (答复检席):两道闸原来拦住的是正当动作。

    ①本位自有单(复检席的部署单、平台位的工单台单)执行方是本位员工、判卷人与复检人都只能是本位总监,
      三方互斥在这里数学上无解,单子判过之后永远出不去(实撞)。已批。
    ②「判过了但正确的处置就是不并线」原来没有出路:close 要求「实机复验过」,于是烂在待复检里。
    """

    def to_judged(self, judge: str = "UI总监"):
        ticket = self.service.create_dispatch(
            SLOT, "不并线与自记并线", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        ticket, _ = self.service.judge(ticket["编号"], True, judge, verdict=PASS_VERDICT)
        return ticket

    def test_self_owned_merge_is_allowed_and_marked_as_self_recorded(self):
        """所属位 == 判卷人 == 复检人 时放行,但复检人写成「自记·待设计者终验」,不伪装第三方。"""
        ticket = self.to_judged(judge=SLOT)
        merged = self.service.merge(ticket["编号"], SLOT)
        self.assertEqual("已合并", merged["状态"])
        self.assertIn("自记", merged["复检人"])
        self.assertIn("待设计者终验", merged["复检人"])
        self.assertIn(SLOT, merged["复检人"])

    def test_cross_slot_three_way_gate_still_holds(self):
        """别位的单照旧:判卷人自己来复检仍然拒,报错里要说得出本位自有单那条例外。"""
        ticket = self.to_judged(judge="UI总监")
        # 复验先补上:别位的单不走 self_owned 例外,不补就先撞「还没复验」,
        # 测不到这里要钉的三方互斥闸。
        self.verified(ticket["编号"])
        with self.assertRaises(TicketError) as blocked:
            self.service.merge(ticket["编号"], "UI总监")
        self.assertIn("复检人必须与执行员工、判卷人都不同", str(blocked.exception))
        self.assertIn("本位自有单", str(blocked.exception))

    def test_not_merged_close_needs_a_reason(self):
        """不并线结案必须写原因——没有原因的不并线,事后与「忘了并」分不清。"""
        ticket = self.to_judged()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT, not_merged=True, reason="   ")
        self.assertIn("--reason 必填", str(blocked.exception))

    def test_not_merged_close_from_pending_review_records_the_reason(self):
        """待复检 → 关闭,原因落库并进备注,事件名与普通关闭区分开。"""
        ticket = self.to_judged()
        closed = self.service.close(
            ticket["编号"], SLOT, not_merged=True, reason="方案作废,设计者拍板",
        )
        self.assertEqual("关闭", closed["状态"])
        self.assertIn("方案作废", closed["备注"])
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("关闭", reloaded["状态"])
        self.assertIn("不并线结案", reloaded["备注"])

    def to_pending_judgement(self):
        """停在「待判」:交板了,但还没有人判。"""
        ticket = self.service.create_dispatch(
            SLOT, "待判不并线", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        return self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")

    def test_not_merged_close_from_pending_judgement_needs_a_verdict(self):
        """ (总编在实撞):待判就是还没判过。

        这条路原来对「待复检」与「待判」一视同仁,于是从待判直接关掉的单落成终态,
        判卷人与判语却都是空——事后没人说得出这活到底行不行、是谁看过的。
        """
        ticket = self.to_pending_judgement()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT, not_merged=True, reason="产物是结论不是代码")
        message = str(blocked.exception)
        self.assertIn("还没判过", message)
        self.assertIn("--verdict", message)
        self.assertIn("judge", message, "拦下要给出另一条路,不能只说不行")
        self.assertEqual(
            "待判", self.service.store.load_ticket(ticket["编号"])["状态"], "拦下就不许动状态",
        )

    def test_not_merged_close_from_pending_judgement_records_the_verdict(self):
        """带 --verdict 就一并补判:判卷人与判语都要真落库,不能只落状态。"""
        ticket = self.to_pending_judgement()
        closed = self.service.close(
            ticket["编号"], SLOT, not_merged=True, reason="产物是结论不是代码",
            verdict="判过。判的是提交 abc1234。产物是结论,不并线。",
        )
        self.assertEqual("关闭", closed["状态"])
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual(SLOT, reloaded["判卷人"])
        self.assertIn("abc1234", reloaded["判语"])
        self.assertIn("产物是结论不是代码", reloaded["备注"])

    def test_not_merged_close_refuses_a_verdict_when_the_ticket_was_already_judged(self):
        """待复检那一态判语已经在单上,再带 --verdict 就是覆盖判过的话,拦下。"""
        ticket = self.to_judged()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(
                ticket["编号"], SLOT, not_merged=True, reason="画风作废", verdict="另写一句",
            )
        self.assertIn("判语已经在单上", str(blocked.exception))
        self.assertEqual(PASS_VERDICT, self.service.store.load_ticket(ticket["编号"])["判语"])

    def test_cli_exposes_the_verdict_switch_for_a_pending_judgement_close(self):
        """argparse 少写一行,服务端做对了也用不上——这条和 --reason 那条一样要真跑一遍 CLI。"""
        root = self.root / "cli-not-merged-verdict"
        service = TicketService(TicketStore(root))
        worker = service.staff_new(SLOT, "sol")["员工名"]
        deliverable = root / "产物.md"
        deliverable.write_text("# 产物\n", encoding="utf-8")
        ticket = service.create_dispatch(
            SLOT, "命令行待判不并线", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(deliverable)], internal=True,
        )
        service.claim(ticket["编号"], worker)
        service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        refused = run_local_cli(
            ["close", ticket["编号"], "--by", SLOT, "--not-merged", "--reason", "产物是结论"], root,
        )
        self.assertEqual(2, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("--verdict", refused.stdout + refused.stderr)
        done = run_local_cli([
            "close", ticket["编号"], "--by", SLOT, "--not-merged",
            "--reason", "产物是结论", "--verdict", "判过。判的是提交 abc1234。不并线。",
        ], root)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        reloaded = service.store.load_ticket(ticket["编号"])
        self.assertEqual("关闭", reloaded["状态"])
        self.assertIn("abc1234", reloaded["判语"])

    def test_not_merged_close_is_refused_from_a_working_state(self):
        """只给判过之后的单:已认领态不许走这条路,免得拿它当作废用。"""
        ticket = self.service.create_dispatch(
            SLOT, "还在做", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT, not_merged=True, reason="不想做了")
        self.assertIn("待复检", str(blocked.exception))

    def test_not_merged_close_is_refused_for_an_unrelated_slot(self):
        """别位总监不能替人结案,报错里要说得出谁可以。"""
        ticket = self.to_judged()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], OTHER_SLOT, not_merged=True, reason="我看不顺眼")
        self.assertIn(SLOT, str(blocked.exception))
        self.assertIn("复检·合并", str(blocked.exception))

    def test_normal_close_path_is_untouched(self):
        """老路一个字没动:实机复验过才能普通关闭,不是实机复验过仍然拒。"""
        ticket = self.to_judged()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT)
        self.assertIn("实机复验过", str(blocked.exception))

    def test_cli_exposes_not_merged_and_reason(self):
        """命令行那一头也要真的认这条路:argparse 少写一行,服务端做对了也用不上。"""
        root = self.root / "cli-not-merged"
        service = TicketService(TicketStore(root))
        worker = service.staff_new(SLOT, "sol")["员工名"]
        deliverable = root / "产物.md"
        deliverable.write_text("# 产物\n", encoding="utf-8")
        ticket = service.create_dispatch(
            SLOT, "命令行不并线", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(deliverable)], internal=True,
        )
        service.claim(ticket["编号"], worker)
        service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        refused = run_local_cli(["close", ticket["编号"], "--by", SLOT, "--not-merged"], root)
        self.assertEqual(2, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("--reason 必填", refused.stdout + refused.stderr)
        done = run_local_cli(
            ["close", ticket["编号"], "--by", SLOT, "--not-merged", "--reason", "需求被后来的单取代"], root,
        )
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual("关闭", service.store.load_ticket(ticket["编号"])["状态"])


class CloseFromReworkTests(TicketTestCase):
    """ (后端·服务 0 号收口本批时实撞,平台位在沙箱逐条复现):

    「返工」原来一条通往终态的路都没有。平台位实跑的七条:
    close --not-merged(带不带 --verdict 都拦)/ close --not-deployed / 平路 close /
    merge / verify / judge —— 全按状态名拦下,唯一能动的是 claim 退回「已认领」,
    也就是**招人开窗重做已经上线的活**;void 又会把已交付已上服的单记成「作废」,比不关更坏。

    ★病根不是少一个状态,是闸问错了问题:它问「你停在哪一格」,该问「判过没有」。
      unblock之后「返工」一格装着两种来路完全不同的单——
      判退来的(判语在单上)与 unblock 来的(判卷人与判语全空,返工次数常为 0)。
    """

    def to_rework_via_unblock(self):
        """照的真实来路:block 再 unblock。★返工次数仍是 0 = 没被判退过。"""
        ticket = self.service.create_dispatch(
            SLOT, "阻塞解了落返工", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.block(ticket["编号"], "等前置", SLOT)
        self.service.unblock(ticket["编号"], SLOT)
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("返工", reloaded["状态"])
        self.assertEqual(0, reloaded.get("返工次数", 0), "unblock 来的返工不累计返工次数")
        self.assertEqual("", str(reloaded.get("判卷人", "")).strip(), "unblock 来的从没判过")
        self.assertEqual("", str(reloaded.get("判语", "")).strip())
        return reloaded

    def to_rework_via_judge(self):
        """另一种来路:真判退。判卷人与判语都在单上,返工次数 ≥ 1。"""
        ticket = self.service.create_dispatch(
            SLOT, "判退落返工", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        self.service.judge(
            ticket["编号"], False, SLOT, reason="没达标", verdict="模型责任。没达标。", blame="模型",
        )
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("返工", reloaded["状态"])
        self.assertEqual(1, reloaded["返工次数"])
        self.assertTrue(str(reloaded["判语"]).strip(), "判退也写判语")
        return reloaded

    def test_1_unblocked_rework_can_close_with_a_verdict(self):
        """unblock 来的返工 = 与「待判」同形:必须带 --verdict 一并补判,带了就放行。"""
        ticket = self.to_rework_via_unblock()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(
                ticket["编号"], SLOT, not_merged=True, reason="活已随 T-000042 上服,只欠记账",
            )
        message = str(blocked.exception)
        self.assertIn("还没判过", message)
        self.assertIn("--verdict", message)
        self.assertIn(
            "judge 只收「待判」", message,
            "★拒绝语给的出路必须是真能走的:返工态走不了 judge,不许把人指到死路",
        )
        self.assertEqual(
            "返工", self.service.store.load_ticket(ticket["编号"])["状态"], "拦下就不许动状态",
        )
        closed = self.service.close(
            ticket["编号"], SLOT, not_merged=True,
            reason="活已并进主干 43baf29dc、随某次部署上服,本单只欠记账",
            verdict="判过。判的是提交 c7b6bc7bc。活已随批上服,不再在本单名下并线。",
        )
        self.assertEqual("关闭", closed["状态"])
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual(SLOT, reloaded["判卷人"], "★不许落成「关闭 / 判卷人空 / 判语空」")
        self.assertIn("c7b6bc7bc", reloaded["判语"])
        self.assertIn("43baf29dc", reloaded["备注"])

    def test_2_the_prior_state_is_recorded(self):
        """从「返工」关掉与从「待复检」关掉,事后必须分得出来——不记这一格,落库后再也分不出。"""
        ticket = self.to_rework_via_unblock()
        self.service.close(
            ticket["编号"], SLOT, not_merged=True, reason="只欠记账", verdict="判过。判的是 abc1234。",
        )
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertIn("不并线结案", reloaded["备注"])
        self.assertIn("原「返工」", reloaded["备注"])
        events = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("工单号") == ticket["编号"] and "不并线结案" in str(row.get("说明", ""))
        ]
        self.assertEqual(1, len(events), events)
        self.assertIn("原「返工」", str(events[0].get("说明", "")))

    def test_3_judged_rework_closes_without_a_verdict_and_refuses_one(self):
        """判退来的返工:判语已经在单上,不必再补;再带 --verdict 就是覆盖判过的话,拦下。"""
        ticket = self.to_rework_via_judge()
        original = self.service.store.load_ticket(ticket["编号"])["判语"]
        with self.assertRaises(TicketError) as blocked:
            self.service.close(
                ticket["编号"], SLOT, not_merged=True, reason="不再做了", verdict="另写一句",
            )
        self.assertIn("判语已经在单上", str(blocked.exception))
        self.assertEqual(
            original, self.service.store.load_ticket(ticket["编号"])["判语"], "拦下不许改判语",
        )
        closed = self.service.close(ticket["编号"], SLOT, not_merged=True, reason="判退后决定不再做")
        self.assertEqual("关闭", closed["状态"])
        self.assertEqual(original, self.service.store.load_ticket(ticket["编号"])["判语"])

    def test_4_not_deployed_refusal_points_at_a_path_that_really_exists(self):
        """--not-deployed 对返工单原来指路 rework,而 rework 只收「待复检」= 又一条死路。"""
        ticket = self.to_rework_via_unblock()
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT, not_deployed=True, reason="x")
        message = str(blocked.exception)
        self.assertIn("只给并过线", message)
        self.assertIn("close --not-merged", message)
        self.assertNotIn(
            "走 rework", message,
            "★返工态走不了 rework(它只收待复检),拒绝语不许再把人指到那儿",
        )
        # 真跑一遍它指的那条路,证明它不是死路。
        self.service.close(
            ticket["编号"], SLOT, not_merged=True, reason="按拒绝语指的路走",
            verdict="判过。判的是 abc1234。",
        )
        self.assertEqual("关闭", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_5_the_accepted_states_are_pinned_verbatim(self):
        """★名单逐字钉死,不许按别的名单推导。

        test_1~4 都是 `for` 不到的具体态,但白名单本身若被人删一位,那几条只会少测一位、
        照样全绿(自指的闸会跟着名单一起缩水)。所以这里把三态原样写出来。
        """
        self.assertEqual(
            ("待复检", "待判", "返工"), service_module.CLOSE_NOT_MERGED_STATES,
            "改这个名单要连同 close() 那段注释与本用例一起改,别只改一处",
        )
        # 还在做的态一律不收:免得拿这条路当作废用。
        for state in ("新建", "已认领", "已合并", "实机复验过", "阻塞"):
            self.assertNotIn(state, service_module.CLOSE_NOT_MERGED_STATES, state)

    def test_6_a_working_state_is_still_refused(self):
        """老闸没松:还在做的单不许走这条路,报错里要列得出真正收哪几态。"""
        ticket = self.service.create_dispatch(
            SLOT, "还在做", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as blocked:
            self.service.close(ticket["编号"], SLOT, not_merged=True, reason="不想做了")
        message = str(blocked.exception)
        for state in service_module.CLOSE_NOT_MERGED_STATES:
            self.assertIn(state, message)
        self.assertEqual("已认领", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_7_cli_really_accepts_a_rework_close(self):
        """argparse 那一头也要真认:服务端做对了、CLI 少一行照样用不上。"""
        root = self.root / "cli-close-from-rework"
        service = TicketService(TicketStore(root))
        worker = service.staff_new(SLOT, "sol")["员工名"]
        deliverable = root / "产物.md"
        deliverable.write_text("# 产物\n", encoding="utf-8")
        ticket = service.create_dispatch(
            SLOT, "命令行返工结案", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(deliverable)], internal=True,
        )
        service.claim(ticket["编号"], worker)
        service.block(ticket["编号"], "等前置", SLOT)
        service.unblock(ticket["编号"], SLOT)
        self.assertEqual("返工", service.store.load_ticket(ticket["编号"])["状态"])
        refused = run_local_cli(
            ["close", ticket["编号"], "--by", SLOT, "--not-merged", "--reason", "只欠记账"], root,
        )
        self.assertEqual(2, refused.returncode, refused.stdout + refused.stderr)
        self.assertIn("--verdict", refused.stdout + refused.stderr)
        done = run_local_cli([
            "close", ticket["编号"], "--by", SLOT, "--not-merged",
            "--reason", "活已随部署单上服,只欠记账", "--verdict", "判过。判的是提交 abc1234。",
        ], root)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        reloaded = service.store.load_ticket(ticket["编号"])
        self.assertEqual("关闭", reloaded["状态"])
        self.assertIn("abc1234", reloaded["判语"])
        self.assertIn("原「返工」", reloaded["备注"])


class SettleChannelTests(TicketTestCase):
    """0 号收口通道。

    设计者 09-15 取消了两个模块的判卷与复检,由各自 0 号员工窗全权并线上服,
    而台面状态机仍假设判卷/复检存在 ⇒ Git 上已并进 main 的支,台面单还挂待判;
    0 号用真实员工署名跑 merge 被「仅待复检可合并」拦;总监只能 close --not-merged 收,
    前缀「不并线结案」与事实相反。

    ★为什么另开一条边而不是给 merge/judge 开例外:给 merge 开例外仍然要求单先走到「待复检」,
      那一步只有 judge 能给,而 judge 的产出(判卷人、判语)在免判卷模块里根本不存在——
      照那条路走必然要伪造一个判卷人。
    """

    def dispatch(self, title="收口探针", internal=True, exempt=True):
        ticket = self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=internal,
        )
        if exempt:
            self.service.set_exempt_judging(ticket["编号"], True, SLOT)
        return self.service.store.load_ticket(ticket["编号"])

    def test_1_an_unmarked_ticket_is_refused_and_told_how_to_mark_it(self):
        """没标「免判卷模块」一律拒——这条通道不是所有单的快捷方式。"""
        ticket = self.dispatch(exempt=False)
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as blocked:
            self.service.settle(ticket["编号"], self.worker, fact="已并", main_commit="abc1234")
        message = str(blocked.exception)
        self.assertIn("没有标「免判卷模块」", message)
        self.assertIn("--exempt-judging", message, "拒绝语要给出置它的那条命令")
        self.assertEqual("已认领", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_2_the_switch_is_writable_only_by_owner_conductor_designer(self):
        ticket = self.dispatch(exempt=False)
        with self.assertRaises(TicketError) as blocked:
            self.service.set_exempt_judging(ticket["编号"], True, OTHER_SLOT)
        self.assertIn(SLOT, str(blocked.exception))
        self.assertFalse(self.service.store.load_ticket(ticket["编号"])["免判卷模块"])
        for actor in (SLOT, "总编", "设计者"):
            self.service.set_exempt_judging(ticket["编号"], True, actor)
            self.assertTrue(self.service.store.load_ticket(ticket["编号"])["免判卷模块"])
            self.service.set_exempt_judging(ticket["编号"], False, actor)
            self.assertFalse(self.service.store.load_ticket(ticket["编号"])["免判卷模块"])

    def test_3_the_switch_is_settable_in_the_states_that_actually_need_it(self):
        """★这一格挂在 `edit` 上就等于永远置不上:edit 的可改态是 新建/已认领/返工,
        而需要它的单正好卡在 待判/待复检/阻塞。"""
        for state, prepare in (
            ("待判", lambda tid: (self.service.claim(tid, self.worker),
                                  self.service.submit(tid, "ok", "pytest", "passed"))),
            ("阻塞", lambda tid: (self.service.claim(tid, self.worker),
                                  self.service.block(tid, "等前置", SLOT))),
        ):
            ticket = self.dispatch(f"可置探针·{state}", exempt=False)
            prepare(ticket["编号"])
            self.assertEqual(state, self.service.store.load_ticket(ticket["编号"])["状态"])
            self.service.set_exempt_judging(ticket["编号"], True, SLOT)
            self.assertTrue(self.service.store.load_ticket(ticket["编号"])["免判卷模块"], state)

    def test_4_settle_takes_exactly_the_pinned_states(self):
        """★名单逐字钉死:下面几条都是具体态,白名单被删一位只会少测一位、照样全绿。"""
        self.assertEqual(
            ("已认领", "返工", "待判", "待复检", "阻塞"), service_module.SETTLE_STATES,
            "改这个名单要连 settle() 的注释与本用例一起改",
        )
        for state in ("新建", "已合并", "实机复验过", "关闭", "作废"):
            self.assertNotIn(state, service_module.SETTLE_STATES, state)

    def test_5_a_fresh_ticket_is_refused_because_nothing_has_been_merged_yet(self):
        ticket = self.dispatch()
        self.assertEqual("新建", ticket["状态"])
        with self.assertRaises(TicketError) as blocked:
            self.service.settle(ticket["编号"], SLOT, fact="已并", main_commit="abc1234")
        self.assertIn("新建", str(blocked.exception))
        self.assertEqual("新建", self.service.store.load_ticket(ticket["编号"])["状态"], "拦下不动状态")

    def test_6_an_internal_ticket_lands_on_merged_without_a_faked_judge(self):
        ticket = self.dispatch(internal=True)
        self.service.claim(ticket["编号"], self.worker)
        settled = self.service.settle(
            ticket["编号"], self.worker,
            fact="支 c7b6bc7bc 已并进主干 43baf29dc,随部署记录 T-000042 上服",
            main_commit="c47666352", engine_commit="43baf29dc", deploy_head="9bc2aa8f9",
        )
        self.assertEqual("已合并", settled["状态"])
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertEqual("", str(reloaded["判卷人"]).strip(), "★判卷人必须留空,填上就是伪造留痕")
        self.assertIn(service_module.SETTLE_PREFIX, reloaded["复检人"])
        self.assertIn(service_module.SETTLE_PENDING_DESIGNER, reloaded["复检人"])
        record = reloaded["收口"]
        self.assertEqual(self.worker, record["签署人"])
        self.assertEqual("c47666352", record["主仓提交"])
        self.assertEqual("43baf29dc", record["引擎提交"])
        self.assertEqual("9bc2aa8f9", record["部署头"])
        self.assertIn("T-000042", record["事实"])

    def test_7_a_player_facing_ticket_lands_on_live_verified_with_an_honest_exemption(self):
        """可感知单落「实机复验过」,而免图原因要写真话,不许伪装成「已经有图了」。"""
        ticket = self.dispatch(internal=False)
        self.service.claim(ticket["编号"], self.worker)
        settled = self.service.settle(
            ticket["编号"], SLOT, fact="0 号即本批并线与上服本体,判据在回执里",
            engine_commit="43baf29dc",
        )
        self.assertEqual("实机复验过", settled["状态"])
        reason = self.service.store.load_ticket(ticket["编号"])["免独图原因"]
        self.assertIn(service_module.SETTLE_PREFIX, reason)
        self.assertIn("不另索真登录图", reason)

    def test_8_the_prefix_never_says_the_untrue_words(self):
        """★前缀不许出现「不并线结案」「作废」——与事实相反。

        这类单的活是真并了、常常也真上服了,缺的只是台面上一笔记账。
        """
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        self.service.settle(ticket["编号"], self.worker, fact="已并已上服", main_commit="abc1234")
        reloaded = self.service.store.load_ticket(ticket["编号"])
        written = f"{reloaded['复检人']}\n{reloaded['备注']}"
        for lie in ("不并线结案", "作废"):
            self.assertNotIn(lie, written, f"前缀里不许出现「{lie}」")
        self.assertIn("0 号自并", written)
        self.assertIn("免判免复检", written)
        events = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("工单号") == ticket["编号"] and "0 号自并" in str(row.get("说明", ""))
        ]
        self.assertEqual(1, len(events), "事件线上也要是真前缀")

    def test_9_the_fact_line_and_one_commit_are_both_required(self):
        """免了判卷与复检,这一行事实就是这张单唯一的账;两仓提交号一个都给不出 = 谎报。"""
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        with self.assertRaises(TicketError) as no_fact:
            self.service.settle(ticket["编号"], self.worker, fact="  ", main_commit="abc1234")
        self.assertIn("--fact 必填", str(no_fact.exception))
        with self.assertRaises(TicketError) as no_commit:
            self.service.settle(ticket["编号"], self.worker, fact="已并")
        self.assertIn("至少要给一个", str(no_commit.exception))
        self.assertIn("谎报", str(no_commit.exception))
        self.assertEqual("已认领", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_10_only_the_module_side_can_sign(self):
        """署名按**位**收敛:「0 号」不是台面概念,它就是该模块总监位名下的一个员工编号。"""
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        other_worker = self.service.staff_new(OTHER_SLOT, "sol")["员工名"]
        with self.assertRaises(TicketError) as blocked:
            self.service.settle(ticket["编号"], other_worker, fact="已并", main_commit="abc1234")
        message = str(blocked.exception)
        self.assertIn(other_worker, message)
        self.assertIn(SLOT, message, "拒绝语要说得出谁可以")
        self.assertEqual("已认领", self.service.store.load_ticket(ticket["编号"])["状态"])
        # 同模块的另一个员工(典型就是 0 号)可以替执行员工署名。
        sibling = self.service.staff_new(SLOT, "sol")["员工名"]
        self.service.settle(ticket["编号"], sibling, fact="0 号代记", main_commit="abc1234")
        self.assertEqual("已合并", self.service.store.load_ticket(ticket["编号"])["状态"])

    def test_11_the_review_slot_is_not_notified(self):
        """ 第 3 条:免判卷单的 0 号动作不再推复检席——它对这两个模块没有活,
        再推只是给它攒过期未读。"""
        ticket = self.dispatch()
        self.service.claim(ticket["编号"], self.worker)
        self.service.settle(ticket["编号"], self.worker, fact="已并", main_commit="abc1234")
        def rows(slot):
            store = self.service.store
            return [
                row for row in store.read_jsonl(store.thread_path(slot))
                if ticket["编号"] in str(row)
            ]
        self.assertEqual([], rows(service_module.REVIEW_SLOT),
                         "复检线不该收到这张单的通知")
        # 所属位与总编照旧要收到——★这一半是反面判据:只断言「复检席没收到」的话,
        #   把整个通知调用删掉也照样绿。
        for slot in (SLOT, service_module.CONDUCTOR_SLOT):
            self.assertTrue(rows(slot), f"{slot} 应当收到通知")

    def test_12_the_cli_exposes_the_whole_channel(self):
        """argparse 少一行,服务端做对了也用不上;--exempt-judging 也不许和别的 set 参数混写。"""
        root = self.root / "cli-settle"
        service = TicketService(TicketStore(root))
        worker = service.staff_new(SLOT, "sol")["员工名"]
        deliverable = root / "产物.md"
        deliverable.write_text("# 产物\n", encoding="utf-8")
        ticket = service.create_dispatch(
            SLOT, "命令行收口", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(deliverable)], internal=True,
        )
        service.claim(ticket["编号"], worker)
        mixed = run_local_cli([
            "set", ticket["编号"], "--exempt-judging", "是", "--body", "顺手改正文", "--by", SLOT,
        ], root)
        self.assertEqual(2, mixed.returncode, mixed.stdout + mixed.stderr)
        self.assertIn("不能和", mixed.stdout + mixed.stderr)
        marked = run_local_cli(
            ["set", ticket["编号"], "--exempt-judging", "是", "--by", SLOT], root,
        )
        self.assertEqual(0, marked.returncode, marked.stdout + marked.stderr)
        self.assertTrue(service.store.load_ticket(ticket["编号"])["免判卷模块"])
        done = run_local_cli([
            "settle", ticket["编号"], "--by", worker, "--fact", "已并已上服",
            "--main-commit", "abc1234", "--deploy-head", "def5678",
        ], root)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("0 号收口记账", done.stdout)
        self.assertEqual("已合并", service.store.load_ticket(ticket["编号"])["状态"])

    def test_13_old_tickets_default_to_not_exempt(self):
        """老单没有这一格,读的时候补 false——★setdefault 不许换成覆盖,
        否则后端总监置过的单读一次就被抹回去。"""
        ticket = self.dispatch(exempt=False)
        raw = dict(self.service.store.load_ticket(ticket["编号"]))
        raw.pop("免判卷模块", None)
        raw.pop("收口", None)
        self.service.store.atomic_json(self.service.store.item_path(ticket["编号"]), raw)
        reloaded = self.service.store.load_ticket(ticket["编号"])
        self.assertFalse(reloaded["免判卷模块"])
        self.assertEqual({}, reloaded["收口"])
        self.service.set_exempt_judging(ticket["编号"], True, SLOT)
        self.assertTrue(
            self.service.store.load_ticket(ticket["编号"])["免判卷模块"],
            "置过之后再读一次必须还在",
        )


class FoldThreadNoticesTests(TicketTestCase):
    """ (设计者 2026-09-16 定):把一位对话线上过期的系统通知折叠,人话留言留着。

    ★先补的是一个更根本的洞:在这一笔之前,`_notify_slots` 落的**动作自动生成行**
      与 `say` 落的**人话留言**字段完全相同,机器分不出来——于是「折叠通知、只留人话」
      这件事只能靠人肉挑时刻。现在系统事件行带「系统事件: true」,两条路分得开。
    ★只标已读,不删行、不改时间线。
    """

    def thread(self, slot):
        return self.service.store.read_jsonl(self.service.store.thread_path(slot))

    def test_1_system_notices_are_marked_and_human_says_are_not(self):
        self.service._notify_slots((SLOT,), "总编", "动作生成的一行", "T-000001")
        self.service.say(SLOT, "总编", "这是人话留言,开窗第一眼要看见")
        result = self.service.fold_thread_notices(SLOT, "总编")
        self.assertEqual(1, len(result["已折叠"]), result)
        rows = self.thread(SLOT)
        self.assertEqual(2, len(rows), "★只标已读,一行都不许删")
        by_text = {str(row["文字"]): row for row in rows}
        self.assertIn(SLOT, by_text["动作生成的一行"]["已读标记"])
        self.assertNotIn(SLOT, by_text["这是人话留言,开窗第一眼要看见"]["已读标记"])

    def test_2_a_notify_row_is_tagged_and_a_say_row_is_not(self):
        """★这一格就是本笔补的那个洞;它没了,上一条用例就只能靠猜。"""
        self.service._notify_slots((SLOT,), "总编", "动作行", "T-000001")
        self.service.say(SLOT, "总编", "人话行")
        rows = {str(row["文字"]): row for row in self.thread(SLOT)}
        self.assertTrue(rows["动作行"].get("系统事件"))
        self.assertFalse(rows["人话行"].get("系统事件"))

    def test_3_legacy_rows_without_the_tag_are_kept_not_guessed(self):
        """老行没有那一格 ⇒ 默认**留着**。猜错就是把人话折叠掉,那正是这件事要防的。"""
        path = self.service.store.thread_path(SLOT)
        self.service.store.append_jsonl(path, {
            "时间": "2026-09-15T14:17:00+08:00", "发言人": "总编",
            "文字": "老行:某位 把 T-000172 判过但不并线结案", "图片列表": [],
            "引用工单号": "T-000172", "已读标记": [],
        })
        result = self.service.fold_thread_notices(SLOT, "总编")
        self.assertEqual([], result["已折叠"], "老行默认不折叠")
        self.assertEqual(1, len(result["仍未读"]))
        # 点名时刻才折叠;点名的那一条永远留着。
        folded = self.service.fold_thread_notices(
            SLOT, "总编", keep_unread=[], system_only=False,
        )
        self.assertEqual(1, len(folded["已折叠"]))
        self.assertIn(SLOT, self.thread(SLOT)[0]["已读标记"])

    def test_4_named_timestamps_stay_unread_even_with_all_unread(self):
        """★点名保留的那几条,连 --all-unread 也不许碰——那是开窗第一眼要看的人话。"""
        path = self.service.store.thread_path(SLOT)
        for stamp, text in (
            ("2026-09-15T19:48:24+08:00", "裁定 526 已落…"),
            ("2026-09-15T20:16:07+08:00", "T-000050 清卫批已收口…"),
            ("2026-09-16T07:13:33+08:00", "裁定 527…"),
            ("2026-09-15T14:17:00+08:00", "某位 把 T-000172 判过但不并线结案"),
        ):
            self.service.store.append_jsonl(path, {
                "时间": stamp, "发言人": "总编", "文字": text,
                "图片列表": [], "引用工单号": "", "已读标记": [],
            })
        result = self.service.fold_thread_notices(
            SLOT, "总编", system_only=False,
            keep_unread=["2026-09-15T19:48:24", "2026-09-15T20:16:07", "2026-09-16T07:13:33"],
        )
        self.assertEqual(1, len(result["已折叠"]), result)
        self.assertEqual(3, len(result["仍未读"]))
        unread = [row for row in self.thread(SLOT) if SLOT not in (row.get("已读标记") or [])]
        self.assertEqual(
            {"裁定 526 已落…", "T-000050 清卫批已收口…", "裁定 527…"},
            {str(row["文字"]) for row in unread},
        )

    def test_5_only_three_roles_may_touch_another_slots_unread_state(self):
        self.service._notify_slots((SLOT,), "总编", "动作行", "T-000001")
        with self.assertRaises(TicketError) as blocked:
            self.service.fold_thread_notices(SLOT, OTHER_SLOT)
        self.assertIn("只有", str(blocked.exception))
        self.assertNotIn(SLOT, self.thread(SLOT)[0].get("已读标记") or [])
        for actor in ("总编", "设计者", "平台·工单台"):
            self.service.fold_thread_notices(SLOT, actor)

    def test_6_the_action_is_written_to_the_audit_log(self):
        """改别位的未读状态必须留痕,不然事后说不清是谁折的。"""
        self.service._notify_slots((SLOT,), "总编", "动作行", "T-000001")
        self.service.fold_thread_notices(SLOT, "平台·工单台")
        rows = [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("动作") == "fold-thread-notices"
        ]
        self.assertEqual(1, len(rows), rows)
        self.assertEqual("平台·工单台", rows[0]["操作人"])
        self.assertIn(SLOT, rows[0]["说明"])

    def test_7_the_cli_exposes_it(self):
        root = self.root / "cli-fold"
        service = TicketService(TicketStore(root))
        service._notify_slots((SLOT,), "总编", "动作行", "T-000001")
        service.say(SLOT, "总编", "人话行")
        done = run_local_cli(["fold-notices", SLOT, "--by", "总编"], root)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("已折叠 1 行", done.stdout)
        self.assertIn("一行没删", done.stdout)
        rows = {
            str(row["文字"]): row
            for row in service.store.read_jsonl(service.store.thread_path(SLOT))
        }
        self.assertIn(SLOT, rows["动作行"]["已读标记"])
        self.assertNotIn(SLOT, rows["人话行"]["已读标记"])


class MergeRecordsWhenThereWasNoSecondEyeTests(TicketTestCase):
    """ 第 5 条:总编报的那处「三方互斥闸误伤」——本位实核后**归因不成立**。

    他报的是「merge 把复检席位名与复检席员工 -32 当同一人拦」。那道闸是**精确相等**,
    位名与「位名-NN」从不相等;本位在沙箱照复现(所属位=复检席、指派给=复检席-01、
    判卷人=总编),让复检席位名去 merge,那道闸一个字没响。

    真正拦下那一步的是的「判过 ∧ 复验过」:`self_owned` 要求
    判卷人 == 所属总监位,而判卷人是**总编**(代记)时它不成立 ⇒ 要求先复验。
    ⇒ 真病不是「拦住了」(补一道 verify 就过, 就是这么过的),而是**账面不诚实**:
      本位自己 verify、再自己 merge,落库 复验人 == 复检人 == 本位,看着像有第三只眼,其实没有。
    ⇒ 所以这里**不放宽任何闸**,只把那一笔如实标出来。
    """

    def to_judged_by_conductor(self):
        ticket = self.service.create_dispatch(
            SLOT, "本位自有单·总编代判", ["DECISIONS.md:测试"], "工单台", self.worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(ticket["编号"], "ok", "pytest", "passed")
        self.service.judge(ticket["编号"], True, "总编", verdict=PASS_VERDICT)
        return self.service.store.load_ticket(ticket["编号"])

    def test_1_the_three_way_gate_does_not_conflate_a_slot_with_its_staff_number(self):
        """★先钉住那条**不成立**的归因,免得下一窗又去改那道闸。"""
        ticket = self.to_judged_by_conductor()
        self.assertNotEqual(SLOT, ticket["指派给"], "员工编号与位名本来就是两个字符串")
        self.service.verify(ticket["编号"], SLOT, "过", gates="四套全绿")
        merged = self.service.merge(ticket["编号"], SLOT)
        self.assertEqual("已合并", merged["状态"], "位名去并从来没有被三方互斥闸拦住过")

    def test_2_what_really_blocks_is_the_verify_requirement(self):
        """判卷人是总编 ⇒ self_owned 不成立 ⇒ 撞的是「判过 ∧ 复验过」,不是三方互斥。"""
        ticket = self.to_judged_by_conductor()
        with self.assertRaises(TicketError) as blocked:
            self.service.merge(ticket["编号"], SLOT)
        message = str(blocked.exception)
        self.assertIn("还没复验", message)
        self.assertNotIn("复检人必须与执行员工", message, "拦下的不是三方互斥那道闸")

    def test_3_self_verified_merges_are_marked_as_having_no_second_eye(self):
        """自己 verify 再自己 merge = 没有第三只眼,落库必须这么写,不许看着像有。"""
        ticket = self.to_judged_by_conductor()
        self.service.verify(ticket["编号"], SLOT, "过", gates="四套全绿")
        merged = self.service.merge(ticket["编号"], SLOT)
        self.assertIn("自记", merged["复检人"])
        self.assertIn(service_module.SETTLE_PENDING_DESIGNER, merged["复检人"])

    def test_4_a_real_independent_reviewer_is_still_recorded_as_one(self):
        """★反面判据:复验人与复检人**真是两位**时,不许被误标成「自记」。

        注意这里必须让两个人真不同:复验人 = 总编、复检人 = 复检席。
        (本位第一版把两边都写成复检席,那本来就是同一个人,用例自己写错了。)
        """
        ticket = self.to_judged_by_conductor()
        self.service.verify(ticket["编号"], "总编", "过", gates="四套全绿")
        merged = self.service.merge(ticket["编号"], service_module.REVIEW_SLOT)
        self.assertEqual("已合并", merged["状态"])
        self.assertNotIn("自记", merged["复检人"])
        self.assertEqual(service_module.REVIEW_SLOT, merged["复检人"])


class DecisionGateReasonTests(TicketTestCase):
    """拍板单闸的拒绝语必须把机器真认的字面原样说出来。

    上一窗实撞两次:闸认的是带序号的「一、这是什么」,拒绝语只说「这是什么」——
    照拒绝语写必被拦,而人看不出差在哪。这是本台面第三次撞同一族毛病
    (前两例:拒绝语给了跑不起来的命令、需求单答复闸的三种口径与单里写法不一致)。
    """

    def test_1_the_reason_quotes_every_header_verbatim(self):
        for header in service_module.DECISION_HEADERS:
            self.assertIn(
                header, service_module.DECISION_GATE_REASON,
                f"拒绝语没把「{header}」原样列出来,照它写的人写不出闸认的字面",
            )

    def test_2_a_body_written_straight_from_the_reason_passes_the_gate(self):
        """★真判据:把拒绝语里列出来的标题抠出来,照它拼一份正文,必须能过闸。

        只断言「拒绝语含某字符串」挡不住措辞漂移;这一条真跑那道闸。
        """
        quoted = re.findall(r"「([^」]+)」", service_module.DECISION_GATE_REASON)
        headers = [text for text in quoted if text in service_module.DECISION_HEADERS]
        self.assertEqual(
            list(service_module.DECISION_HEADERS), headers,
            "拒绝语里的标题要与真源同序同数",
        )
        body = "\n".join(f"{header}:随便写点正文。" for header in headers)
        self.service._validate_decision_body(body)  # 不抛就算过

    def test_3_the_id_count_gate_says_its_own_reason(self):
        """编号超三个是另一个病因,原来与标题那条共用一句话 ⇒ 照着改标题永远改不好。"""
        body = "\n".join(
            f"{header}:见 DA-501 DA-502 DA-503 DA-504 的口径。"
            for header in service_module.DECISION_HEADERS
        )
        with self.assertRaises(TicketError) as blocked:
            self.service._validate_decision_body(body)
        message = str(blocked.exception)
        self.assertIn("专业编号", message)
        self.assertNotEqual(
            service_module.DECISION_GATE_REASON, message,
            "两个病因不许共用一句拒绝语",
        )
        self.assertNotIn(
            service_module.DECISION_HEADERS[0], message,
            "标题没问题的时候别让人去动标题",
        )


class CurrentStateBoardTests(TicketTestCase):
    """当前值落工单台一处机器可读,各位读它不要转抄。

    转抄之所以会错,是因为这几个数每天都在变而抄件不会自己更新—— 就是照着
    抄错的判据图尺寸做的,整张单作废。所以写口子只留给复检席与总编,别位一律拒。
    """

    WRITER = "复检·合并"
    TOOLS = "取图链=已改未并,在 feat/shot 上；ticket.py=T-000089 待复检"

    def tickets_root(self) -> Path:
        return self.root / "tickets"

    def state_get(self) -> subprocess.CompletedProcess:
        return run_local_cli(["state", "get"], self.tickets_root())

    def test_r4_1_review_slot_and_orchestrator_may_both_write(self):
        """两个合法署名位都要真的能写,写完值落库。"""
        first = self.service.state_set("judging_resolution", "1280x720", self.WRITER)
        self.assertEqual("1280x720", first["新值"])
        second = self.service.state_set("deploy_head_engine", "C0ED7A843", "总编")
        self.assertEqual("c0ed7a843", second["新值"])
        board = self.service.state_board()
        self.assertEqual("1280x720", board["值"]["judging_resolution"])
        self.assertEqual("c0ed7a843", board["值"]["deploy_head_engine"])

    def test_r4_2_other_slots_and_staff_are_refused_with_both_writers_named(self):
        """别位总监与员工一律拒,报错里两个合法署名位都要出现——不然被拒的人不知道该找谁。"""
        for actor in (SLOT, OTHER_SLOT, self.worker, "设计者", ""):
            with self.assertRaises(TicketError) as blocked:
                self.service.state_set("judging_resolution", "1280x720", actor)
            message = str(blocked.exception)
            self.assertIn("复检·合并", message)
            self.assertIn("总编", message)
        self.assertIsNone(self.service.state_board()["值"]["judging_resolution"])

    def test_r4_3_unknown_key_is_refused_with_every_legal_key_listed(self):
        """键不认识就拒,并且把合法键原样列全:只说「键不对」等于让人猜。"""
        with self.assertRaises(TicketError) as blocked:
            self.service.state_set("deploy_head", "c0ed7a8", "总编")
        message = str(blocked.exception)
        for key in ("judging_resolution", "deploy_head_engine", "deploy_head_server",
                    "walk_run_speed", "pending_shared_tools"):
            self.assertIn(key, message)

    def test_r4_4_state_get_prints_machine_readable_json_that_matches_what_was_set(self):
        """机器可读是本单的正题:stdout 必须是干净 JSON,值要对得上。"""
        self.service.state_set("judging_resolution", "1280x720", self.WRITER)
        self.service.state_set("walk_run_speed", "走=1.6,跑=3.2", self.WRITER)
        self.service.state_set("pending_shared_tools", self.TOOLS, "总编")
        result = self.state_get()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        board = json.loads(result.stdout)
        self.assertEqual("1280x720", board["值"]["judging_resolution"])
        self.assertEqual({"走": 1.6, "跑": 3.2}, board["值"]["walk_run_speed"])
        self.assertEqual(
            [{"名字": "取图链", "状态": "已改未并,在 feat/shot 上"},
             {"名字": "ticket.py", "状态": "T-000089 待复检"}],
            board["值"]["pending_shared_tools"],
        )

    def test_r4_4b_untouched_keys_stay_empty_and_the_cli_says_who_should_fill_them(self):
        """没填过的键就是空;提示走 stderr,stdout 仍是能直接 json.loads 的 JSON。"""
        result = self.state_get()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        board = json.loads(result.stdout)
        self.assertEqual(list(board["值"]), board["未填"])
        self.assertTrue(all(value is None for value in board["值"].values()))
        self.assertIn("未填", result.stderr)
        self.assertIn("state set", result.stderr)
        self.assertIn("复检席", result.stderr)

    def test_r4_5_every_set_writes_who_when_and_old_to_new_into_the_log(self):
        """一条日志要能回答:谁、何时、哪个键、旧值 → 新值。"""
        self.service.state_set("judging_resolution", "1280x720", self.WRITER)
        self.service.state_set("judging_resolution", "2560x1440", "总编")
        rows = [row for row in self.service.store.read_jsonl(self.service.store.log_path)
                if row.get("事件") == "state-set"]
        self.assertEqual(2, len(rows))
        self.assertEqual([self.WRITER, "总编"], [row["发言人"] for row in rows])
        self.assertTrue(all(row["时间"] for row in rows))
        self.assertEqual(["judging_resolution", "judging_resolution"], [row["值面键"] for row in rows])
        self.assertIsNone(rows[0]["旧值"])
        self.assertEqual("1280x720", rows[1]["旧值"])
        self.assertEqual("2560x1440", rows[1]["新值"])
        self.assertIn("1280x720 → 2560x1440", rows[1]["说明"])
        latest = self.service.state_board()["最近改动"]["judging_resolution"]
        self.assertEqual("总编", latest["改动人"])
        self.assertEqual("1280x720", latest["旧值"])

    def test_r4_6_receipt_ends_with_the_summary_and_shows_未填_for_empty_items(self):
        """员工窗第 0 步跑 receipt 就该看见这四项;摘要必须是最后一行,空的显示「未填」。"""
        ticket = self.dispatch("值面摘要")
        self.service.state_set("judging_resolution", "1280x720", self.WRITER)
        result = run_local_cli(["receipt", ticket["编号"]], self.tickets_root())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        summary = lines[-1]
        self.assertIn("当前值面", summary)
        self.assertIn("判据图 1280x720", summary)
        # 没填的三项照样露面,写着「未填」——看不见的项等于逼人回去翻接管件。
        self.assertIn("部署头 未填/未填", summary)
        self.assertIn("走/跑 未填", summary)
        self.assertIn("已改未并公共工具 未填", summary)
        self.assertNotIn("当前值面", "\n".join(lines[:-1]))

    def test_bad_values_are_refused_with_the_写法_spelled_out(self):
        """形状不对当场拒,并把该怎么写原样说出来;拒掉的不许留半个坏值。"""
        cases = {
            "judging_resolution": "1280*720",
            "deploy_head_engine": "头一个提交",
            "walk_run_speed": "走=1.6",
            "pending_shared_tools": "取图链",
        }
        for key, value in cases.items():
            with self.assertRaises(TicketError) as blocked:
                self.service.state_set(key, value, self.WRITER)
            self.assertIn(key, str(blocked.exception))
            self.assertIsNone(self.service.state_board()["值"][key])

    def test_values_survive_the_sqlite_backend_and_the_web_bundle(self):
        """值和工单库同一处存:换 SQLite 后端照样读得出,网页包也带上这一份。"""
        store = SqliteStore(self.root / "db" / "tickets.sqlite3")
        service = TicketService(store)
        service.state_set("deploy_head_server", "d03bdff9f", "总编")
        self.assertEqual("d03bdff9f", TicketService(SqliteStore(store.database)).state_board()["值"]["deploy_head_server"])
        bundle = service.build_bundle(self.root / "bundle.js")
        payload = json.loads(bundle.read_text(encoding="utf-8").split("=", 1)[1].strip().rstrip(";"))
        self.assertEqual("d03bdff9f", payload["state"]["值"]["deploy_head_server"])

    def test_web_top_bar_reads_the_board_instead_of_transcribing_it(self):
        """顶栏那四项必须来自服务端值面,不能是页面里另抄一份常量。"""
        page = repository_file_or_skip(self, "tools", "browser", "index.html").read_text(encoding="utf-8")
        script = repository_file_or_skip(self, "tools", "browser", "tickets.js").read_text(encoding="utf-8")
        self.assertIn('id="stateBoard"', page)
        self.assertIn("/api/state", script)
        self.assertIn("renderStateBoard", script)
        self.assertIn("最近改动文本", script)
class QuestionAssigneeAndPendingTests(TicketTestCase):
    """ ( 与设计者反映的美术总监之间发不了工单):

    「指派给」是各位扫自己活的那一格。把需求/阻塞的答复权交给了所属总监位,
    可建单时还把它们一律指给总编——收件位按「指派给」扫**看不见本该自己答的单**,
    发的人以为没发出去。实测全台面待答 40 张里 22 张这么错位,最老的单号还在三四百段。
    """

    def test_cross_slot_demand_lands_on_the_recipient_slot(self):
        """跨位发需求:指派给 = 收件位,不再是总编——这是「发不出去」的真因。"""
        ticket = self.service.create_question(
            "需求", OTHER_SLOT, "地面位发给别位的需求", "请把接口补上", SLOT,
        )
        self.assertEqual(OTHER_SLOT, ticket["所属总监位"])
        self.assertEqual(OTHER_SLOT, ticket["指派给"])

    def test_cross_slot_question_still_lands_on_the_recipient_slot(self):
        """疑问的跨位规则本来就是对的,别改坏了。"""
        ticket = self.service.create_question("疑问", OTHER_SLOT, "跨位疑问", "问一句", SLOT)
        self.assertEqual(OTHER_SLOT, ticket["指派给"])

    def test_self_question_still_goes_to_the_designer(self):
        """自己位上的疑问仍然送设计者,这一档没动。"""
        ticket = self.service.create_question("疑问", SLOT, "本位疑问", "问一句", "设计者")
        self.assertEqual("设计者", ticket["指派给"])

    def test_demand_filed_against_the_conductor_stays_with_the_conductor(self):
        """发给总编的需求仍归总编——别把该他答的也推走。"""
        ticket = self.service.create_question("需求", "总编", "发给总编的需求", "请裁定", SLOT)
        self.assertEqual("总编", ticket["指派给"])

    def test_pending_answer_line_lists_the_ids_and_is_empty_when_clean(self):
        """inbox 尾巴那一行:有待答就列出张数与单号;一张都不欠时不打空行。"""
        self.assertEqual("", self.service.pending_answer_line(OTHER_SLOT))
        first = self.service.create_question("需求", OTHER_SLOT, "第一张", "正文", SLOT)
        second = self.service.create_question("疑问", OTHER_SLOT, "第二张", "正文", SLOT)
        line = self.service.pending_answer_line(OTHER_SLOT)
        self.assertIn("待答 2 张", line)
        self.assertIn(first["编号"], line)
        self.assertIn(second["编号"], line)
        # 答掉一张,行里就只剩另一张
        self.service.answer(first["编号"], "受理,明天给", OTHER_SLOT)
        line = self.service.pending_answer_line(OTHER_SLOT)
        self.assertIn("待答 1 张", line)
        self.assertNotIn(first["编号"], line)

    def test_backfill_only_lists_until_apply_is_given(self):
        """回填默认只打清单不写——里面可能真有该总编答的,先给人看一眼。"""
        ticket = self.service.create_question("需求", OTHER_SLOT, "历史错位单", "正文", SLOT)
        stored = self.service.store.load_ticket(ticket["编号"])
        stored["指派给"] = "总编"          # 造一张老口径的单
        self.service.store.save_ticket(stored, "set", SLOT, "造历史错位")
        dry = self.service.backfill_question_assignees(False)
        self.assertEqual(1, dry["命中"])
        self.assertFalse(dry["已写入"])
        self.assertEqual("总编", self.service.store.load_ticket(ticket["编号"])["指派给"])
        done = self.service.backfill_question_assignees(True)
        self.assertEqual(1, done["命中"])
        self.assertTrue(done["已写入"])
        self.assertEqual(OTHER_SLOT, self.service.store.load_ticket(ticket["编号"])["指派给"])

    def test_backfill_leaves_the_conductors_own_demands_alone(self):
        """所属位就是总编的那些,回填一个都不许碰。"""
        ticket = self.service.create_question("需求", "总编", "该他答的", "正文", SLOT)
        report = self.service.backfill_question_assignees(True)
        self.assertNotIn(ticket["编号"], [row["编号"] for row in report["明细"]])
        self.assertEqual("总编", self.service.store.load_ticket(ticket["编号"])["指派给"])

    def test_a_decision_ticket_is_not_counted_against_the_slot_that_filed_it(self):
        """总编判退第一轮点出的真例:拍板单送设计者,发起位不欠它(那一类)。"""
        decision = self.service.create_question(
            "拍板", SLOT, "发起位送设计者的拍板", VALID_DECISION_BODY, SLOT,
        )
        self.assertEqual("设计者", decision["指派给"])
        self.assertEqual(SLOT, decision["所属总监位"])
        self.assertEqual([], self.service.pending_answers(SLOT))
        self.assertEqual("", self.service.pending_answer_line(SLOT))

    def test_a_self_slot_question_waiting_on_the_designer_is_not_counted_either(self):
        """本位自己提给设计者的疑问同理:指派给是设计者,本位没欠。"""
        self.service.create_question("疑问", SLOT, "等设计者答的疑问", "问一句", SLOT)
        self.assertEqual([], self.service.pending_answers(SLOT))

    def test_the_line_still_counts_what_this_slot_really_owes(self):
        """反向:真该本位答的仍要数进去,别为了修上面那条把闸修哑。"""
        mine = self.service.create_question("需求", SLOT, "别位发来的需求", "请办", OTHER_SLOT)
        self.assertEqual(SLOT, mine["指派给"])
        self.assertIn(mine["编号"], [row["编号"] for row in self.service.pending_answers(SLOT)])
        self.assertIn(mine["编号"], self.service.pending_answer_line(SLOT))

    def test_cli_inbox_tail_and_pending_mine(self):
        """命令行两头都要真的认:inbox 尾巴带待答行、list --pending-mine 列得出来。"""
        root = self.root / "cli-pending"
        service = TicketService(TicketStore(root))
        service.staff_new(SLOT, "sol")
        ticket = service.create_question("需求", SLOT, "命令行待答", "正文", OTHER_SLOT)
        shown = run_local_cli(["inbox", "--slot", SLOT, "--for", SLOT], root)
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertIn("你位当前待答", shown.stdout)
        self.assertIn(ticket["编号"], shown.stdout)
        mine = run_local_cli(["list", "--slot", SLOT, "--pending-mine"], root)
        self.assertEqual(0, mine.returncode, mine.stderr)
        self.assertIn(ticket["编号"], mine.stdout)


class InternalMergedIsTerminalTests(TicketTestCase):
    """内部单并线即到头,不该再老化、不该再占「上服」那一格。

    2026-09-07 设计者当面问这几个 live 部署单为什么卡住—— 卡 38 小时、
     卡 37 小时,两张都是已合并的内部单,没有人该动它们,是工具没跟上规矩。
    """

    def merged(self, internal: bool):
        ticket = self.service.create_dispatch(
            SLOT, "并线到头", ["DECISIONS.md:测试"], "工单台" if internal else "主界面/面板根",
            self.worker, task_tier="乙", deliverables=[str(self.deliverable)], internal=internal,
        )
        self.service.claim(ticket["编号"], self.worker)
        if internal:
            self.service.submit(ticket["编号"], "验证完成", "python -m pytest", "all passed")
        else:
            self.service.attach(ticket["编号"], str(self.picture()), "world", self.worker)
            self.service.submit(ticket["编号"], "登录后界面已出现")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        return self.merged_ticket(ticket["编号"], "独立复检")

    def test_internal_merged_never_goes_stale(self):
        """内部单并线之后,过多久都不算「卡住了」。"""
        ticket = self.merged(True)
        later = datetime.now().astimezone() + timedelta(hours=200)
        self.assertIsNone(self.service.stale_info(ticket, later))
        self.assertTrue(service_module.is_terminal(ticket))

    def test_player_facing_merged_still_goes_stale(self):
        """玩家可感知单不适用:它还欠一张真登录图,超 24 小时照旧算卡住。"""
        ticket = self.merged(False)
        later = datetime.now().astimezone() + timedelta(hours=30)
        info = self.service.stale_info(ticket, later)
        self.assertIsNotNone(info)
        self.assertEqual("已合并", info["状态"])
        self.assertFalse(service_module.is_terminal(ticket))

    def test_digest_stall_count_leaves_internal_merged_out(self):
        """日览的停滞段也读同一条判据,别一处改一处不改。"""
        self.merged(True)
        lines = "\n".join(self.service.digest(24))
        self.assertNotIn("并线到头", lines.split("[停滞]", 1)[-1] if "[停滞]" in lines else "")

    def test_front_end_uses_the_same_judgement(self):
        """前端与服务端必须是同一条判据:两处各写一套,页面与命令行会各说各话。"""
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn('function isTerminal(t){return NON_STALE_STATES.has(t.状态)||(t.状态==="已合并"&&!!t.非玩家可感知)}', script)
        self.assertIn("if(isTerminal(t)||t.类型===\"阻塞\")return null;", script)
        self.assertIn('if(t.状态==="已合并"&&isTerminal(t))return 6;', script)

def unwritable_memory_path(root: Path) -> Path:
    """一条**必然**写不成的记忆件路径，用来钉「重刷失败不许拖垮交板」。

    任务书写的是「指到一个不存在的盘符」。盘符在 Windows 上要现找一个空的（写死 Q:
    可能正好有人挂了盘），在 Linux 上根本不存在这个概念——服务器上跑同一套用例，
    `Q:\\x\\y.md` 只是个相对文件名，会**写成功**，用例就假绿了。
    所以两边都取同一类失败：让父目录是一个已经存在的**普通文件**，mkdir 必抛 OSError。
    """
    blocker = root / "这是个文件不是目录"
    blocker.write_text("x", encoding="utf-8")
    return blocker / "工位记忆" / "记忆.md"


class SlotMemoryTests(TicketTestCase):
    """固定工位的记忆闭环（设计者 2026-09-07 当面口述）。

    ★三条口述 + 总编两条硬约束：
    · 名册记「固定工位」与记忆 md 路径，开窗卡自动插「先读记忆」；
    · 记忆件的骨架由工具从**已交板**的单自动生成，每一行都回指到某张单，员工只补一小节；
    · 交板留「给下一窗」，判卷人可以划掉错的行——划掉不是删除。
    ★记忆件是快照：第 0 步「先核分支头与绿数」写死在生成物顶部，不是可选项。
    """

    def setUp(self) -> None:
        super().setUp()
        self.memory = self.root / "工位记忆" / f"{self.worker}.md"

    # ── 造数据 ────────────────────────────────────────────────────────────
    def internal_dispatch(self, title: str = "内部工具单", assign: str | None = None):
        self.serial = getattr(self, "serial", 0) + 1
        taskbook = self.root / f"tb-{self.serial}.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", self.worker if assign is None else assign,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True, taskbook=str(taskbook),
        )

    def submitted(self, title: str = "内部工具单", handoff: str = "", raw: str = "44 passed"):
        ticket = self.internal_dispatch(title)
        self.service.claim(ticket["编号"], self.worker)
        return self.service.submit(
            ticket["编号"], "内部验证", "python -m pytest", raw, handoff=handoff,
        )

    def generated(self, out: Path | None = None, max_lines: int = 400) -> str:
        path = self.service.memory_export(self.worker, out or self.memory, max_lines)
        return Path(path).read_text(encoding="utf-8")

    # ── R5 1 ──────────────────────────────────────────────────────────────
    def test_1_staff_fix_marks_the_slot_and_unfix_keeps_the_path(self):
        """staff fix 打标记与路径；staff unfix 取消标记，但路径**保留**便于回看。"""
        member = self.service.staff_fix(self.worker, SLOT, str(self.memory))
        self.assertTrue(member["固定工位"])
        self.assertEqual(str(self.memory), member["记忆md路径"])
        # 父目录还不在只提醒不拦：记忆件是 memory export 生成的，先有标记才有第一次导出。
        self.assertIn("父目录还不在", member["提示"])
        listed = next(row for row in self.service.list_staff(SLOT) if row["员工名"] == self.worker)
        self.assertTrue(listed["固定工位"])
        released = self.service.staff_unfix(self.worker, SLOT)
        self.assertFalse(released["固定工位"])
        self.assertEqual(str(self.memory), released["记忆md路径"])
        self.assertEqual("", self.service.staff_memory_path(self.worker))
        # 落盘也要是这个样子，不能只在返回值里对。
        again = next(row for row in self.service.list_staff(SLOT) if row["员工名"] == self.worker)
        self.assertFalse(again["固定工位"])
        self.assertEqual(str(self.memory), again["记忆md路径"])

    # ── R5 2 ──────────────────────────────────────────────────────────────
    def test_2_only_the_owning_slot_or_the_conductor_may_fix(self):
        """别位总监与员工窗跑 staff fix 被拒，报错要说得出谁可以。"""
        for actor in (OTHER_SLOT, self.worker, "设计者"):
            with self.subTest(actor=actor):
                with self.assertRaises(TicketError) as caught:
                    self.service.staff_fix(self.worker, actor, str(self.memory))
                message = str(caught.exception)
                self.assertIn(SLOT, message)
                self.assertIn("总编", message)
                self.assertIn(actor, message)
        # 本位总监与总编都放行；unfix 的权限与 fix 对称。
        self.assertTrue(self.service.staff_fix(self.worker, SLOT, str(self.memory))["固定工位"])
        self.assertTrue(self.service.staff_fix(self.worker, "总编", str(self.memory))["固定工位"])
        with self.assertRaises(TicketError):
            self.service.staff_unfix(self.worker, OTHER_SLOT)

    # ── R5 3 ──────────────────────────────────────────────────────────────
    def test_3_dispatch_lines_only_change_for_a_fixed_slot(self):
        """固定工位的单执行行带记忆 md；非固定工位的单其余各行**逐字不变**。

        ★对照组用的是「打过标记又 unfix、路径仍在名册里」的同一位员工：
        只按「有没有路径」判断的写法会在这里露馅——路径一直在，变的只有固定工位标记。
        """
        ticket = self.internal_dispatch("开窗指令")
        before = self.service.dispatch_instructions(ticket)
        self.assertEqual(3, len(before))
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        fixed = self.service.dispatch_instructions(ticket)
        self.assertEqual(3, len(fixed))
        for index in (0, 2):
            self.assertEqual(before[index], fixed[index])
        self.assertIn(str(self.memory), fixed[1])
        self.assertIn("先核分支头与绿数再干活", fixed[1])
        self.assertNotIn(str(self.memory), fixed[0])
        self.assertNotIn(str(self.memory), fixed[2])
        self.service.staff_unfix(self.worker, SLOT)
        self.assertEqual(before, self.service.dispatch_instructions(ticket))
        # 网页与 CLI 消费的是同一处，两个出口都得跟着变。
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        self.assertEqual(fixed, self.service.ticket_view(ticket)["开窗指令"])
        self.assertIn(str(self.memory), self.service.dispatch_instruction_text(ticket))

    # ── R5 4 ──────────────────────────────────────────────────────────────
    def test_4_only_submitted_tickets_go_into_the_memory_file(self):
        """memory export 只收这位**已交板**的单；没交板的一张都不进。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        done = self.submitted("交过板的")
        fresh = self.internal_dispatch("还没认领的")
        claimed = self.internal_dispatch("认领了没交板的")
        self.service.claim(claimed["编号"], self.worker)
        text = self.generated()
        self.assertIn(done["编号"], text)
        self.assertNotIn(fresh["编号"], text)
        self.assertNotIn(claimed["编号"], text)
        # 判退回「返工」的单交过板，仍然要留在记忆件里——上一窗踩的坑正是这种单最值钱。
        self.service.judge(done["编号"], False, SLOT, "再改一版", REWORK_VERDICT, "模型")
        self.assertIn(done["编号"], self.generated())

    # ── R5 5 ──────────────────────────────────────────────────────────────
    def test_5_step_zero_is_written_into_the_top_of_the_generated_file(self):
        """生成的 md 顶部含「第 0 步」那几行**原文**，且排在第一条正文之前。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        ticket = self.submitted("有第 0 步")
        text = self.generated()
        self.assertIn(service_module.MEMORY_STEP_ZERO, text)
        for line in (
            "## 第 0 步(每次开窗必做,不许跳)",
            "1. `git fetch` 后核主干分支的短号与本文件记的是否一致;",
            "2. 在自己的工作树上跑一次全量,拿到**当下**的绿数;",
            "3. 本文件里的分支头、绿数、行号**只当线索不当事实**——它是快照,写下那一刻起就在过期。",
            "   对不上就以现在跑出来的为准,并在本单里报一行。",
        ):
            self.assertIn(line, text.splitlines(), line)
        self.assertLess(text.index("## 第 0 步"), text.index(ticket["编号"]))
        # 快照会过期这件事在生成物里不止一处：顶部横幅一处、第 0 步第 3 条一处。
        self.assertIn("只当线索不当事实", text)
        self.assertIn("快照", text)

    # ── R5 6 ──────────────────────────────────────────────────────────────
    def test_6_every_entry_points_back_at_a_ticket_with_branch_and_model(self):
        """每一条含单号、分支或「证据里没写」、实际模型或「未标」。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        with_branch = self.submitted("证据里有分支", raw="366 passed 于 feat/desk-slot-memory 265cb53b9")
        without = self.submitted("证据里没分支", raw="全绿")
        # 一张单填上「实际模型」，一张不填：两种都要写得出，不许静默留空。
        stored = self.service.store.load_ticket(with_branch["编号"])
        stored["实际模型"] = "opus"
        self.service.store.save_ticket(stored, "set", SLOT, "补实际模型")
        text = self.generated()
        self.assertIn(f"### {with_branch['编号']} · 证据里有分支", text)
        self.assertIn("分支 feat/desk-slot-memory", text)
        self.assertIn("265cb53b9", text)
        self.assertIn("本节由模型 opus 写", text)
        self.assertIn(f"### {without['编号']} · 证据里没分支", text)
        self.assertIn("证据里没写", text)
        self.assertIn("本节由模型 未标 写", text)
        # 每一条都要能回指到某张单：正文里的每个 ### 标题都带一个真单号。
        headings = [line for line in text.splitlines() if line.startswith("### ")]
        self.assertEqual(2, len(headings))
        for heading in headings:
            self.assertRegex(heading, r"^### T-\d{6} · ")

    # ── R5 7 ──────────────────────────────────────────────────────────────
    def test_7_max_lines_archives_the_oldest_entries(self):
        """--max-lines 超限时最旧的条目进 .archive.md，主文件顶部写明归档了几条。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        tickets = [self.submitted(f"第{index}张")["编号"] for index in range(1, 5)]
        full = self.generated()
        self.assertTrue(all(ticket_id in full for ticket_id in tickets))
        archive = service_module.memory_archive_path(self.memory)
        self.assertFalse(archive.exists())
        # 头（含第 0 步）约 15 行，一条约 8 行：给 25 行只装得下最后一条。
        trimmed = self.generated(max_lines=25)
        self.assertIn("更早的 3 条已归档到", trimmed)
        self.assertIn(str(archive), trimmed)
        self.assertIn(tickets[-1], trimmed)
        for ticket_id in tickets[:-1]:
            self.assertNotIn(ticket_id, trimmed)
        archived = archive.read_text(encoding="utf-8")
        for ticket_id in tickets[:-1]:
            self.assertIn(ticket_id, archived)
        # 归档是**追加**不是覆盖：再刷一次，上一轮归档的内容还在。
        self.generated(max_lines=25)
        again = archive.read_text(encoding="utf-8")
        self.assertGreater(len(again), len(archived))
        self.assertEqual(2, again.count(tickets[0]))
        # 第 0 步永远不被挪走——挪走了的记忆件比没有记忆件更坏。
        self.assertIn(service_module.MEMORY_STEP_ZERO, trimmed)

    # ── R5 8 ──────────────────────────────────────────────────────────────
    def test_8_handoff_is_stored_and_a_judge_can_strike_a_line(self):
        """submit --handoff 落库；judge --strike-handoff 划掉而不是删掉；行号越界被拒。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        ticket = self.submitted("留一句给下一窗", handoff="真源在 A\n那个数是错的,别信\n下一窗从 R2 起手")
        rows = service_module.handoff_rows(self.service.store.load_ticket(ticket["编号"]))
        self.assertEqual(["真源在 A", "那个数是错的,别信", "下一窗从 R2 起手"], [row["文字"] for row in rows])
        with self.assertRaises(TicketError) as caught:
            self.service.judge(ticket["编号"], True, SLOT, "", PASS_VERDICT, strike_handoff="4")
            self.fail("行号越界必须被拒")
        self.assertIn("共有 3 行", str(caught.exception))
        _, warning = self.service.judge(ticket["编号"], True, SLOT, "", PASS_VERDICT, strike_handoff="2")
        self.assertIn("已划掉", warning)
        struck = service_module.handoff_rows(self.service.store.load_ticket(ticket["编号"]))
        # ★划掉不是删除：原文一个字都不能少，只多一个「谁在什么时候认为它错了」。
        self.assertEqual(3, len(struck))
        self.assertEqual("那个数是错的,别信", struck[1]["文字"])
        self.assertEqual(SLOT, struck[1]["划掉判卷人"])
        self.assertTrue(struck[1]["划掉时间"])
        self.assertEqual("", struck[0]["划掉判卷人"])
        text = self.generated()
        self.assertIn("~~已划掉~~", text)
        self.assertIn("那个数是错的,别信", text)
        self.assertIn(f"判卷人划掉：{SLOT}", text)

    # ── R5 9 ──────────────────────────────────────────────────────────────
    def test_9_submit_and_judge_refresh_the_memory_file_by_themselves(self):
        """submit / judge 落库之后自动重刷：改一次 handoff 再交，md 内容跟着变。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        ticket = self.submitted("会自动重刷", handoff="第一版底数")
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "完成", ticket["记忆重刷提示"])
        self.assertTrue(self.memory.is_file())
        self.assertIn("第一版底数", self.memory.read_text(encoding="utf-8"))
        # 判退回返工，重新认领、改一句 handoff 再交：文件跟着走，不用有人手工再跑一次导出。
        _, warning = self.service.judge(ticket["编号"], False, SLOT, "再来一版", REWORK_VERDICT, "模型")
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "完成", warning)
        self.service.claim(ticket["编号"], self.worker)
        self.service.submit(
            ticket["编号"], "内部验证", "python -m pytest", "44 passed", handoff="第二版底数",
        )
        refreshed = self.memory.read_text(encoding="utf-8")
        self.assertIn("第二版底数", refreshed)
        self.assertNotIn("第一版底数", refreshed)
        # 非固定工位一律不重刷，也就不该多出这一行。
        self.service.staff_unfix(self.worker, SLOT)
        quiet = self.submitted("不重刷")
        self.assertNotIn("记忆重刷提示", quiet)

    # ── R5 10 ─────────────────────────────────────────────────────────────
    def test_10_a_failed_refresh_never_breaks_the_submit(self):
        """★重刷失败不许拖垮交板：路径写到写不进去的地方，submit 照样成功。

        工具的附加动作不能反过来卡住员工交板——这是本单的硬要求，不是「尽量」。
        """
        broken = unwritable_memory_path(self.root)
        self.service.staff_fix(self.worker, SLOT, str(broken))
        ticket = self.submitted("重刷会失败")
        self.assertEqual("待判", self.service.store.load_ticket(ticket["编号"])["状态"])
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "失败", ticket["记忆重刷提示"])
        self.assertFalse(broken.exists())
        # judge 那一头同样不许被拖垮。
        judged, warning = self.service.judge(ticket["编号"], True, SLOT, "", PASS_VERDICT)
        self.assertEqual("待复检", judged["状态"])
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "失败", warning)
        # 手工跑 memory export 时反过来：那是人主动要的动作，失败就要报出来，不能吞。
        with self.assertRaises(OSError):
            self.service.memory_export(self.worker)

    # ── 命令行两头都要真的认 ────────────────────────────────────────────────
    def test_cli_staff_fix_memory_export_and_submit_handoff(self):
        """命令行走一遍：staff fix / staff list 标出固定工位 / memory export / submit --handoff。"""
        root = self.root / "cli-memory"
        service = TicketService(TicketStore(root))
        worker = service.staff_new(SLOT, "sol")["员工名"]
        taskbook = self.root / "cli-tb.md"
        taskbook.write_text("# 任务书\n", encoding="utf-8")
        memory = self.root / "cli-memory-file" / f"{worker}.md"
        ticket = service.create_dispatch(
            SLOT, "命令行记忆件", ["DECISIONS.md:测试"], "工单台", worker,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True, taskbook=str(taskbook),
        )
        fixed = run_local_cli(["staff", "fix", worker, "--memory", str(memory), "--by", SLOT], root)
        self.assertEqual(0, fixed.returncode, fixed.stderr)
        self.assertIn("固定工位", fixed.stdout)
        listed = run_local_cli(["staff", "list", "--slot", SLOT], root)
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertIn("· 固定工位", listed.stdout)
        denied = run_local_cli(["staff", "fix", worker, "--memory", str(memory), "--by", OTHER_SLOT], root)
        self.assertNotEqual(0, denied.returncode)
        self.assertIn(SLOT, denied.stdout + denied.stderr)
        run_local_cli(["claim", ticket["编号"], "--by", worker], root)
        submitted = run_local_cli([
            "submit", ticket["编号"], "--evidence", "内部验证",
            "--verify-command", "python -m pytest", "--raw-output", "44 passed",
            "--handoff", "真源在 tools/tickets\n绿数别信,自己跑",
        ], root)
        self.assertEqual(0, submitted.returncode, submitted.stderr)
        self.assertIn(service_module.MEMORY_REFRESH_PREFIX + "完成", submitted.stdout)
        self.assertIn("绿数别信,自己跑", memory.read_text(encoding="utf-8"))
        exported = run_local_cli(["memory", "export", "--staff", worker, "--out", str(memory)], root)
        self.assertEqual(0, exported.returncode, exported.stderr)
        self.assertIn(str(memory), exported.stdout)
        judged = run_local_cli([
            "judge", ticket["编号"], "--pass", "--by", SLOT,
            "--verdict", "设计者怎么打开它：开这份工位记忆 md 看。通过。", "--strike-handoff", "2",
        ], root)
        self.assertEqual(0, judged.returncode, judged.stderr)
        self.assertIn("已划掉", judged.stdout)
        self.assertIn("~~已划掉~~", memory.read_text(encoding="utf-8"))


class AutoRetireAtTerminalTests(TicketTestCase):
    """非固定员工到终态自动退役 + 名册默认只显示在岗。

    总编 2026-09-07 追加的第四条,设计者已认。为什么非做成规则不可:
    「窗关了顺手跑一次 staff retire」这件事,工单台上线到今天**一次都没人跑过**——
    名册里堆着一批早就不在的窗,总监照派单下拉派过去,单子就那么停在「新建」。
    ★固定工位一律不退:它跨窗复用,退了下一窗连 claim 都进不来,
      正好砸掉那一批的目的。
    """

    def setUp(self) -> None:
        super().setUp()
        self.memory = self.root / "工位记忆" / f"{self.worker}.md"

    # ── 造数据 ────────────────────────────────────────────────────────────
    def internal_ticket(self, assign: str, title: str = "内部工具单"):
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", assign,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True,
        )

    def merged(self, assign: str, title: str = "内部工具单"):
        """内部单一路走到「已合并」——它在那里就是终态。"""
        ticket = self.internal_ticket(assign, title)
        self.service.claim(ticket["编号"], assign)
        self.service.submit(ticket["编号"], "内部验证", "python -m pytest", "44 passed")
        self.service.judge(ticket["编号"], True, "UI总监", verdict=PASS_VERDICT)
        return self.merged_ticket(ticket["编号"], "独立复检")

    def state_of(self, name: str) -> str:
        row = next(r for r in self.service.list_staff(SLOT, True) if r["员工名"] == name)
        return str(row["状态"])

    def retire_events(self) -> list[dict]:
        return [
            row for row in self.service.store.read_jsonl(self.service.store.log_path)
            if row.get("事件") == "staff-auto-retire"
        ]

    # ── 用例 1 ────────────────────────────────────────────────────────────
    def test_1_a_fixed_slot_is_never_retired(self):
        """固定工位到终态**不退**。"""
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        ticket = self.merged(self.worker)
        # 前提先自己核一遍:这张单确实到了「执行方可以走了」那一步,不退是因为固定工位,
        # 不是因为压根没触发——否则这条用例挖不出任何东西。
        self.assertTrue(service_module.is_done_for_staff(ticket))
        self.assertEqual("在岗", self.state_of(self.worker))
        self.assertEqual("", str(ticket.get("自动退役提示", "")))
        self.assertEqual([], self.retire_events())

    # ── 用例 2 ────────────────────────────────────────────────────────────
    def test_2_another_open_ticket_keeps_the_window_open(self):
        """非固定,但手上还有别的在办单:不退;那张也结了才轮到收窗。"""
        busy = self.internal_ticket(self.worker, "手上另一张")
        self.service.claim(busy["编号"], self.worker)
        done = self.merged(self.worker, "先做完的那张")
        self.assertEqual("在岗", self.state_of(self.worker))
        self.assertEqual("", str(done.get("自动退役提示", "")))
        # 作废也是终态:最后一张一走,窗就该收了。
        voided = self.service.void(busy["编号"], "建重了,并进前一张", SLOT)
        self.assertEqual("已收窗", self.state_of(self.worker))
        self.assertIn(self.worker, str(voided["自动退役提示"]))

    # ── 用例 3 ────────────────────────────────────────────────────────────
    def test_3_the_last_ticket_closes_the_window_and_leaves_a_line(self):
        """非固定且手上没有别的在办单:自动收窗,并在事件线记一行。"""
        ticket = self.merged(self.worker)
        self.assertEqual("已收窗", self.state_of(self.worker))
        notice = str(ticket["自动退役提示"])
        self.assertIn(self.worker, notice)
        self.assertIn("staff reopen", notice)
        events = self.retire_events()
        self.assertEqual(1, len(events))
        self.assertEqual(ticket["编号"], events[0]["工单号"])
        self.assertIn(self.worker, events[0]["说明"])
        # 收窗之后再派给他要被现有那道闸拦下,人话里得说得出怎么救。
        with self.assertRaises(TicketError) as raised:
            self.service.claim(self.internal_ticket(self.worker, "下一张")["编号"], self.worker)
        self.assertIn("staff reopen", str(raised.exception))

    # ── 用例 4 ────────────────────────────────────────────────────────────
    def test_4_the_roster_hides_the_retired_until_all(self):
        """staff list 默认只显示在岗;--all 才看得到退役的。"""
        second = self.service.staff_new(SLOT, "sol")["员工名"]
        self.merged(self.worker)
        self.assertEqual([second], [row["员工名"] for row in self.service.list_staff(SLOT)])
        self.assertIn(self.worker, [row["员工名"] for row in self.service.list_staff(SLOT, True)])
        # 命令行那一份要跟服务端同一把尺子:服务端改了、命令行照旧,两处就各说一套。
        default = run_local_cli(["staff", "list", "--slot", SLOT], self.service.store.root)
        self.assertEqual(0, default.returncode, default.stderr)
        self.assertNotIn(self.worker, default.stdout)
        self.assertIn(second, default.stdout)
        self.assertIn("--all", default.stdout)
        every = run_local_cli(["staff", "list", "--slot", SLOT, "--all"], self.service.store.root)
        self.assertEqual(0, every.returncode, every.stderr)
        self.assertIn(self.worker, every.stdout)
        self.assertIn("已收窗", every.stdout)

    # ── 用例 5 ────────────────────────────────────────────────────────────
    def test_5_the_accounting_never_notices_the_retirement(self):
        """账不受影响:退役编号仍在册,history 与模型合格率一分不少。

        账按**模型**统计不按编号——把同一位 reopen 回来再算一遍,两份必须逐字相等。
        """
        ticket = self.merged(self.worker)
        self.assertEqual("已收窗", self.state_of(self.worker))
        history = self.service.history(self.worker)
        self.assertEqual([ticket["编号"]], [row["编号"] for row in history["工单"]])
        retired_stats = self.service.model_statistics()
        # 记的是 sol 那一行(名字带着档位后缀),交板 1、判过 1——退役一分没少。
        scored = [row for row in retired_stats if row["模型"].startswith("sol")]
        self.assertEqual([(1, 1)], [(row["交板数"], row["判过"]) for row in scored])
        self.service.staff_reopen(self.worker)
        self.assertEqual(retired_stats, self.service.model_statistics())

    # ── 闸:阻塞不是终态 ──────────────────────────────────────────────────
    def test_6_blocking_is_not_done_and_still_holds_the_window(self):
        """阻塞既不触发收窗,也仍旧算这位手上的一张在办单。

        ★这就是不能直接复用 is_terminal 的地方:那一条把阻塞算终态(没人该动它、不报老化),
        可阻塞解开之后原员工还得接着做,而 claim 只认在岗——退了他就再也认领不回来。
        """
        blocked = self.internal_ticket(self.worker, "被挂起的那张")
        self.service.claim(blocked["编号"], self.worker)
        held = self.service.block(blocked["编号"], "等别位先答", service_module.CONDUCTOR_SLOT)
        self.assertTrue(service_module.is_terminal(held))
        self.assertFalse(service_module.is_done_for_staff(held))
        self.assertEqual("在岗", self.state_of(self.worker))
        # 再结掉别的单也不许收窗:阻塞那张还在他手上等着解。
        self.merged(self.worker, "同时在做的另一张")
        self.assertEqual("在岗", self.state_of(self.worker))
        self.assertEqual([], self.retire_events())

    # ── 闸:履历不是在办 ──────────────────────────────────────────────────
    def test_7_a_ticket_handed_to_someone_else_no_longer_holds_the_window(self):
        """改派走的单不算他手上的活:「经手工单号列表」是履历,改派之后不会撤回。

        照履历数的话,凡是被改派过一次的员工永远退不掉。
        """
        moved = self.internal_ticket(self.worker, "后来改派走的那张")
        self.service.claim(moved["编号"], self.worker)
        taker = self.service.staff_new(SLOT, "sol")["员工名"]
        self.service.edit(moved["编号"], SLOT, assign=taker)
        self.assertIn(moved["编号"], self.service.find_staff(self.worker)[1]["经手工单号列表"])
        self.merged(self.worker, "他自己那张")
        self.assertEqual("已收窗", self.state_of(self.worker))
        # 接手的那位手上还有活,不能跟着一起被收。
        self.assertEqual("在岗", self.state_of(taker))

    # ── 闸:附加动作不许弄挂主流程 ────────────────────────────────────────
    def test_8_a_failed_retirement_never_breaks_the_close(self):
        """名册这一步出任何问题,都只多一行字;结案本身照常落库成功。"""
        with mock.patch.object(
            TicketService, "_open_ticket_ids", side_effect=RuntimeError("名册读坏了"),
        ):
            ticket = self.merged(self.worker)
        self.assertEqual("已合并", self.service.store.load_ticket(ticket["编号"])["状态"])
        notice = str(ticket["自动退役提示"])
        self.assertIn("名册读坏了", notice)
        self.assertIn(f"staff retire {self.worker}", notice)
        self.assertEqual("在岗", self.state_of(self.worker))

    # ── 闸:前端与服务端同一条判据 ────────────────────────────────────────
    def test_9_the_page_hides_the_retired_and_shows_the_notice(self):
        """网页呼出名单默认只显示在岗,并且要把收窗那句话弹给人看。"""
        script = (ROOT / "tools" / "browser" / "tickets.js").read_text(encoding="utf-8")
        self.assertIn("const onDuty=staff.filter(m=>m.状态==='在岗'),retired=staff.filter(m=>m.状态!=='在岗');", script)
        self.assertIn("已收窗 ${retired.length} 位", script)
        self.assertIn("${result?.自动退役提示?`\\n${result.自动退役提示}`:''}", script)

    # ── 闸:命令行端到端 ──────────────────────────────────────────────────
    def test_10_the_command_line_says_it_out_loud(self):
        """结案类命令的回执必须当场说出「名册被动过了」,别让人事后才发现。"""
        root = self.service.store.root
        ticket = self.internal_ticket(self.worker, "命令行走一遍")
        self.service.claim(ticket["编号"], self.worker)
        voided = run_local_cli(
            ["void", ticket["编号"], "--reason", "建错了", "--by", SLOT], root,
        )
        self.assertEqual(0, voided.returncode, voided.stderr)
        self.assertIn(service_module.AUTO_RETIRE_PREFIX, voided.stdout)
        self.assertIn(self.worker, voided.stdout)
        self.assertEqual("已收窗", self.state_of(self.worker))


class StaticAssetCacheHeaderTests(HttpServiceTests):
    """静态资产必须带缓存指令(根因候选:旧 JS 配今天的数据)。

    /tickets.js 原来只有 Last-Modified、没有任何 Cache-Control,浏览器按启发式
    缓存可以拿几天前的脚本配今天的数据跑;no-cache 每次再验证、可 304,不加流量。
    API 响应自己的 no-store 不许被盖掉。
    """

    def test_static_assets_get_no_cache_and_api_keeps_no_store(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", "/tickets.js")
        static = connection.getresponse()
        static.read()
        self.assertEqual(200, static.status)
        self.assertEqual("no-cache", static.getheader("Cache-Control"))
        connection.close()

        status, _payload = self.request("GET", "/api/tickets")
        self.assertEqual(200, status)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", "/api/tickets")
        api = connection.getresponse()
        api.read()
        self.assertEqual("no-store", api.getheader("Cache-Control"))
        connection.close()


class StaffNumberWidenedTests(TicketTestCase):
    """员工编号扩到三位(后端·服务 -99 用完)。

    不走回收重用:员工号被判语/断点件/章程/记忆大量引用,同一个号在两个时期
    指向两个人,查判退责任会错到别人头上。
    """

    def test_1_three_digit_numbers_register_and_sign_back_to_the_slot(self):
        staff = self.service.store.load_staff()
        group = staff.setdefault("总监位", {}).setdefault(SLOT, {"下一个编号": 1, "员工": []})
        group["下一个编号"] = 100
        self.service.store.save_staff(staff)
        member = self.service.staff_new(SLOT, "sol")
        self.assertEqual(f"{SLOT}-100", member["员工名"])
        # -100 的署名必须能收敛回总监位,否则权限闸认不出这是哪位的员工。
        self.assertEqual(SLOT, self.service._signer_slot(f"{SLOT}-100"))
        self.assertEqual(SLOT, self.service._signer_slot(f"{SLOT}-07"))

    def test_2_the_ceiling_still_blocks_at_999(self):
        staff = self.service.store.load_staff()
        group = staff.setdefault("总监位", {}).setdefault(SLOT, {"下一个编号": 1, "员工": []})
        group["下一个编号"] = 1000
        self.service.store.save_staff(staff)
        with self.assertRaisesRegex(TicketError, "上限 -999"):
            self.service.staff_new(SLOT, "sol")


class DeskReadPathPerformanceTests(TicketTestCase):
    """读路径上两处「同一件事做了 N 遍」的收口。

    线上实测(在服务器本机量,绕开线路与代理,2400 张单):
    GET /api/tickets 纯服务端 3.8/4.7/3.8 秒,GET /api/changes?since=0 6.7 秒。
    病根两个,都不在业务上:
    · SqliteStore.ensure() 被**每一次读**无条件重跑——7 条 CREATE TABLE ＋ 4 条
      INSERT OR IGNORE,实测 0.79ms/次;更糟的是**读路径在写库**,要去抢 locked()
      那把 BEGIN IMMEDIATE 的写锁;
    · list_cards → card_view → ticket_view → dispatch_instructions → staff_memory_path
      → find_staff → store.load_staff():整条链零缓存,每张单重开一条 sqlite 连接
      把整册名册解析一遍(2.5ms/次)。

    ★本类第一条是硬闸:**输出逐字节不变**。两处改的都只是「少做几遍」,
      结果差一个字节就说明改坏了。快路(名册整趟取一次)与慢路(每张单各取一次,
      即改前那条路——card_view 不传名册时走的正是它)必须字节相同。
    """

    def setUp(self) -> None:
        super().setUp()
        self.memory = self.root / "工位记忆" / f"{self.worker}.md"
        self.other_worker = self.service.staff_new(SLOT, "sol")["员工名"]
        self.serial = 0

    # ── 造数据 ────────────────────────────────────────────────────────────
    def internal_dispatch(self, title: str, assign: str | None = None, taskbook: bool = True):
        self.serial += 1
        path = ""
        if taskbook:
            book = self.root / f"tb-{self.serial}.md"
            book.write_text("# 任务书\n", encoding="utf-8")
            path = str(book)
        return self.service.create_dispatch(
            SLOT, title, ["DECISIONS.md:测试"], "工单台", self.worker if assign is None else assign,
            task_tier="乙", deliverables=[str(self.deliverable)], internal=True, taskbook=path,
        )

    def mixed_desk(self) -> None:
        """四张单各走 card_view 里的一条分支。

        ★少哪一张,等价断言就往恒真上滑一格:全是「没有任务书」的单的话,
          dispatch_instructions 第一行就 return [],名册根本不会被读到,
          那这条「传名册与不传名册结果相同」就什么也没证。
        """
        self.service.staff_fix(self.worker, SLOT, str(self.memory))
        self.internal_dispatch("固定工位·有任务书")                       # 走「先读记忆 md」那一支
        self.internal_dispatch("非固定工位·有任务书", self.other_worker)   # 查得到人但不加记忆行
        self.internal_dispatch("没有任务书路径", taskbook=False)           # 压根到不了名册
        self.service.create_question("拍板", SLOT, "要设计者拍的", VALID_DECISION_BODY)  # 正文要留着

    @staticmethod
    def blob(value) -> bytes:
        return json.dumps(value, ensure_ascii=False).encode("utf-8")

    # ── 一、输出逐字节不变 ────────────────────────────────────────────────
    def test_t2413_1_list_cards_is_byte_identical_to_the_per_ticket_roster_path(self):
        """快路(名册一次)与慢路(每张单一次)输出逐字节相同。"""
        self.mixed_desk()
        rows = self.service._filtered_tickets()
        self.assertEqual(4, len(rows))
        slow = [self.service.card_view(row) for row in rows]  # 改前那条路,一字未改
        fast = self.service.list_cards()
        self.assertEqual(self.blob(slow), self.blob(fast))

        # ★判据非恒真:夹具真的把四条分支都走了一遍,名册真的被查过。
        lines = [row["开窗指令"] for row in fast]
        second = [row[1] for row in lines if row]
        self.assertTrue(any(str(self.memory) in line for line in second), "没有一张单走到记忆 md 那一支")
        self.assertTrue(any(str(self.memory) not in line for line in second), "没有一张单走非固定工位那一支")
        self.assertTrue(any(not row for row in lines), "没有一张单走「回空列表」那一支")
        self.assertTrue(any("正文" in row for row in fast), "没有一张单留着正文")

    def test_t2413_2_full_text_and_delta_rows_are_byte_identical_too(self):
        """全文那一份与增量那一份同样逐字节不变——三条出口共用同一条名册链。"""
        self.mixed_desk()
        rows = self.service._filtered_tickets()
        self.assertEqual(
            self.blob([self.service.ticket_view(row) for row in rows]),
            self.blob(self.service.list_tickets()),
        )
        full = {row["编号"]: row for row in self.service.list_cards()}
        delta = self.service.changes_since(0)["工单"]
        self.assertEqual(sorted(full), sorted(row["编号"] for row in delta))
        for row in delta:
            self.assertEqual(self.blob(full[row["编号"]]), self.blob(row))

    # ── 二、名册整趟只取一次 ──────────────────────────────────────────────
    def test_t2413_3_the_roster_is_read_once_per_list_not_once_per_ticket(self):
        """改前是「每张单一次」;现在整趟一次,空增量一次都不取。"""
        self.mixed_desk()
        store = self.service.store
        with mock.patch.object(store, "load_staff", wraps=store.load_staff) as spy:
            cards = self.service.list_cards()
        self.assertEqual(4, len(cards))
        self.assertEqual(1, spy.call_count)

        cursor = store.log_cursor()
        with mock.patch.object(store, "load_staff", wraps=store.load_staff) as idle:
            self.assertEqual([], self.service.changes_since(cursor)["工单"])
        self.assertEqual(0, idle.call_count)

    # ── 三、ensure() 每个实例只真跑一次 ───────────────────────────────────
    def test_t2413_4_ensure_still_builds_every_table_on_a_brand_new_database(self):
        """加了开关之后,**第一次**照样要把库建出来——这是那个开关最容易改坏的一面。"""
        database = self.root / "全新库" / "tickets.sqlite"
        self.assertFalse(database.exists())
        store = SqliteStore(database)
        store.ensure()
        with store._database() as connection:
            built = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(
            {"tickets", "images", "threads", "staff", "slots", "log", "model_scores", "metadata"},
            built - {"sqlite_sequence"},
        )
        # 四条 INSERT OR IGNORE 的默认行也要在:缺了它 load_staff / next_ticket_id 当场炸。
        self.assertEqual(
            sorted(SqliteStore._default_staff()["总监位"]), sorted(store.load_staff()["总监位"]),
        )
        self.assertEqual(0, store.read_json(store.counter_path, {"最后编号": -1})["最后编号"])
        self.assertEqual({}, store.read_json(store.state_path, {"没建出来": True}))
        self.assertEqual([], store.list_tickets())
        self.assertEqual("T-000001", store.next_ticket_id())

    def test_t2413_5_ensure_stops_rebuilding_the_schema_after_the_first_call(self):
        """读路径不再重跑建表;而开关是**实例级**的,换个实例(＝换个进程)照样建。"""
        database = self.root / "重复建表" / "tickets.sqlite"
        store = SqliteStore(database)
        store.ensure()
        # _put_singleton 是建表那一段的一部分(slots ＋ staff 两条),真跑一趟就是 2 次。
        with mock.patch.object(store, "_put_singleton", wraps=store._put_singleton) as spy:
            for _ in range(20):
                store.list_tickets()
                store.read_json(store.staff_path, {})
                store.log_cursor()
                store.tickets_changed_since(0)
                store.read_jsonl(store.log_path)
        self.assertEqual(0, spy.call_count)  # 改前:这 100 次读要重跑 100 遍建表

        second = SqliteStore(database)
        with mock.patch.object(second, "_put_singleton", wraps=second._put_singleton) as fresh:
            second.ensure()
            second.ensure()
        self.assertEqual(2, fresh.call_count)  # 只有头一趟那两条,第二趟在开头就回了


class KeepAliveAndCachingTests(TicketTestCase):
    """工单台「非常卡」的四条真因。设计者 2026-09-22 报,数都是服务器本机量的:

      1. 全程 HTTP/1.0 —— protocol_version 全仓零命中 ⇒ 没有 keep-alive,
         每个请求一次全新 TCP+TLS 握手;远程线路每次三个来回。
      2. 图片发 Cache-Control: no-store ⇒ 浏览器**绝不缓存**。最近 3000 条请求里
         1672 条是 /api/image/,去重后只有 300 张 —— 同一张图被重拉 12 次,次次全量正文。
      3. 静态件不压 —— 一个 3,766,647 字节的静态 js,
         带着 Accept-Encoding: gzip 请求也照回全量。
      4. log_message 读 self.path,而那三条出错路径上 self.path 还没被赋值 ⇒
         AttributeError ⇒ 本来能发出去的 400/505 变成空响应。

    ★开 keep-alive 是这一单最大的风险点:HTTP/1.1 下只要有一条响应的 Content-Length
      少了或写错,连接复用就会串包或吊死,而这台服务是 13 个位的工单台。
      所以下面的用例**一条都不许只断言响应头**——全部真发两个请求、真读两份正文。
    """

    def serve(self, directory: Path | None = None):
        handler = partial(TicketRequestHandler, directory=str(directory or ROOT / "tools" / "browser"))
        server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, token="t0ken")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # 与 ResponseCompressionTests 同一套顺序:addCleanup 后进先出,
        # 先登记 server_close、后登记 shutdown,才能保证先停 serve_forever 再关套接字。
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def connect(self, address):
        conn = http.client.HTTPConnection(address[0], address[1], timeout=10)
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def fetch(conn, path, extra=None):
        headers = {"X-Ticket-Token": "t0ken"}
        headers.update(extra or {})
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response, response.read()

    def save_image(self, name: str, payload: bytes) -> None:
        self.service.store.save_image(self.service.store.images_dir, name, payload)

    @staticmethod
    def png_bytes(color) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), color).save(buffer, format="PNG")
        return buffer.getvalue()

    # ---------- 一、keep-alive ----------

    def test_1_two_requests_really_ride_one_connection(self):
        """★硬闸:一条连接上连发两个请求、拿到两个**完整**响应。

        只断言「响应头里有 keep-alive」挡不住「第二个响应串包」——
        所以这里既钉死套接字对象没被换掉(真复用,不是 http.client 偷偷重连),
        也把两份正文都解析出来逐个比内容(真完整,不是读了半截)。
        """
        ticket = self.dispatch("复用连接")
        address = self.serve()
        conn = self.connect(address)

        first, raw_first = self.fetch(conn, "/api/tickets")
        self.assertEqual(200, first.status)
        self.assertEqual(11, first.version, "服务端还在说 HTTP/1.0,keep-alive 根本没开")
        self.assertFalse(first.will_close, "第一个响应就宣告要关连接,谈不上复用")
        reused = conn.sock
        self.assertIsNotNone(reused)

        second, raw_second = self.fetch(conn, "/api/slots")
        # ★这一条是「真复用」的判据:http.client 一旦发现连接关了会**静默重连**,
        #   那时 conn.sock 会换成另一个对象,上面两个响应照样都是 200。
        self.assertIs(reused, conn.sock, "第二个请求换了套接字 ⇒ 连接没被复用,只是重连了")
        self.assertEqual(200, second.status)

        # 两份正文都要能完整解析,而且内容各是各的——串包/短读在这里必露馅。
        payload_first = json.loads(raw_first.decode("utf-8"))
        payload_second = json.loads(raw_second.decode("utf-8"))
        self.assertEqual(ticket["编号"], payload_first["result"][0]["编号"])
        self.assertIn("slots", payload_second["result"])
        self.assertNotIn("slots", payload_first["result"])

    def test_2_a_pipelined_pair_of_requests_does_not_desync(self):
        """两个请求**一次性写进套接字**(真流水线)——这是对 Content-Length 最狠的一条。

        任何一条响应多写或少写一个字节,第二份就对不齐,下面的解析立刻炸。
        """
        self.dispatch("流水线")
        address = self.serve()
        blob = (
            b"GET /api/state HTTP/1.1\r\nHost: desk\r\nX-Ticket-Token: t0ken\r\n\r\n"
            b"GET /api/tickets HTTP/1.1\r\nHost: desk\r\nX-Ticket-Token: t0ken\r\n"
            b"Connection: close\r\n\r\n"
        )
        with socket.create_connection(address, timeout=10) as sock:
            sock.sendall(blob)
            chunks = []
            while True:
                piece = sock.recv(65536)
                if not piece:
                    break
                chunks.append(piece)
        stream = b"".join(chunks)
        self.assertEqual(2, stream.count(b"HTTP/1.1 200 OK"), f"两个响应没都回来:{stream[:200]!r}")
        # 逐段按 Content-Length 切开,切得干净才算框架没错位。
        rest = stream
        for _ in range(2):
            head, _, rest = rest.partition(b"\r\n\r\n")
            length = int(re.search(rb"Content-Length: (\d+)", head).group(1))
            body, rest = rest[:length], rest[length:]
            self.assertEqual(length, len(body), "正文长度与 Content-Length 对不上 ⇒ 串包")
            json.loads(body.decode("utf-8"))
        self.assertEqual(b"", rest, f"两个响应之外还剩下字节 ⇒ 多写了:{rest[:120]!r}")

    def test_3_a_post_whose_body_is_never_read_must_not_poison_the_connection(self):
        """POST 的正文没被读走 ⇒ 那条连接必须关掉,否则剩下的字节会被当成下一个请求。

        走的是「路径不认识」那一条:服务端在读正文之前就回 404。
        ★把 _guard_request_body 撤掉,下面第二个请求会拿到 400(服务端把那堆 JSON
          的第一行当成了请求起始行)——这正是串包。
        """
        address = self.serve()
        conn = self.connect(address)
        body = json.dumps({"塞满": "让服务端有东西可以读错" * 20}, ensure_ascii=False).encode("utf-8")
        conn.request("POST", "/no-such-endpoint", body=body, headers={
            "X-Ticket-Token": "t0ken", "Content-Type": "application/json; charset=utf-8",
        })
        response = conn.getresponse()
        self.assertEqual(404, response.status)
        response.read()
        self.assertTrue(response.will_close, "正文没读走却留着连接复用 ⇒ 下一个请求必串包")

        # 复用被正确否决之后,后续请求照常能做(http.client 会自己重连)。
        follow, raw = self.fetch(conn, "/api/state")
        self.assertEqual(200, follow.status, f"下一个请求被上一次的残留正文毒化了:{raw[:200]!r}")
        self.assertTrue(json.loads(raw.decode("utf-8"))["ok"])

    def test_4_the_login_redirect_carries_a_length(self):
        """★没有 Content-Length 又不是 chunked 的响应,HTTP/1.1 客户端会一直读到连接关闭。

        未登录跳 /login 那条原来正是这样,开了复用之后会把浏览器吊在那里。
        """
        auth = AccountManager(self.root / "auth.sqlite")
        auth.init_admin("owner")
        handler = partial(TicketRequestHandler, directory=str(ROOT / "tools" / "browser"))
        server = TicketHTTPServer(("127.0.0.1", 0), handler, self.service, "", auth)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = self.connect(server.server_address)
        conn.request("GET", "/index.html")
        response = conn.getresponse()
        body = response.read()
        self.assertEqual(302, response.status)
        self.assertEqual("/login", response.getheader("Location"))
        self.assertEqual("0", response.getheader("Content-Length"))
        self.assertEqual(b"", body)
        # 带着长度就能接着用同一条连接——这才是「不吊死」的真判据。
        self.assertFalse(response.will_close)
        conn.request("GET", "/login")
        self.assertEqual(200, conn.getresponse().status)

    def test_5_the_idle_timeout_was_lowered_because_reuse_holds_a_thread(self):
        """空闲连接会占住自己那条工作线程直到超时,所以开复用的同时把这个数调下来了。"""
        self.assertLessEqual(TicketHTTPServer.connection_timeout, 10)
        self.assertGreaterEqual(TicketHTTPServer.connection_timeout, 5, "太短会误伤远程线路上的慢客户端")

    # ---------- 二、图片可缓存 ----------

    def test_6_an_image_is_revalidated_not_refetched(self):
        """★硬闸:第一趟 200 + ETag,第二趟带 If-None-Match 拿 304 且正文为空。"""
        payload = self.png_bytes((55, 90, 125))
        self.save_image("蓝图.png", payload)
        address = self.serve()
        conn = self.connect(address)

        first, raw = self.fetch(conn, "/api/image/" + quote("蓝图.png"))
        self.assertEqual(200, first.status)
        self.assertEqual(payload, raw)
        etag = first.getheader("ETag")
        self.assertTrue(etag and etag.startswith('"'), f"没发 ETag:{etag!r}")
        self.assertEqual(str(len(payload)), first.getheader("Content-Length"))
        # ★no-store 是「绝不缓存」,正是 1672 条请求只对应 300 张图的病根。
        self.assertNotIn("no-store", (first.getheader("Cache-Control") or ""))

        second, empty = self.fetch(conn, "/api/image/" + quote("蓝图.png"), {"If-None-Match": etag})
        self.assertEqual(304, second.status)
        self.assertEqual(b"", empty, "304 不许带正文")
        self.assertEqual(etag, second.getheader("ETag"))
        # 304 之后连接还能接着用:框架没错位。
        self.assertEqual(200, self.fetch(conn, "/api/state")[0].status)

    def test_7_an_overwritten_image_gets_a_new_etag_and_the_new_bytes(self):
        """★硬闸:图片允许**同名覆盖**(store.save_image),所以绝不能用 immutable 长缓存。

        覆盖之后客户端拿着旧 ETag 再问,必须换回 200 + 新图,不许是 304。
        ★ETag 按内容算而不是按 mtime+size 算,就是为了挡住「同尺寸、同一秒内覆盖」
          那一格——那一格的失败是静默的,屏上永远是旧图,没有任何报错。
        """
        before = self.png_bytes((10, 20, 30))
        self.save_image("会被覆盖.png", before)
        address = self.serve()
        conn = self.connect(address)
        first, raw_before = self.fetch(conn, "/api/image/" + quote("会被覆盖.png"))
        old_etag = first.getheader("ETag")
        self.assertEqual(before, raw_before)

        after = self.png_bytes((200, 160, 60))
        self.assertNotEqual(before, after)
        self.save_image("会被覆盖.png", after)

        second, raw_after = self.fetch(conn, "/api/image/" + quote("会被覆盖.png"), {"If-None-Match": old_etag})
        self.assertEqual(200, second.status, "同名覆盖之后还回 304 ⇒ 客户端永远拿不到新图")
        self.assertEqual(after, raw_after)
        self.assertNotEqual(old_etag, second.getheader("ETag"))
        # 新 ETag 立刻就能用来换 304,说明它是稳定的、不是每次都变。
        third, _ = self.fetch(conn, "/api/image/" + quote("会被覆盖.png"),
                              {"If-None-Match": second.getheader("ETag")})
        self.assertEqual(304, third.status)

    def test_8_images_are_never_cached_immutably(self):
        """口径钉死:no-cache/must-revalidate 可以,immutable 与长 max-age 不行。"""
        self.assertIn("must-revalidate", http_server_module.IMAGE_CACHE_CONTROL)
        self.assertIn("no-cache", http_server_module.IMAGE_CACHE_CONTROL)
        self.assertNotIn("immutable", http_server_module.IMAGE_CACHE_CONTROL)
        self.assertNotIn("max-age", http_server_module.IMAGE_CACHE_CONTROL)

    def test_9_a_bad_image_name_gets_a_real_reply_on_both_paths(self):
        """/img/ 那条原来在 try 之外:文件名不安全时**一个字节的响应都不发**。

        /api/image/ 同样的调用在 try 里、回 400。同一个错两种结果,这里对齐。
        """
        address = self.serve()
        conn = self.connect(address)
        for prefix in ("/img/", "/api/image/"):
            with self.subTest(prefix=prefix):
                response, raw = self.fetch(conn, prefix + "%2F%2E%2E%2Fsecret")
                self.assertEqual(400, response.status)
                self.assertIn("不安全", json.loads(raw.decode("utf-8"))["reason"])

    # ---------- 三、首屏那 3.59 MB ----------


    def test_11_the_offline_bundle_is_only_requested_on_file_urls(self):
        """线上 /data/tickets-bundle.js 是 404:它被 .gitignore 明令不入仓,
        而 pack.sh 用 git archive 打包 ⇒ 永远进不了线上包。

        但它是**离线**那条路的整份数据(API_MODE 只在 http/https 下为真,
        file:// 时 tickets.js 走 fallbackData() 读 window.TICKET_DESK_BUNDLE)。
        ⇒ 不删也不入仓,按协议挂:联机一次都不请求,离线照旧能用。
        """
        browser = ROOT / "tools" / "browser"
        if not (browser / "index.html").is_file():
            self.skipTest(f"{PACKAGE_TREE_SKIP_PREFIX},这条要读 {browser}")
        html = (browser / "index.html").read_text(encoding="utf-8")
        body = _strip_html_comments(html)
        mentions = [line for line in body.splitlines() if "tickets-bundle.js" in line]
        self.assertTrue(mentions, "离线那条路的数据不能整个删掉")
        # ★每一处提到它的地方都必须挂在 file: 那个条件下。
        #   无条件的 <script src=...> 就是线上每次开页白撞的那个 404。
        for line in mentions:
            self.assertIn("location.protocol", line, f"这一处是无条件请求:{line.strip()}")
            self.assertIn("file:", line, f"这一处不是只在离线下取:{line.strip()}")
        # 仓里确实不收它,所以「让它进 git」这条路是走不通的——这一条把前提钉住。
        ignored = subprocess.run(
            ["git", "check-ignore", "tools/browser/data/tickets-bundle.js"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if ignored.returncode not in (0, 1):
            self.skipTest("这台机器上没有可用的 git")
        self.assertEqual(0, ignored.returncode, "它不再被 .gitignore 挡着了,这条用例的前提要重写")

    # ---------- 四、静态件 gzip ----------

    def test_12_a_gzip_client_gets_the_static_file_compressed(self):
        """静态件原来一个字节都没压过——gzip 当初只加在 _json 那条路上。"""
        directory = self.root / "static"
        directory.mkdir()
        text = ("// 工单台静态件压缩用例\n" + "同一段中文注释会被压得很扁。\n" * 400).encode("utf-8")
        (directory / "big.js").write_bytes(text)
        address = self.serve(directory)
        conn = self.connect(address)

        packed_response, packed = self.fetch(conn, "/big.js", {"Accept-Encoding": "gzip"})
        self.assertEqual(200, packed_response.status)
        self.assertEqual("gzip", packed_response.getheader("Content-Encoding"))
        self.assertEqual("Accept-Encoding", packed_response.getheader("Vary"))
        self.assertEqual(text, gzip.decompress(packed), "解开必须与原文逐字节相同")
        self.assertEqual(len(packed), int(packed_response.getheader("Content-Length")))
        self.assertLess(len(packed), len(text) // 4)
        self.assertIn("javascript", packed_response.getheader("Content-Type"))

        # 压完还能接着用同一条连接:长度写对了。
        self.assertEqual(200, self.fetch(conn, "/api/state")[0].status)

    def test_13_a_client_that_does_not_ask_for_gzip_is_untouched(self):
        """★命令行那条路(remote.py 用 http.client)默认不发 Accept-Encoding,一个字节都不受影响。"""
        self.assertNotIn("Accept-Encoding", (ROOT / "tools" / "tickets" / "remote.py").read_text(encoding="utf-8"))
        directory = self.root / "static"
        directory.mkdir()
        text = ("x" * 80 + "\n").encode("utf-8") * 60
        (directory / "big.js").write_bytes(text)
        address = self.serve(directory)
        response, raw = self.fetch(self.connect(address), "/big.js")
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertEqual(text, raw)

    def test_14_binary_and_tiny_static_files_are_left_alone(self):
        """图片/字体本来就是压过的,再压一遍只费 CPU;几百字节的小件压完可能更大。"""
        directory = self.root / "static"
        directory.mkdir()
        # ★纯色 PNG 只有几百字节,会落进「太小不压」那一格,证明不了「二进制不压」。
        #   用噪声图逼出一个过阈值的真二进制件,两条分支才各自独立。
        noise = random.Random(20260922).randbytes(96 * 96 * 3)
        buffer = io.BytesIO()
        Image.frombytes("RGB", (96, 96), noise).save(buffer, format="PNG")
        (directory / "shot.png").write_bytes(buffer.getvalue())
        (directory / "tiny.js").write_bytes(b"var a=1;\n")
        self.assertGreater((directory / "shot.png").stat().st_size, http_server_module.GZIP_MIN_BYTES)
        address = self.serve(directory)
        conn = self.connect(address)
        for name in ("shot.png", "tiny.js"):
            with self.subTest(name=name):
                response, raw = self.fetch(conn, "/" + name, {"Accept-Encoding": "gzip"})
                self.assertEqual(200, response.status)
                self.assertIsNone(response.getheader("Content-Encoding"))
                self.assertEqual((directory / name).read_bytes(), raw)
        self.assertNotIn(".png", http_server_module.GZIP_STATIC_SUFFIXES)
        self.assertNotIn(".webp", http_server_module.GZIP_STATIC_SUFFIXES)

    def test_15_a_revalidated_static_file_comes_back_as_304(self):
        """静态页原来靠 no-cache + Last-Modified 换 304。

        压缩这条新路必须保住这个能力,否则 tickets.js 每次刷新都要重发一份压缩包。
        """
        directory = self.root / "static"
        directory.mkdir()
        (directory / "big.js").write_bytes(("行\n" * 2000).encode("utf-8"))
        address = self.serve(directory)
        conn = self.connect(address)
        first, packed = self.fetch(conn, "/big.js", {"Accept-Encoding": "gzip"})
        etag = first.getheader("ETag")
        self.assertTrue(etag and etag.endswith('-gzip"'), f"压缩版要有自己的 ETag:{etag!r}")
        second, empty = self.fetch(conn, "/big.js", {"Accept-Encoding": "gzip", "If-None-Match": etag})
        self.assertEqual(304, second.status)
        self.assertEqual(b"", empty)
        self.assertGreater(len(packed), 0)
        # 改了文件之后同一个 If-None-Match 必须换回 200:别把陈旧版锁死在客户端上。
        (directory / "big.js").write_bytes(("另一行\n" * 2000).encode("utf-8"))
        third, fresh = self.fetch(conn, "/big.js", {"Accept-Encoding": "gzip", "If-None-Match": etag})
        self.assertEqual(200, third.status)
        self.assertEqual(("另一行\n" * 2000).encode("utf-8"), gzip.decompress(fresh))

    # ---------- 五、log_message 那个真 bug ----------

    def test_16_a_malformed_request_line_still_gets_a_real_reply(self):
        """★线上 journal 里的真异常:log_message 读 self.path,而

            BaseHTTPRequestHandler.parse_request() 里 `self.command, self.path = command, path`
            是**最后**才赋值的,「Bad request version」这条路在赋值之前就
            send_error → log_error → log_message ⇒ AttributeError。
        后果不是少记一行日志,是那一次的 400/505 **整个发不出去,客户端收到空响应**。
        这条用例真发一行坏请求,看回来的是不是一个像样的响应。

        ★光看「响应回来了」不够:log_message 外面还兜了一层 try(见 test_17),
          就算 getattr 这一处退回去,那层兜底也会把 AttributeError 吞掉、响应照样发出。
          于是这里还要钉死**记账本身是成功的**——降级计数一次都不许涨。
          没有这一条,getattr 那条分支就是「测了个寂寞」。
        """
        address = self.serve()
        failures_before = TicketRequestHandler.log_failures
        # 三条出错路径里能从网络上直接打出来的两条:版本不认、起始行语法不对。
        cases = {
            b"GET /api/state HTTP/9.9\r\nHost: desk\r\n\r\n": b"505",       # Invalid HTTP version
            b"GET /api/state HTTP/x.y\r\nHost: desk\r\n\r\n": b"400",       # Bad request version
            b"GARBAGE\r\nHost: desk\r\n\r\n": b"400",                        # Bad request syntax
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                with socket.create_connection(address, timeout=10) as sock:
                    sock.sendall(line)
                    chunks = []
                    while True:
                        piece = sock.recv(65536)
                        if not piece:
                            break
                        chunks.append(piece)
                stream = b"".join(chunks)
                self.assertNotEqual(b"", stream, "客户端收到空响应 ⇒ log_message 又把响应吃掉了")
                # ★真判据是「期望的状态码回到了客户端」,不是「回来的一定是 HTTP/1.x 状态行」。
                #   请求行连版本都没解析出来时 self.request_version 还停在默认的 'HTTP/0.9',而
                #   send_response_only / send_header / end_headers 三处的第一行都是
                #       if self.request_version != 'HTTP/0.9':
                #   ⇒ 标准库**有意**只发裸正文、既不发状态行也不发响应头(HTTP/0.9 的形态)。
                #   哪几条出错路径落进哪个形态随标准库版本变:parse_request() 里
                #   `self.request_version = version` 相对三处 send_error 的位置改过,
                #   本机 3.14.7 三条都发状态行,服务器 3.14.4 三条都发裸正文。
                #   钉死输出格式 = 换一台机器就红(且红得像本仓的回归,其实不是)。
                if stream.startswith(b"HTTP/1."):
                    status_line = stream.split(b"\r\n", 1)[0]
                    self.assertIn(expected, status_line, f"状态行不对:{status_line!r}")
                else:
                    # HTTP/0.9 形态:回来的是标准库那张错误页,状态码写在 `<p>Error code: NNN</p>` 里。
                    # 先钉一句页面模板,免得将来标准库改了措辞、这条断言悄悄变成找不到的死断言。
                    self.assertIn("Error code: %(code)d", http.server.DEFAULT_ERROR_MESSAGE)
                    self.assertIn(
                        b"Error code: " + expected, stream,
                        f"裸正文里没有期望的状态码:{stream[:200]!r}",
                    )

        self.assertEqual(
            failures_before, TicketRequestHandler.log_failures,
            "响应是靠外层兜底救回来的,记账其实炸了 ⇒ self.path 那处没改对",
        )
        # 请求行都没解析出来的那一趟,path 记空串——记空是对的,抛异常不是。
        rows = self.service.store.read_jsonl(self.service.store.root / "server.log")
        self.assertTrue(rows, "坏请求一行日志都没落下")
        self.assertIn("", [row.get("path") for row in rows])

    def test_17_a_failing_access_log_never_swallows_the_response(self):
        """记一行访问日志失败,不该把一个本来能发出去的响应变成空响应。

        ★但也不许静默吞错:吞了要留计数 + 降级写 stderr。
        """
        self.dispatch("日志坏了也要回话")
        address = self.serve()
        before = TicketRequestHandler.log_failures
        stderr = io.StringIO()
        original = store_module.TicketStore.append_jsonl

        def exploding(path, value):
            if str(path).endswith("server.log"):
                raise OSError("磁盘满了")
            return original(path, value)

        with mock.patch.object(store_module.TicketStore, "append_jsonl", staticmethod(exploding)), \
                contextlib.redirect_stderr(stderr):
            response, raw = self.fetch(self.connect(address), "/api/tickets")
            self.assertEqual(200, response.status)
            self.assertTrue(json.loads(raw.decode("utf-8"))["ok"])

        self.assertGreater(TicketRequestHandler.log_failures, before, "吞了错却没留计数")
        self.assertIn("访问日志写入失败", stderr.getvalue())
        self.assertIn("磁盘满", stderr.getvalue())

    def test_18_the_access_log_still_records_the_path_on_a_normal_request(self):
        """兜住异常不等于把记账停掉——正常那一趟照样要落一行,path 照样要记对。"""
        self.dispatch("正常记账")
        address = self.serve()
        self.fetch(self.connect(address), "/api/tickets?state=%E6%96%B0%E5%BB%BA")
        rows = self.service.store.read_jsonl(self.service.store.root / "server.log")
        paths = [row.get("path") for row in rows]
        self.assertIn("/api/tickets", paths, f"访问日志没记下这一趟:{rows[-3:]}")
        self.assertNotIn("state=", " ".join(p for p in paths if p), "查询串不该进日志")


def _strip_html_comments(html: str) -> str:
    """把 HTML 注释摘掉再比对。

    ★文本闸连注释也拦:上面几条要证明「页面不再请求某个文件」,
      而解释「为什么不再请求它」的注释里必然写着那个文件名。不摘掉就是自己撞自己的闸。
    """
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


if __name__ == "__main__":
    unittest.main()
