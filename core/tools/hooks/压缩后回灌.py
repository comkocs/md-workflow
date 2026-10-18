"""压缩后只读回灌本位现状、当日口径与近期仍在跑的子代理。"""

import datetime as dt
import json
from pathlib import Path
import re
import sys
import time


产品目录 = Path(__file__).resolve().parents[3]
结束状态 = {"completed", "complete", "done", "success", "failed", "error", "killed",
        "cancelled", "canceled", "stopped", "interrupted"}
标题 = ("一、本位接管件 §一", "二、当日口径", "三、本窗在跑子代理清单")
段上限 = (12000, 10000, 7000)


def 读文本(路径):
    try:
        return 路径.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""


def 读对象(原文):
    try:
        值 = json.loads(原文)
        return 值 if isinstance(值, dict) else {}
    except (ValueError, TypeError):
        return {}


def 办公目录():
    配置 = json.loads((产品目录 / "core/desk_config.json").read_text(encoding="utf-8-sig"))
    路径 = Path(配置["办公目录"].replace("\\", "/"))
    return (路径 if 路径.is_absolute() else 产品目录 / 路径).resolve()


def 会话路径(事件):
    值 = 事件.get("transcript_path")
    if isinstance(值, str) and 值:
        路径 = Path(值.replace("\\", "/"))
        if not 路径.is_absolute():
            路径 = Path(事件.get("cwd") or ".") / 路径
        if 路径.is_file():
            return 路径
    编号 = 事件.get("session_id")
    if isinstance(编号, str) and re.fullmatch(r"[A-Za-z0-9_-]+", 编号):
        return next((Path.home() / ".claude/projects").glob("*/" + 编号 + ".jsonl"), None)
    return None


def 正文(记录):
    消息 = 记录.get("message")
    内容 = 消息.get("content", "") if isinstance(消息, dict) else ""
    if isinstance(内容, str):
        return 内容
    return "\n".join(项["text"] for 项 in 内容 if isinstance(项, dict)
                     and isinstance(项.get("text"), str)) if isinstance(内容, list) else ""


def 解析主窗(路径, 截止):
    首句, 已取, 调用, 已结束 = "", False, {}, set()
    if 路径 is None:
        return 首句, 调用, 已结束
    with 路径.open("rb") as 源:
        剩余 = 路径.stat().st_size
        while 剩余 > 0 and time.monotonic() < 截止:
            行 = 源.readline(剩余)
            剩余 -= len(行)
            if not 行:
                break
            记录 = 读对象(行)
            文本 = 正文(记录)
            if 记录.get("type") == "user" and not 记录.get("isMeta") and not 已取:
                首句, 已取 = 文本, True
            消息 = 记录.get("message")
            内容 = 消息.get("content", []) if isinstance(消息, dict) else []
            for 项 in 内容 if isinstance(内容, list) else []:
                if not isinstance(项, dict):
                    continue
                输入 = 项.get("input")
                if 项.get("type") == "tool_use" and 项.get("name") in ("Agent", "Task") and isinstance(输入, dict):
                    调用[项.get("id")] = dict(编号=输入.get("resume"), 描述=输入.get("description"),
                                              类型=输入.get("subagent_type"), 模型=输入.get("model"),
                                              启动=记录.get("timestamp"), 后台=输入.get("run_in_background"))
                if 项.get("type") != "tool_result":
                    continue
                结果 = 记录.get("toolUseResult") or {}
                if not isinstance(结果, dict):
                    continue
                tid = 项.get("tool_use_id")
                if tid in 调用:
                    代理 = 调用[tid]
                    代理["编号"] = 结果.get("agentId") or 代理["编号"]
                    代理["模型"] = 结果.get("resolvedModel") or 代理["模型"]
                    代理["后台"] = 结果.get("isAsync") or 代理["后台"]
                    if 项.get("is_error") or 结果.get("status") in 结束状态 or not 代理["后台"]:
                        已结束.update(值 for 值 in (tid, 代理["编号"]) if 值)
                任务 = 结果.get("task")
                if isinstance(任务, dict) and 任务.get("status") in 结束状态:
                    已结束.update(任务[键] for 键 in ("agentId", "id", "task_id", "toolUseId")
                                 if isinstance(任务.get(键), str))
            if 记录.get("type") == "user":
                for 通知 in re.findall(r"<task-notification>(.*?)</task-notification>", 文本, re.S):
                    状态 = re.search(r"<status>\s*([^<]+)\s*</status>", 通知)
                    if 状态 and 状态.group(1).strip().lower() in 结束状态:
                        已结束.update(re.findall(r"<(?:task-id|agent-id|tool-use-id)>\s*([^<\s]+)", 通知))
    return 首句, 调用, 已结束


def 所属位(首句, 办公根):
    模式 = re.escape(办公根.as_posix().rstrip("/")) + r"/([^/\s<>\"']+)/(?:章程|接管)"
    匹配 = re.search(模式, 首句.replace("\\", "/"), re.I)
    if 匹配 and 匹配.group(1) not in (".", ".."):
        return 匹配.group(1)
    return None


