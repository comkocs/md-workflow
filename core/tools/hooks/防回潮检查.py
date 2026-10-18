"""写后只读检查作废词、版本号、体积预算与章程缺节，只提示不拦。"""

import fnmatch
import json
from pathlib import Path, PurePosixPath
import re
import sys


产品目录 = Path(__file__).resolve().parents[3]
列条上限 = 10


def 办公目录():
    配置 = json.loads((产品目录 / "core/desk_config.json").read_text(encoding="utf-8-sig"))
    路径 = Path(配置["办公目录"].replace("\\", "/"))
    return (路径 if 路径.is_absolute() else 产品目录 / 路径).resolve()


def 预算命中(路径, 原文, 字节数):
    表 = 产品目录 / "desk/预算表.md"
    if not 表.is_file():
        return []
    字段 = None
    命中 = []
    for 行 in 表.read_text(encoding="utf-8-sig").splitlines():
        if not 行.startswith("|"):
            continue
        格 = [项.strip() for 项 in 行.strip().strip("|").split("|")]
        if all(名 in 格 for 名 in ("件", "量法", "预算")):
            字段 = {名: 格.index(名) for 名 in ("件", "量法", "预算")}
            continue
        if 字段 is None or len(格) <= max(字段.values()):
            continue
        件, 量法, 预算 = (格[字段[名]] for 名 in ("件", "量法", "预算"))
        if "不设上限" in 预算 or 量法 not in ("字节", "字符"):
            continue
        数值 = re.search(r"\d[\d,]*", 预算)
        if 数值 is None:
            continue
        模式列表 = re.findall(r"`([^`]+)`", 件) or [件]
        for 模式 in 模式列表:
            模式 = 模式.replace("\\", "/")
            文件名 = PurePosixPath(模式).name
            if not fnmatch.fnmatchcase(路径.name, 文件名):
                continue
            if any(符 in 模式 for 符 in "*?[") and "/" in 模式 and not 路径.match(模式):
                continue
            上限 = int(数值.group().replace(",", ""))
            实量 = 字节数 if 量法 == "字节" else len(原文)
            if 实量 > 上限:
                命中.append("体积超预算：{} {} / 预算 {} {}".format(实量, 量法, 上限, 量法))
            break
    return 命中


def 检查(路径, 办公根):
    数据 = 路径.read_bytes()
    原文 = 数据.decode("utf-8-sig")
    命中 = []
    词表 = 产品目录 / "desk/作废词表.txt"
    if 词表.is_file():
        词 = {行.strip() for 行 in 词表.read_text(encoding="utf-8-sig").splitlines()
             if 行.strip() and not 行.lstrip().startswith("#")}
        if 词:
            模式 = re.compile("|".join(re.escape(项) for 项 in sorted(词, key=lambda 项: (-len(项), 项))))
            for 行号, 行 in enumerate(原文.splitlines(), 1):
                for 匹配 in 模式.finditer(行):
                    命中.append("第 {} 行：作废词「{}」".format(行号, 匹配.group()))
                    if len(命中) == 列条上限:
                        return 命中
    键 = "/" + 路径.as_posix().casefold().lstrip("/")
    件类 = 路径.name == "章程.md" or any(段 in 键 for 段 in (
        "/.claude/agents/", "/desk/子代理/", "/desk/模板/", "/desk/角色/"))
    if 件类 and 路径.name not in ("宪法.md", "宪法_历史附录.md"):
        for 行号, 行 in enumerate(原文.splitlines(), 1):
            for 匹配 in re.finditer(r"v[0-9]+\.[0-9]+", 行):
                命中.append("第 {} 行：写死版本号「{}」".format(行号, 匹配.group()))
                if len(命中) == 列条上限:
                    return 命中
    命中.extend(预算命中(路径, 原文, len(数据)))
    try:
        相对 = 路径.relative_to(办公根)
    except ValueError:
        相对 = None
    if 相对 is not None and len(相对.parts) == 2 and 路径.name == "章程.md":
        缺节 = [节 for 节 in ("本位管什么", "真源表", "开窗步骤", "规矩")
              if not re.search(r"^#{1,6}\s+(?:[一二三四0-9]+[、.．]\s*)?" + 节, 原文, re.M)]
        if 缺节:
            命中.append("章程四节骨架缺节：" + " / ".join(缺节))
    return 命中[:列条上限]


def 主程序():
    事件 = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    if not isinstance(事件, dict) or 事件.get("tool_name") not in (None, "Write", "Edit", "MultiEdit"):
        return
    输入 = 事件.get("tool_input")
    if not isinstance(输入, dict) or not isinstance(输入.get("file_path"), str):
        return
    路径 = Path(输入["file_path"].replace("\\", "/"))
    if 路径.suffix.casefold() != ".md":
        return
    if not 路径.is_absolute():
        cwd = 事件.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return
        路径 = Path(cwd.replace("\\", "/")) / 路径
    路径 = 路径.resolve()
    if not 路径.is_file():
        return
    命中 = 检查(路径, 办公目录())
    if 命中:
        上下文 = "【防回潮提示，只提示不拦】\n" + "\n".join("- " + 项 for 项 in 命中)
        输出 = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": 上下文}}
        sys.stdout.buffer.write((json.dumps(输出, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    try:
        主程序()
        sys.stdout.flush()
    except BaseException:
        sys.stdout = None
    sys.exit(0)
