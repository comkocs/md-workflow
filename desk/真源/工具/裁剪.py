"""按需求卡裁剪通用条目，只依赖 Python 3.11+ 标准库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path


包目录 = Path(__file__).resolve().parent.parent
模块表 = (
    "底线", "决策与原话", "真源与完成", "分工与权责", "留痕与通道",
    "任务书与派遣", "审计与验收", "规矩寿命", "记忆与接棒", "跨窗协作",
    "代码与仓库", "发布与上线", "素材与命名", "负载与性能", "审查席", "真源库",
)
层表 = ("宪法", "作业", "做法")
规模表 = ("小", "中", "大")
特征表 = ("代码", "上线", "素材", "数值", "多模型", "审查岗", "规矩库")
占位缺省 = {
    "项目名": "新项目", "决策者": "决策者", "总协调": "总协调",
    "受众": "用户", "成品": "交付成果", "真源件": "项目真源.md",
    "留痕处": "记录.md", "口径件": "当日口径.md", "规矩总件": "宪法.md",
    "作业件": "作业规矩.md", "接管件": "接棒记录.md",
}
占位式 = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
编号式 = re.compile(r"G-[0-9]{4}\Z")
声明 = "本件由裁剪生成,要改先改需求卡或条目表再重裁"
自定样板 = (
    "# 本项目自定\n\n"
    "每条一行：要求 —— 出处:资料/决策者原话.md 第 N 条\n"
    "与通用条冲突时以本件为准，并在该条后写「压 G-xxxx」。\n"
)
输出名 = (
    "宪法.md", "作业规矩.md", "做法建议.md", "当日口径.md",
    "开窗必读.md", "拆掉清单.md", "裁剪记录.json", "本项目自定_样板.md",
)


class 输入错(ValueError):
    """可直接向使用者说明的输入错误。"""


def 未知占位(文本: str) -> list[str]:
    return [名称 for 名称 in 占位式.findall(文本) if 名称 not in 占位缺省]


def 所有字符串(值):
    if isinstance(值, str):
        yield 值
    elif isinstance(值, dict):
        for 子值 in 值.values():
            yield from 所有字符串(子值)
    elif isinstance(值, list):
        for 子值 in 值:
            yield from 所有字符串(子值)


def 非空文(值) -> bool:
    return isinstance(值, str) and bool(值.strip())


def 校验条目(条目: list) -> list[str]:
    错误 = []
    已见 = set()
    必填 = ("编号", "模块", "层", "短题", "条文", "为什么", "最小规模", "需要特征", "依赖", "源条数")
    for 行号, 条 in enumerate(条目, 1):
        if not isinstance(条, dict):
            错误.append(f"第 {行号} 条：必须是对象")
            continue
        标识 = f"第 {行号} 条（{条.get('编号', '无编号')}）"
        for 字段 in 必填:
            if 字段 not in 条:
                错误.append(f"{标识}：缺必填 {字段}")
        编号 = 条.get("编号")
        if not isinstance(编号, str) or not 编号式.fullmatch(编号) or 编号 == "G-0000":
            错误.append(f"{标识}：编号须为 G-四位数字，从 G-0001 起")
        elif 编号 in 已见:
            错误.append(f"{标识}：编号重复 {编号}")
        else:
            已见.add(编号)
        for 字段, 枚举 in (("模块", 模块表), ("层", 层表), ("最小规模", 规模表), ("最大规模", 规模表)):
            值 = 条.get(字段, "大" if 字段 == "最大规模" else None)
            if 值 not in 枚举:
                错误.append(f"{标识}：{字段} 不在定表")
        for 字段, 上限 in (("短题", 20), ("条文", None), ("为什么", 120)):
            值 = 条.get(字段)
            if not 非空文(值):
                错误.append(f"{标识}：{字段} 须为非空文字")
            elif 上限 and len(值) > 上限:
                错误.append(f"{标识}：{字段} 超过 {上限} 字")
        最小, 最大 = 条.get("最小规模"), 条.get("最大规模", "大")
        if 最小 in 规模表 and 最大 in 规模表 and 规模表.index(最小) > 规模表.index(最大):
            错误.append(f"{标识}：最小规模大于最大规模")
        特征 = 条.get("需要特征")
        if not isinstance(特征, list) or any(值 not in 特征表 for 值 in 特征):
            错误.append(f"{标识}：需要特征须为定表内特征的列表")
        条件 = 条.get("加回条件", "")
        if not isinstance(条件, str):
            错误.append(f"{标识}：加回条件须为文字")
        elif (最小 in ("中", "大") or (isinstance(特征, list) and 特征)) and not 条件.strip():
            错误.append(f"{标识}：需要加回条件，不能为空")
        依赖 = 条.get("依赖")
        if not isinstance(依赖, list):
            错误.append(f"{标识}：依赖须为编号列表")
        else:
            for 目标 in 依赖:
                if not isinstance(目标, str) or not 编号式.fullmatch(目标) or 目标 == "G-0000":
                    错误.append(f"{标识}：依赖编号格式错误 {目标!r}")
                elif 目标 == 编号:
                    错误.append(f"{标识}：不得自依赖")
        源条数 = 条.get("源条数")
        if type(源条数) is not int or 源条数 < 1:
            错误.append(f"{标识}：源条数须为正整数")
        for 文本 in 所有字符串(条):
            for 名称 in 未知占位(文本):
                错误.append(f"{标识}：未知占位 {{{{{名称}}}}}")
    for 行号, 条 in enumerate(条目, 1):
        if isinstance(条, dict) and isinstance(条.get("依赖"), list):
            for 目标 in 条["依赖"]:
                if isinstance(目标, str) and 编号式.fullmatch(目标) and 目标 not in 已见:
                    错误.append(f"第 {行号} 条（{条.get('编号', '无编号')}）：依赖不存在 {目标}")
    return 错误


def 读条目(路径: Path) -> tuple[list[dict], str]:
    原始 = 路径.read_bytes()
    try:
        文本 = 原始.decode("utf-8")
    except UnicodeDecodeError as 错:
        raise 输入错("条目表须为 UTF-8 文本") from 错
    条目, 错误 = [], []
    for 行号, 行 in enumerate(文本.splitlines(), 1):
        if not 行.strip():
            continue
        try:
            条目.append(json.loads(行))
        except json.JSONDecodeError as 错:
            错误.append(f"条目表第 {行号} 行：JSON 格式错误（第 {错.colno} 列）")
    错误.extend(校验条目(条目))
    if 错误:
        raise 输入错("\n".join(错误))
    return 条目, hashlib.sha256(原始).hexdigest()


def 推算规模(同时窗数: int, 分工位数: int, 仓数: int, 上线: bool) -> str:
    if 同时窗数 >= 7 or 分工位数 >= 6 or (仓数 >= 2 and 上线 and 分工位数 >= 4):
        return "大"
    if 同时窗数 >= 4 or 分工位数 >= 2 or (仓数 >= 2 and 上线):
        return "中"
    return "小"


def 读卡(路径: Path) -> dict:
    原始 = 路径.read_bytes()
    try:
        卡 = tomllib.loads(原始.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as 错:
        raise 输入错(f"需求卡格式错误：{错}") from 错
    for 节 in ("项目", "称呼", "规模", "特征", "手调"):
        if 节 in 卡 and not isinstance(卡[节], dict):
            raise 输入错(f"需求卡 [{节}] 须为表")
    项目 = 卡.get("项目", {})
    for 字段 in ("名", "一句", "受众", "成品"):
        if not 非空文(项目.get(字段)):
            raise 输入错(f"需求卡 [项目] {字段} 须为非空文字")
    特征 = dict.fromkeys(特征表, False)
    for 名, 值 in 卡.get("特征", {}).items():
        if 名 not in 特征表 or type(值) is not bool:
            raise 输入错(f"需求卡 [特征] {名} 须为定表内的布尔开关")
        特征[名] = 值
    规模 = 卡.get("规模", {})
    数字 = {}
    for 名, 缺省, 下限 in (("同时窗数", 1, 1), ("分工位数", 1, 0), ("仓数", 0, 0)):
        值 = 规模.get(名, 缺省)
        if type(值) is not int or 值 < 下限:
            raise 输入错(f"需求卡 [规模] {名} 须为不小于 {下限} 的整数")
        数字[名] = 值
    推算 = 推算规模(**数字, 上线=特征["上线"])
    档 = 规模.get("档", "")
    if not isinstance(档, str) or 档 not in ("", *规模表):
        raise 输入错("需求卡 [规模] 档 须为空、小、中或大")
    if 档 and 档 != 推算:
        print(f"提醒：规模档为{档}，数字推算为{推算}；本次使用档{档}。", file=sys.stderr)
    称呼 = dict(占位缺省)
    称呼.update(项目名=项目["名"], 受众=项目["受众"], 成品=项目["成品"])
    for 名, 值 in 卡.get("称呼", {}).items():
        if 名 not in 占位缺省 or not isinstance(值, str):
            raise 输入错(f"需求卡 [称呼] {名} 须为定表内的文字")
        if 值.strip():
            称呼[名] = 值
    for 值 in (*项目.values(), *称呼.values()):
        if isinstance(值, str) and 占位式.search(值):
            raise 输入错("需求卡项目与称呼须填实际文字，不要再填 {{…}} 占位")
    手调 = 卡.get("手调", {})
    调整 = {}
    for 动作 in ("加", "减"):
        项 = 手调.get(动作, [])
        if not isinstance(项, list):
            raise 输入错(f"需求卡 [手调] {动作} 须为编号、理由组成的列表")
        for 一项 in 项:
            if not isinstance(一项, dict) or not 非空文(一项.get("编号")) or not 非空文(一项.get("理由")):
                raise 输入错(f"需求卡 [手调] {动作} 每项必须同时有编号与非空理由")
        调整[动作] = 项
    return {
        "项目": 项目, "称呼": 称呼, "规模": 档 or 推算, "数字": 数字,
        "特征": 特征, "手调": 调整, "sha256": hashlib.sha256(原始).hexdigest(),
    }


def 裁选(条目: list[dict], 卡: dict) -> tuple[list[dict], list[dict], dict[str, str]]:
    档 = 规模表.index(卡["规模"])
    保留 = set()
    拆因 = {}
    全编号 = {条["编号"] for 条 in 条目}
    for 条 in 条目:
        因 = []
        if 档 < 规模表.index(条["最小规模"]):
            因.append(f"规模低于{条['最小规模']}")
        if 档 > 规模表.index(条.get("最大规模", "大")):
            因.append(f"规模高于{条['最大规模']}")
        缺特征 = [名 for 名 in 条["需要特征"] if not 卡["特征"][名]]
        if 缺特征:
            因.append("未开特征：" + "、".join(缺特征))
        if 因:
            拆因[条["编号"]] = "；".join(因)
        else:
            保留.add(条["编号"])
    for 动作 in ("加", "减"):
        for 项 in 卡["手调"][动作]:
            编号 = 项["编号"]
            if 编号 not in 全编号:
                raise 输入错(f"手调{动作}的编号不存在：{编号}")
            if 动作 == "加":
                保留.add(编号)
                拆因.pop(编号, None)
            else:
                保留.discard(编号)
                拆因[编号] = "手调减：" + 项["理由"]
    错误 = []
    for 条 in 条目:
        if 条["编号"] in 保留:
            for 依赖 in 条["依赖"]:
                if 依赖 not in 保留:
                    错误.append(f"依赖被拆：{条['编号']}（{条['短题']}）需要 {依赖}；请改需求卡，不会自动补条")
    if 错误:
        raise 输入错("\n".join(错误))
    return (
        [条 for 条 in 条目 if 条["编号"] in 保留],
        [条 for 条 in 条目 if 条["编号"] not in 保留],
        拆因,
    )


def 替换(文本: str, 称呼: dict[str, str]) -> str:
    未知 = 未知占位(文本)
    if 未知:
        raise 输入错("未知占位：" + "、".join("{{" + 名 + "}}" for 名 in 未知))
    return 占位式.sub(lambda 匹配: 称呼[匹配.group(1)], 文本)


def 按模块(条目: list[dict]):
    for 模块 in 模块表:
        本组 = sorted((条 for 条 in 条目 if 条["模块"] == 模块), key=lambda 条: 条["编号"])
        if 本组:
            yield 模块, 本组


def 格式条文(条: dict, 称呼: dict[str, str]) -> str:
    正文 = 替换(条["条文"], 称呼)
    为什么 = 替换(条["为什么"], 称呼)
    return f"**{替换(条['短题'], 称呼)}**〔{条['编号']}〕{正文}\n> 为什么:{为什么}\n"


def 件头(题: str, 卡: dict, 表摘要: str) -> str:
    特征 = "、".join(名 for 名 in 特征表 if 卡["特征"][名]) or "无"
    return (
        f"# {题}\n\n项目：{卡['称呼']['项目名']}\n\n规模：{卡['规模']}；特征：{特征}\n\n"
        f"需求卡 sha256：`{卡['sha256']}`\n\n条目表 sha256：`{表摘要}`\n\n{声明}\n\n"
    )


def 加回说明(条: dict) -> str:
    if 条.get("加回条件", "").strip():
        return 条["加回条件"]
    if 条.get("最大规模", "大") != "大":
        return f"项目规模回到{条['最小规模']}至{条['最大规模']}时，改需求卡重裁。"
    return "重新需要这条时，在需求卡 [手调] 加中写编号与理由，再重裁。"


def 生成拆掉清单(拆掉: list[dict], 保留模块: list[str], 卡: dict, 表摘要: str) -> str:
    节表 = {
        "中": ("项目长到中档时加回", "同时窗 ≥4 或分工位 ≥2 或 仓 ≥2 且上线。"),
        "大": ("长到大档时加回", "同时窗 ≥7 或分工位 ≥6 或（仓 ≥2 且上线 且分工位 ≥4）。"),
        "代码": ("开始写程序(代码)时加回", "在需求卡中开启代码特征，再重裁。"),
        "上线": ("开始部署上线时加回", "在需求卡中开启上线特征，再重裁。"),
        "素材": ("开始批量做素材时加回", "在需求卡中开启素材特征，再重裁。"),
        "数值": ("出现须统一维护的规则或数值时加回", "在需求卡中开启数值特征，再重裁。"),
        "多模型": ("开始派多家模型时加回", "在需求卡中开启多模型特征，再重裁。"),
        "审查岗": ("决策者要设审查岗时加回", "决策者要设审查岗时加回"),
        "规矩库": ("决策者要建规矩库时加回", "决策者要建规矩库时加回"),
        "简化": ("本档已有完整版、不用的简化条", "项目规模回到条目适用范围时，改需求卡重裁。"),
        "手调": ("手调拆掉的条目", "重新需要时在需求卡中调整手调减，再重裁。"),
    }
    分组 = {名: [] for 名 in 节表}
    另需 = {}
    档 = 规模表.index(卡["规模"])
    手调减 = {项["编号"] for 项 in 卡["手调"]["减"]}
    for 条 in 拆掉:
        缺特征 = tuple(名 for 名 in 特征表 if 名 in 条["需要特征"] and not 卡["特征"][名])
        if 条["编号"] in 手调减:
            节, 缺特征 = "手调", ()
        elif 档 > 规模表.index(条.get("最大规模", "大")):
            节, 缺特征 = "简化", ()
        elif 档 < 规模表.index(条["最小规模"]):
            节 = 条["最小规模"]
        elif 缺特征:
            节, 缺特征 = 缺特征[0], 缺特征[1:]
        else:
            节 = "手调"
        分组[节].append(条)
        另需[条["编号"]] = 缺特征
    段 = [件头("拆掉清单", 卡, 表摘要) + "各组节首写加回门槛；条后仅括注与组门槛不同的加回条件。\n\n"]
    for 节, (题, 门槛) in 节表.items():
        if not 分组[节]:
            continue
        数目 = f"({len(分组[节])} 条)" if 节 in ("中", "大") else ""
        段.append(f"## {题}{数目}\n\n")
        段.append("门槛：" + 门槛 + "\n\n")
        for 模块, 本组 in 按模块(分组[节]):
            短题 = []
            for 条 in 本组:
                特征 = 另需[条["编号"]]
                补充 = f"（另需 {'、'.join(特征)}）" if 特征 else ""
                条件 = 替换(加回说明(条), 卡["称呼"]).strip()
                if 条件 != 门槛:
                    补充 += f"（加回条件：{条件}）"
                短题.append(f"{替换(条['短题'], 卡['称呼'])}〔{条['编号']}〕{补充}")
            内容 = "、".join(短题)
            段.append(f"{模块}:{内容}\n")
        段.append("\n")
    if not 拆掉:
        段.append("本次没有拆掉条目。\n")
    return "".join(段)


def 生成各件(条目: list[dict], 卡: dict, 表摘要: str) -> dict[str, str]:
    保留, 拆掉, 拆因 = 裁选(条目, 卡)
    称呼 = 卡["称呼"]
    结果 = {}
    for 层, 文件名 in zip(层表, ("宪法.md", "作业规矩.md", "做法建议.md")):
        段 = [件头(文件名[:-3], 卡, 表摘要)]
        if 文件名 == "宪法.md":
            段[0] += "本项目独有的要求见 `本项目自定.md`，与本件冲突以它为准。\n\n"
        for 模块, 本组 in 按模块([条 for 条 in 保留 if 条["层"] == 层]):
            段.append(f"## {模块}\n\n")
            段.extend(格式条文(条, 称呼) + "\n" for 条 in 本组)
        if len(段) == 1:
            段.append("本层无保留条目。\n")
        结果[文件名] = "".join(段)
    有口径 = any("{{口径件}}" in 条["条文"] for 条 in 保留)
    if 有口径:
        结果["当日口径.md"] = 件头("当日口径", 卡, 表摘要) + (
            "格式说明：每行写一项当前约定，填生效时刻、事项、当前说法、依据和何时失效；变更留旧记录。\n\n"
            "| 生效时刻 | 事项 | 当前说法 | 依据 | 何时失效 |\n"
            "| --- | --- | --- | --- | --- |\n"
        )
    保留模块 = [模块 for 模块, _ in 按模块(保留)]
    口径指针 = f"当日口径.md（称呼：{称呼['口径件']}）" if 有口径 else "本次未启用，不生成口径件。"
    结果["开窗必读.md"] = 件头("开窗必读", 卡, 表摘要) + (
        f"项目一句：{卡['项目']['一句']}\n\n决策者：{称呼['决策者']}\n\n"
        f"保留模块：{'、'.join(保留模块) or '无'}\n\n口径件指针：{口径指针}\n\n"
        "每窗先读本件，再依次读本项目自定.md（如有）、宪法.md、作业规矩.md；需要具体操作时读做法建议.md。\n"
        "了解删减与加回时读拆掉清单.md；实际保留编号和手调理由见裁剪记录.json。\n"
    )
    字节数 = len(结果["开窗必读.md"].encode("utf-8"))
    if 字节数 > 3072:
        raise 输入错(f"开窗必读.md 为 {字节数} B，超过 3,072 B；请缩短项目名、一句或称呼后重裁")
    结果["拆掉清单.md"] = 生成拆掉清单(拆掉, 保留模块, 卡, 表摘要)
    记录 = {
        "项目名": 称呼["项目名"], "规模": 卡["规模"], "规模数字": 卡["数字"],
        "特征": 卡["特征"], "需求卡sha256": 卡["sha256"], "条目表sha256": 表摘要,
        "说明": 声明, "保留": [条["编号"] for 条 in 保留],
        "拆掉": [条["编号"] for 条 in 拆掉], "保留模块": 保留模块,
        "拆因": 拆因, "手调": 卡["手调"],
        "加回条件": {条["编号"]: 替换(加回说明(条), 称呼) for 条 in 拆掉},
    }
    结果["裁剪记录.json"] = json.dumps(记录, ensure_ascii=False, indent=2) + "\n"
    return 结果


def 写各件(目录: Path, 各件: dict[str, str]) -> int:
    各件 = {名: 内容 for 名, 内容 in 各件.items() if 名 != "本项目自定.md"}
    if (目录 / "本项目自定.md").exists():
        各件.pop("本项目自定_样板.md", None)
    else:
        各件["本项目自定_样板.md"] = 自定样板
    已有 = [目录 / 名 for 名 in 输出名 if (目录 / 名).exists()
          and (名 != "本项目自定_样板.md" or 名 in 各件)]
    if any(not 路径.is_file() for 路径 in 已有):
        raise 输入错("输出件位置被同名目录占用，请另选输出目录")
    目录.mkdir(parents=True, exist_ok=True)
    if 已有:
        次 = 1
        while (目录 / "_旧版" / f"第{次}次").exists():
            次 += 1
        旧目录 = 目录 / "_旧版" / f"第{次}次"
        旧目录.mkdir(parents=True)
        for 路径 in 已有:
            shutil.move(str(路径), str(旧目录 / 路径.name))
    for 名, 内容 in 各件.items():
        (目录 / 名).write_text(内容, encoding="utf-8", newline="\n")
    return len(各件)


def 生成表(条目: list[dict], 摘要: str) -> str:
    段 = [f"# 条目表\n\n条目表 sha256：`{摘要}`\n\n本件由条目表.jsonl 生成，要改先改条目表再生成。\n\n"]
    for 模块, 本组 in 按模块(条目):
        段.append(f"## {模块}\n\n")
        for 条 in 本组:
            段.append(
                f"**{条['短题']}**〔{条['编号']}〕{条['条文']}\n"
                f"> 为什么:{条['为什么']}\n\n"
                f"层：{条['层']}；规模：{条['最小规模']}至{条.get('最大规模', '大')}；"
                f"特征：{'、'.join(条['需要特征']) or '无'}；"
                f"依赖：{'、'.join(条['依赖']) or '无'}；源条数：{条['源条数']}。\n\n"
                f"加回条件：{加回说明(条)}\n\n"
            )
    return "".join(段)


def 主程(参数=None) -> int:
    解析 = argparse.ArgumentParser(description="按项目需求裁剪通用条目；Python 3.11+，仅用标准库。")
    子 = 解析.add_subparsers(dest="命令", required=True)
    for 命令 in ("出", "校验", "表"):
        小解析 = 子.add_parser(命令, help={"出": "按需求卡输出各件", "校验": "检查条目表", "表": "生成便于阅读的条目表.md"}[命令])
        小解析.add_argument("--条目", type=Path, default=包目录 / "条目表.jsonl", help="缺省读取本包条目表.jsonl")
        if 命令 == "出":
            小解析.add_argument("--卡", type=Path, required=True, help="需求卡.toml")
            小解析.add_argument("--出", type=Path, required=True, help="输出目录；旧输出挪入其中的 _旧版")
    子.add_parser("档案", help="列出随包预设及其规模和特征")
    参 = 解析.parse_args(参数)
    try:
        if 参.命令 == "档案":
            for 路径 in sorted((包目录 / "档案").glob("*.toml")):
                卡 = 读卡(路径)
                特征 = "、".join(名 for 名 in 特征表 if 卡["特征"][名]) or "无"
                print(f"{路径.name}：规模={卡['规模']}；特征={特征}")
            return 0
        条目, 摘要 = 读条目(参.条目)
        if 参.命令 == "校验":
            print(f"校验通过：{len(条目)} 条，0 错。")
        elif 参.命令 == "表":
            目标 = 参.条目.with_name("条目表.md")
            目标.write_text(生成表(条目, 摘要), encoding="utf-8", newline="\n")
            print(f"已生成 {目标.name}：{len(条目)} 条。")
        else:
            卡 = 读卡(参.卡)
            各件 = 生成各件(条目, 卡, 摘要)
            件数 = 写各件(参.出, 各件)
            记录 = json.loads(各件["裁剪记录.json"])
            print(f"裁剪完成：保留 {len(记录['保留'])} 条，拆掉 {len(记录['拆掉'])} 条，输出 {件数} 件。")
        return 0
    except (输入错, OSError) as 错:
        print(f"错误：{错}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(主程())
