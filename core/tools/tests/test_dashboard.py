"""运行看板随产品与缓存扫描的轻量验收用例。"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.tickets.tests import test_window_dir as window_dir


DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"


def test_dashboard_server_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(DASHBOARD / "server.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dashboard_server_isolated_mode_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", str(DASHBOARD / "server.py"), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dashboard_static_index_contains_title():
    index = DASHBOARD / "static" / "index.html"
    assert index.is_file()
    assert "运行看板" in index.read_text(encoding="utf-8")


@pytest.fixture
def dashboard_cache_repository(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    root = tmp_path / "core"
    root.mkdir()
    office = "/example/office"
    config = root / "desk_config.json"
    config.write_text(json.dumps({"办公目录": office}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / ".gitignore").write_text("core/tools/dashboard/数据缓存/\n", encoding="utf-8")
    cache = root / "tools" / "dashboard" / "数据缓存" / "索引_v7" / "会话.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"cwd": office}), encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "--", ".gitignore", "core/desk_config.json"],
        check=True,
    )
    monkeypatch.setattr(window_dir.existing, "ROOT", root)
    return tmp_path, cache


def test_dashboard_untracked_cache_does_not_fail_window_dir_guard(dashboard_cache_repository):
    repository, cache = dashboard_cache_repository
    assert subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "--quiet", "--", str(cache)],
    ).returncode == 0
    case = window_dir.WindowDirTests("test_6_the_directory_string_is_written_once_in_core")
    case.test_6_the_directory_string_is_written_once_in_core()


def test_dashboard_tracked_cache_still_fails_window_dir_guard(dashboard_cache_repository):
    repository, cache = dashboard_cache_repository
    subprocess.run(["git", "-C", str(repository), "add", "-f", "--", str(cache)], check=True)
    case = window_dir.WindowDirTests("test_6_the_directory_string_is_written_once_in_core")
    with pytest.raises(AssertionError):
        case.test_6_the_directory_string_is_written_once_in_core()
