"""Bash 执行前只拦四种危险命令；不执行命令，不读写仓库。"""

import json
import re
import shlex
import sys


def 命令段(原文):
    词法 = shlex.shlex(原文, posix=True, punctuation_chars=";|&()\n")
    词法.whitespace = " \t\r"
    词法.whitespace_split = True
    段 = []
    for 词 in 词法:
        if re.fullmatch(r"[;|&()\n]+", 词):
            if 段:
                yield 段
                段 = []
        else:
            段.append(词)
    if 段:
        yield 段


def 去赋值(词):
    while 词 and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", 词[0], re.S):
        词 = 词[1:]
    return 词


def git子命令(词):
    索引 = 1
    while 索引 < len(词):
        项 = 词[索引]
        if 项 in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            索引 += 2
        elif 项.startswith("-"):
            索引 += 1
        else:
            return 索引
    return 索引


def 推到main(参数):
    位置 = []
    索引 = 0
    while 索引 < len(参数):
        项 = 参数[索引]
        if 项 in ("--repo", "--receive-pack", "--exec", "-o", "--push-option"):
            索引 += 2
            continue
        if not 项.startswith("-"):
            位置.append(项)
        索引 += 1
    # 首个位置参数为远端；--repo 形式已显式指定远端。
    引用 = 位置 if any(项 == "--repo" or 项.startswith("--repo=") for 项 in 参数) else 位置[1:]
    return any(项.rsplit(":", 1)[-1] in ("main", "refs/heads/main") for 项 in 引用)


def 替代命令(词):
    词 = 去赋值(词)
    if not 词:
        return None
    if 词[0] == "rm":
        递归 = 强制 = False
        for 项 in 词[1:]:
            if 项 == "--":
                break
            if 项.startswith("--"):
                递归 |= 项 == "--recursive"
                强制 |= 项 == "--force"
            elif 项.startswith("-"):
                递归 |= "r" in 项[1:] or "R" in 项[1:]
                强制 |= "f" in 项[1:]
        if 递归 and 强制:
            return "ls -ld -- ."
    if 词[0] != "git":
        return None
    索引 = git子命令(词)
    参数 = 词[索引:]
    if not 参数:
        return None
    if 参数[0] == "reset" and "--hard" in 参数[1:]:
        return shlex.join(词[:索引] + ["status"])
    if 参数[0] == "push" and any(项 in ("--force", "-f") for 项 in 参数[1:]) and 推到main(参数[1:]):
        return shlex.join([项 for 项 in 词 if 项 not in ("--force", "-f")])
    if 参数[:2] == ["worktree", "remove"] and "--force" in 参数[2:]:
        return shlex.join([项 for 项 in 词 if 项 != "--force"])
    return None


def 主程序():
    事件 = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    if not isinstance(事件, dict) or 事件.get("tool_name") != "Bash":
        return
    输入 = 事件.get("tool_input")
    if not isinstance(输入, dict) or not isinstance(输入.get("command"), str):
        return
    # 先完整解析；引号残缺等坏输入一律静默放行。
    各段 = list(命令段(输入["command"]))
    for 段 in 各段:
        替代 = 替代命令(段)
        if 替代 is not None:
            if "\n" in 替代 or "\r" in 替代:
                替代 = "git status"
            输出 = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "替代:" + 替代}}
            sys.stdout.buffer.write((json.dumps(输出, ensure_ascii=False) + "\n").encode("utf-8"))
            return


if __name__ == "__main__":
    try:
        主程序()
        sys.stdout.flush()
    except (ValueError, TypeError, OSError):
        pass
    sys.exit(0)