def 接管段(办公根, 位):
    路径 = 办公根 / 位 / "接管_现状.md"
    if not 路径.is_file():
        return "本位无现状件"
    匹配 = re.search(r"^##[ \t]+§?一[、.．\s][^\r\n]*(?:\r?\n|$).*?(?=^##\s|\Z)",
                   读文本(路径), re.M | re.S)
    return 匹配.group().rstrip() if 匹配 else "本位现状件无 §一"


def 时戳(值):
    try:
        时刻 = dt.datetime.fromisoformat(值.replace("Z", "+00:00"))
        return 时刻.timestamp() if 时刻.tzinfo is not None else None
    except (AttributeError, ValueError, OverflowError):
        return None


def 子窗尾态(路径):
    模型, 结束, 更新 = None, False, None
    try:
        更新 = 路径.stat().st_mtime
        with 路径.open("rb") as 源:
            源.seek(max(0, 路径.stat().st_size - 262144))
            if 源.tell():
                源.readline()
            for 行 in 源:
                记录 = 读对象(行)
                消息 = 记录.get("message")
                if 记录.get("type") == "assistant" and isinstance(消息, dict):
                    模型 = 消息.get("model") or 模型
                    结束 = 消息.get("stop_reason") == "end_turn"
                if 记录.get("type") == "result" and 记录.get("subtype") in 结束状态:
                    结束 = True
    except OSError:
        pass
    return 模型, 结束, 更新


def 单行(值):
    return " ".join(str(值 or "未知").split())[:400]


def 子代理清单(路径, 调用, 已结束, 截止):
    if 路径 is None:
        return "未找到本窗会话记录，无法判定在跑子代理"
    目录 = 路径.with_suffix("") / "subagents"
    代理表 = {代理.get("编号") or tid: dict(代理, 调用编号=tid)
           for tid, 代理 in 调用.items() if 代理.get("后台")}
    for 元路径 in 目录.glob("agent-*.meta.json"):
        if time.monotonic() >= 截止:
            break
        编号 = 元路径.name[6:-10]
        元 = 读对象(读文本(元路径))
        tid = 元.get("toolUseId")
        旧 = 代理表.pop(tid, {}) if tid != 编号 else {}
        代理 = 代理表.setdefault(编号, 旧)
        for 键, 元键 in (("描述", "description"), ("类型", "agentType"), ("模型", "model")):
            代理[键] = 代理.get(键) or 元.get(元键)
        代理["模型"] = 元.get("resolvedModel") or 代理.get("模型")
        代理["调用编号"] = tid or 代理.get("调用编号")
        代理["更新"] = 元路径.stat().st_mtime
        代理["结束"] = 元.get("status") in 结束状态
    for 子路径 in 目录.glob("agent-*.jsonl"):
        if time.monotonic() >= 截止:
            break
        代理表.setdefault(子路径.stem[6:], {})
    行 = []
    现在 = time.time()
    for 编号, 代理 in 代理表.items():
        if time.monotonic() >= 截止:
            break
        if not isinstance(编号, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", 编号):
            continue
        if 编号 in 已结束 or 代理.get("调用编号") in 已结束 or 代理.get("结束"):
            continue
        模型, 结束, 更新 = 子窗尾态(目录 / ("agent-" + 编号 + ".jsonl"))
        更新项 = [值 for 值 in (更新, 代理.get("更新"), 时戳(代理.get("启动"))) if 值 is not None]
        if 结束 or not 更新项 or not 0 <= 现在 - max(更新项) <= 120:
            continue
        行.append("- 编号：{} / 描述：{} / 类型：{} / 模型：{} / 在跑（120 秒内更新）".format(
            单行(编号), 单行(代理.get("描述")), 单行(代理.get("类型")), 单行(代理.get("模型") or 模型)))
    return "\n".join(行) or "未发现 120 秒内更新且未结束的子代理"


def 限长输出(段):
    输出 = "【压缩后回灌】\n"
    for 序号, 内容 in 段:
        上限 = 段上限[序号]
        if len(内容) > 上限:
            标记 = "\n[本段已截断]"
            内容 = 内容[:上限 - len(标记)] + 标记
        输出 += "\n【" + 标题[序号] + "】\n" + 内容 + "\n"
    return 输出[:30000]


def 主程序():
    事件 = 读对象(sys.stdin.buffer.read().decode("utf-8-sig"))
    办公根 = 办公目录()
    路径 = 会话路径(事件)
    截止 = time.monotonic() + 2
    首句, 调用, 已结束 = 解析主窗(路径, 截止)
    位 = 所属位(首句, 办公根)
    口径 = 读文本(办公根 / "当日口径.md") or "无当日口径"
    段 = [(1, 口径)]
    if 位:
        段 = [(0, 接管段(办公根, 位)), (1, 口径),
             (2, 子代理清单(路径, 调用, 已结束, 截止))]
    sys.stdout.buffer.write(限长输出(段).encode("utf-8"))


if __name__ == "__main__":
    try:
        主程序()
        sys.stdout.flush()
    except BaseException:
        sys.stdout = None
    sys.exit(0)
