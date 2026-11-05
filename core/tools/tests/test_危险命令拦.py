"""仅把命令作为 JSON 输入，绝不实际执行危险命令。"""

import json
from pathlib import Path
import subprocess
import sys

import pytest


脚本 = Path(__file__).resolve().parents[1] / "hooks/危险命令拦.py"


def 运行(输入):
    原文 = 输入 if isinstance(输入, str) else json.dumps(输入, ensure_ascii=False)
    结果 = subprocess.run([sys.executable, "-X", "utf8", "-I", str(脚本)],
                        input=原文.encode("utf-8"), capture_output=True, timeout=10)
    assert 结果.returncode == 0
    assert 结果.stderr == b""
    return 结果.stdout.decode("utf-8")


@pytest.mark.parametrize("命令,替代", [
    ("rm -rf _work/练习", "ls -ld -- ."),
    ("git reset --hard HEAD", "git status"),
    ("git push --force origin main", "git push origin main"),
    ("git push -f origin HEAD:refs/heads/main", "git push origin HEAD:refs/heads/main"),
    ("git worktree remove --force _work/练习", "git worktree remove '_work/练习'"),
    ("git -C '路径 含空格' reset --hard", "git -C '路径 含空格' status"),
    ("git status && rm -r -f _work/练习", "ls -ld -- ."),
    ("git status\ngit reset --hard", "git status"),
])
def test_四类危险命令拒绝且返回替代(命令, 替代):
    输出 = 运行({"tool_name": "Bash", "tool_input": {"command": 命令}})
    assert len(输出.splitlines()) == 1
    判定 = json.loads(输出)["hookSpecificOutput"]
    assert 判定["hookEventName"] == "PreToolUse"
    assert 判定["permissionDecision"] == "deny"
    assert 判定["permissionDecisionReason"] == "替代:" + 替代
    assert len(判定["permissionDecisionReason"].splitlines()) == 1


@pytest.mark.parametrize("命令", [
    "git status", "rm -r _work/练习", "rm -f 文件", "git reset --soft HEAD",
    "git push --force origin topic", "git push --force-with-lease origin main",
    "git push origin main", "git worktree remove _work/练习",
    "echo 'rm -rf _work/练习'", "git commit -m 'git reset --hard'",
])
def test_其余命令静默放行(命令):
    assert 运行({"tool_name": "Bash", "tool_input": {"command": 命令}}) == ""


@pytest.mark.parametrize("输入", [
    "{", "[]", "null", {}, {"tool_name": "Bash", "tool_input": None},
    {"tool_name": "Bash", "tool_input": {"command": 1}},
    {"tool_name": "Bash", "tool_input": {"command": "rm -rf '"}},
    {"tool_name": "Write", "tool_input": {"command": "rm -rf 练习"}},
])
def test_坏输入及其他工具静默放行(输入):
    assert 运行(输入) == ""
