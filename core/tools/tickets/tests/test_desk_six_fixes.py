"""工单台六处小修:临时配置、文件锁与真实 HTTP 行为回归。"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools.tickets import config, model
from tools.tickets import store as store_module
from tools.tickets.model import TicketError
from tools.tickets.store import TicketStore
from . import test_ticket_system as existing


@pytest.fixture
def file_store(tmp_path):
    store = TicketStore(tmp_path / "tickets")
    store.ensure()
    return store


def write_lock(store, pid, stamp=None):
    stamp = stamp or model.now_text()
    store.lock_path.write_text(f"pid={pid} time={stamp}", encoding="utf-8")


def test_dead_pid_lock_allows_write_before_timeout(file_store):
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait(timeout=5)
    write_lock(file_store, exited.pid)
    with file_store.locked(timeout_seconds=0.2):
        file_store.atomic_json(file_store.counter_path, {"最后编号": 7})
    assert file_store.read_json(file_store.counter_path) == {"最后编号": 7}
    assert not file_store.lock_path.exists()


def test_live_fresh_pid_lock_still_blocks_write(file_store):
    write_lock(file_store, os.getpid())
    before = file_store.lock_path.read_bytes()
    with pytest.raises(TicketError, match="工单盘正被另一扇窗写入"):
        with file_store.locked(timeout_seconds=0.1):
            file_store.atomic_json(file_store.counter_path, {"最后编号": 7})
    assert file_store.read_json(file_store.counter_path) == {"最后编号": 0}
    assert file_store.lock_path.read_bytes() == before


def test_live_aged_pid_lock_is_recovered_by_age(file_store):
    stamp = (datetime.now().astimezone() - timedelta(seconds=3601)).isoformat()
    write_lock(file_store, os.getpid(), stamp)
    with file_store.locked(timeout_seconds=0.2):
        file_store.atomic_json(file_store.counter_path, {"最后编号": 8})
    assert file_store.read_json(file_store.counter_path) == {"最后编号": 8}


def test_actual_live_fresh_holder_blocks_another_writer(file_store):
    other = TicketStore(file_store.root)
    with file_store.locked():
        with pytest.raises(TicketError, match="工单盘正被另一扇窗写入"):
            with other.locked(timeout_seconds=0.1):
                other.atomic_json(other.counter_path, {"最后编号": 9})
        assert file_store.lock_path.exists()
    assert file_store.read_json(file_store.counter_path) == {"最后编号": 0}


def test_actual_aged_holder_is_recovered_and_cannot_delete_successor(file_store, monkeypatch):
    stamp = (datetime.now().astimezone() - timedelta(seconds=3601)).isoformat()
    first = file_store.locked()
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "now_text", lambda: stamp)
        first.__enter__()
    first_released = False
    try:
        other = TicketStore(file_store.root)
        with other.locked(timeout_seconds=0.2):
            successor = other.lock_path.read_bytes()
            first.__exit__(None, None, None)
            first_released = True
            assert other.lock_path.read_bytes() == successor
            other.atomic_json(other.counter_path, {"最后编号": 9})
        assert not other.lock_path.exists()
        assert other.read_json(other.counter_path) == {"最后编号": 9}
    finally:
        if not first_released:
            first.__exit__(None, None, None)


def test_successful_login_clears_same_ip_401_bucket():
    case = existing.AuthHttpTests()
    case.setUp()
    try:
        password = secrets.token_urlsafe(24)
        assert case.request("POST", "/auth/setup", {"username": case.username, "password": password})[0] == 200
        for _ in range(20):
            assert case.request("GET", "/api/tickets")[0] == 401
        assert case.request("GET", "/api/tickets")[0] == 429
        status, headers, _ = case.request("POST", "/auth/login", {"username": case.username, "password": password})
        assert status == 200
        session = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        assert case.request("GET", "/api/tickets", headers=session)[0] == 200
        image = case.store.images_dir / "login.png"
        image.write_bytes(b"test-image")
        assert case.request("GET", "/img/login.png", headers=session)[0] == 200
    finally:
        case.tearDown()


def run_cli(tmp_path, arguments, *, configuration=None, utf8_disabled=False):
    env = existing.clean_environment(tmp_path / "data")
    if configuration:
        env["TICKET_DESK_CONFIG"] = str(configuration)
    if utf8_disabled:
        env["PYTHONUTF8"] = "0"
        env.pop("PYTHONIOENCODING", None)
    return subprocess.run([*existing.CLI, "--local", *arguments], cwd=existing.ROOT, env=env,
                          capture_output=True, timeout=15)


@pytest.fixture
def configuration(tmp_path):
    path = tmp_path / "desk_config.json"
    path.write_bytes(Path(config.PATH).read_bytes())
    return path


@pytest.mark.parametrize("options,expected", [
    ([], {"名字": "测试新增位"}),
    (["--relay", "--scope", "测试事务"], {"名字": "测试新增位", "只发需求": True, "对口": "测试事务"}),
])
def test_slot_add_validates_and_writes_only_existing_fields(tmp_path, configuration, options, expected):
    before = config.load_config(configuration)
    result = run_cli(tmp_path, ["slot-add", "测试新增位", *options], configuration=configuration)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert "已加位：测试新增位" in result.stdout.decode("utf-8")
    after = config.load_config(configuration)
    assert after["位表"] == before["位表"] + [expected]
    assert set(after["位表"][-1]) <= {"名字", "角色", "只发需求", "对口"}
    assert {k: v for k, v in after.items() if k != "位表"} == {k: v for k, v in before.items() if k != "位表"}
    assert not (tmp_path / "data").exists()


def test_slot_add_duplicate_is_refused_without_modifying_config(tmp_path, configuration):
    name = config.load_config(configuration)["位表"][0]["名字"]
    before = configuration.read_bytes()
    result = run_cli(tmp_path, ["slot-add", name], configuration=configuration)
    assert result.returncode != 0
    assert f"位「{name}」已存在，不能重复添加。" in result.stderr.decode("utf-8")
    assert configuration.read_bytes() == before


@pytest.mark.parametrize("arguments", [
    ["slot-add", ""], ["slot-add", "坏位-01"],
    ["slot-add", "新特殊位", "--role", config.ROLE_PLATFORM],
    ["slot-add", "缺对口位", "--relay"],
])
def test_slot_add_invalid_candidate_leaves_config_untouched(tmp_path, configuration, arguments):
    before = configuration.read_bytes()
    result = run_cli(tmp_path, arguments, configuration=configuration)
    assert result.returncode != 0
    assert "拦下:" in result.stderr.decode("utf-8")
    assert configuration.read_bytes() == before


def test_direct_cli_outputs_utf8_when_python_utf8_is_disabled(tmp_path):
    created = run_cli(tmp_path, ["new", "--type", "疑问", "--slot", existing.SLOT,
                               "--title", "中文回显验证", "--body", existing.VALID_DECISION_BODY, "--json"], utf8_disabled=True)
    assert created.returncode == 0, created.stderr.decode("utf-8")
    text = created.stdout.decode("utf-8")
    assert "中文回显验证" in text
    ticket_id = json.loads(text)["result"]["编号"]
    written = run_cli(tmp_path, ["say", "--by", existing.SLOT, "--slot", existing.SLOT,
                               "中文写入验证", "--ref", ticket_id], utf8_disabled=True)
    assert written.returncode == 0, written.stderr.decode("utf-8")
    assert "已写入" in written.stdout.decode("utf-8")
    denied = run_cli(tmp_path, ["show", "T-999999"], utf8_disabled=True)
    assert denied.returncode != 0
    assert "拦下:本机模式" in denied.stderr.decode("utf-8")


def fold_fixture(tmp_path):
    path = tmp_path / "MEMORY.md"
    content = "# 测试索引\n\n## 长节\n" + "".join(f"- 条目 {i} " + "内容" * 80 + "\n" for i in range(180))
    path.write_bytes(b"\xef\xbb\xbf" + content.replace("\n", "\r\n").encode("utf-8"))
    return path


def run_fold(path, *options):
    script = existing.ROOT.parent / "desk" / "memory" / "fold_index.py"
    return subprocess.run([sys.executable, str(script), str(path), "--auto", "--limit", "24000", *options],
                          capture_output=True, timeout=15)


def test_fold_normalizes_bom_crlf_in_dry_run_without_writing(tmp_path):
    index = fold_fixture(tmp_path)
    before = index.read_bytes()
    result = run_fold(index, "--dry-run")
    assert result.returncode == 0, result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert index.read_bytes() == before
    assert list(tmp_path.iterdir()) == [index]


def test_fold_outputs_utf8_without_bom_or_crlf(tmp_path):
    index = fold_fixture(tmp_path)
    result = run_fold(index)
    assert result.returncode == 0, result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 2
    for path in files:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw
        raw.decode("utf-8")
