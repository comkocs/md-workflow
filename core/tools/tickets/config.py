"""工单台的项目配置:位表、特殊位、任务档、模型名册、启用的扩展。

原来这些全写死在代码里(位名在 model.py、模型名册在 store.py、网页里还抄着一份),
加一位、改一个名字要改好几处,漏一处就是「单发出去了,收件位看不见」。
现在收进一份 JSON 配置文件:

  · 默认读 core/desk_config.json(随仓一起的通用默认);
  · 环境变量 TICKET_DESK_CONFIG 指向另一份**完整**的配置文件时,整份换成那一份(不是叠加);
  · 读不出来、缺键、写歪一律**当场报错**,不悄悄退回默认值——配置写歪了却按默认名册跑起来,
    是这一类工具最坏的失败形态。

★代码里任何一处要用到位名、特殊位、任务档名、模型名册,都从这里取,不许再写字面量。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

CONFIG_ENV = "TICKET_DESK_CONFIG"
CORE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = CORE_ROOT / "desk_config.json"

# 位表里「角色」只认这三种,每种恰好一位:
#   总编排 —— 公共接口:合并授权、裁定落笔、改别位未读、值面写权;
#   复检   —— 独立于指挥链:复验、并线、上服、免独图、值面写权;
#   平台   —— 工单台本身的开发运维:折叠对话线通知、非业务阻塞清账。
ROLE_CONDUCTOR = "总编排"
ROLE_REVIEW = "复检"
ROLE_PLATFORM = "平台"
ROLES = (ROLE_CONDUCTOR, ROLE_REVIEW, ROLE_PLATFORM)
# 拍板人是人不是位:对话线与权限闸对这两者的处理完全不同,名字不许与任何位重名。
OWNER_ROLE = "设计者"
ROSTER_STATES = ("可用", "需批准", "退役")
REQUIRED_KEYS = ("位表", "任务档", "主力模型集合", "旧版主力模型集合", "模型名册", "停用阈值", "启用扩展")
# 员工名是「位名-两到三位编号」,位名自己不能长成那个样子,否则收敛回位名时会认错人。
_STAFF_SUFFIX = re.compile(r"-\d+$")


def config_path(environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    pointer = str(environ.get(CONFIG_ENV, "")).strip()
    return Path(pointer).expanduser() if pointer else DEFAULT_CONFIG_PATH


def load_config(path: Path) -> dict[str, Any]:
    """读一份配置并校验;任何不对都抛 RuntimeError,把文件路径与哪一项不对说清楚。"""
    if not path.is_file():
        hint = f"(环境变量 {CONFIG_ENV} 指向了它)" if path != DEFAULT_CONFIG_PATH else ""
        raise RuntimeError(f"工单台配置文件不存在:{path}{hint}")
    try:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"读不懂工单台配置文件 {path}:{exc}") from exc
    if not isinstance(values, dict):
        raise RuntimeError(f"工单台配置文件 {path} 的顶层必须是一个 JSON 对象。")
    validate(values, path)
    return values


def validate(values: dict[str, Any], path: Path | str = "<配置>") -> None:
    def fail(message: str) -> None:
        raise RuntimeError(f"工单台配置不自洽({path}):{message}")

    missing = [key for key in REQUIRED_KEYS if key not in values]
    if missing:
        fail(f"缺少这些键:{'、'.join(missing)}。必备键:{'、'.join(REQUIRED_KEYS)}。")
    # 以下划线开头的键是给人看的说明,程序不读。
    unknown = sorted(key for key in values if key not in REQUIRED_KEYS and not str(key).startswith("_"))
    if unknown:
        fail(f"有不认识的键:{'、'.join(unknown)}。合法键:{'、'.join(REQUIRED_KEYS)}(另可加以 _ 开头的说明键)。")

    rows = values["位表"]
    if not isinstance(rows, list) or not rows:
        fail("「位表」必须是非空列表。")
    names: list[str] = []
    roles: dict[str, list[str]] = {role: [] for role in ROLES}
    for row in rows:
        if not isinstance(row, dict):
            fail(f"「位表」的每一行都必须是对象,这一行不是:{row!r}")
        name = str(row.get("名字", "")).strip()
        if not name:
            fail(f"「位表」有一行没有名字:{row!r}")
        if _STAFF_SUFFIX.search(name):
            fail(f"位名「{name}」不能以「-数字」结尾——那是员工编号的写法。")
        if name == OWNER_ROLE:
            fail(f"位名不能叫「{OWNER_ROLE}」:{OWNER_ROLE}是拍板的人,不是位。")
        unknown_fields = sorted(set(row) - {"名字", "角色", "只发需求", "对口"})
        if unknown_fields:
            fail(f"位「{name}」有不认识的字段:{'、'.join(unknown_fields)}。")
        role = str(row.get("角色", "")).strip()
        if role:
            if role not in ROLES:
                fail(f"位「{name}」的角色「{role}」不认识,只能是:{'、'.join(ROLES)}。")
            roles[role].append(name)
        relay = row.get("只发需求", False)
        if not isinstance(relay, bool):
            fail(f"位「{name}」的「只发需求」必须是 true 或 false。")
        if relay and role:
            fail(f"位「{name}」既有角色「{role}」又只发需求——有角色的位要干活,两者不能同时成立。")
        if relay and not str(row.get("对口", "")).strip():
            fail(f"只发需求的位「{name}」必须写「对口」(它在哪一类事务上替{OWNER_ROLE}对接),拒绝语要引用它。")
        names.append(name)
    if len(set(names)) != len(names):
        fail(f"「位表」里有重名:{'、'.join(names)}。")
    for role, holders in roles.items():
        if len(holders) != 1:
            fail(f"角色「{role}」必须恰好一位,现在是 {len(holders)} 位({'、'.join(holders) or '无'})。")

    tiers = values["任务档"]
    if (
        not isinstance(tiers, list) or len(tiers) != 3
        or any(not isinstance(tier, str) or not tier.strip() for tier in tiers)
        or len({tier.strip() for tier in tiers}) != 3
    ):
        fail("「任务档」必须是三个互不相同的非空名字,从高到低排(例如 [\"甲\",\"乙\",\"丙\"])。")

    roster = values["模型名册"]
    if not isinstance(roster, list):
        fail("「模型名册」必须是列表。")
    models: list[str] = []
    for row in roster:
        if not isinstance(row, dict) or not str(row.get("模型", "")).strip():
            fail(f"「模型名册」每一行都必须是带「模型」的对象:{row!r}")
        model = str(row["模型"]).strip()
        if str(row.get("任务档上限", "")) not in tiers:
            fail(f"模型「{model}」的任务档上限必须是任务档之一:{'、'.join(tiers)}。")
        if str(row.get("状态", "")) not in ROSTER_STATES:
            fail(f"模型「{model}」的状态只能是:{'、'.join(ROSTER_STATES)}。")
        if not isinstance(row.get("可选档位", []), list):
            fail(f"模型「{model}」的「可选档位」必须是列表。")
        levels = row.get("档位任务档", {})
        if not isinstance(levels, dict) or any(str(tier) not in tiers for tier in levels.values()):
            fail(f"模型「{model}」的「档位任务档」的值必须是任务档之一:{'、'.join(tiers)}。")
        models.append(model)
    if len(set(models)) != len(models):
        fail("「模型名册」里有重复的模型名。")
    for key in ("主力模型集合", "旧版主力模型集合"):
        group = values[key]
        if not isinstance(group, list) or any(not isinstance(item, str) for item in group):
            fail(f"「{key}」必须是字符串列表。")
    stray = [item for item in values["主力模型集合"] if item not in models]
    if stray:
        fail(f"「主力模型集合」里的模型不在「模型名册」里:{'、'.join(stray)}。")

    thresholds = values["停用阈值"]
    if (
        not isinstance(thresholds, dict) or set(thresholds) != {"同位", "全项目"}
        or any(not isinstance(number, int) or isinstance(number, bool) or number < 1 for number in thresholds.values())
    ):
        fail("「停用阈值」必须是 {\"同位\": 正整数, \"全项目\": 正整数}。")

    extensions = values["启用扩展"]
    if not isinstance(extensions, list) or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in extensions
    ):
        fail("「启用扩展」必须是扩展目录名的列表(core/extensions/ 下的子目录名,字母数字下划线)。")


PATH: Path = config_path()
_VALUES: dict[str, Any] = load_config(PATH)


def _slot_rows() -> list[dict[str, Any]]:
    return [dict(row) for row in _VALUES["位表"]]


def _role_holder(role: str) -> str:
    return next(str(row["名字"]).strip() for row in _slot_rows() if str(row.get("角色", "")).strip() == role)


# ── 派生常量:模块加载时算一次,代码各处只读这些 ──────────────────────────
SLOTS: tuple[str, ...] = tuple(str(row["名字"]).strip() for row in _slot_rows())
CONDUCTOR_SLOT: str = _role_holder(ROLE_CONDUCTOR)
REVIEW_SLOT: str = _role_holder(ROLE_REVIEW)
PLATFORM_SLOT: str = _role_holder(ROLE_PLATFORM)
# 只发需求/疑问/拍板、不建派单、不判卷、不并线、不上服的位。
DISPATCH_FORBIDDEN_SLOTS: tuple[str, ...] = tuple(
    str(row["名字"]).strip() for row in _slot_rows() if row.get("只发需求")
)
# 每个只发需求的位管的是哪一类事务——拒绝语要说对,不许写死成某一位的口径。
RELAY_SLOT_SCOPE: dict[str, str] = {
    str(row["名字"]).strip(): str(row.get("对口", "")).strip() for row in _slot_rows() if row.get("只发需求")
}
TASK_TIERS: tuple[str, ...] = tuple(str(tier).strip() for tier in _VALUES["任务档"])
TIER_TOP, TIER_MID, TIER_LOW = TASK_TIERS
MAIN_MODELS: list[str] = [str(item) for item in _VALUES["主力模型集合"]]
LEGACY_MAIN_MODELS: list[str] = [str(item) for item in _VALUES["旧版主力模型集合"]]
MODEL_ROSTER: list[dict[str, Any]] = [dict(row) for row in _VALUES["模型名册"]]
BAN_THRESHOLDS: dict[str, int] = dict(_VALUES["停用阈值"])
ENABLED_EXTENSIONS: tuple[str, ...] = tuple(_VALUES["启用扩展"])


def client_view() -> dict[str, Any]:
    """网页要用的那一部分配置:位名、特殊位、任务档、模型名册。与服务端同一份来源,不另抄。"""
    return {
        "位名": list(SLOTS),
        "总编排位": CONDUCTOR_SLOT,
        "复检位": REVIEW_SLOT,
        "平台位": PLATFORM_SLOT,
        "只分发不派单位": list(DISPATCH_FORBIDDEN_SLOTS),
        "对口": dict(RELAY_SLOT_SCOPE),
        "拍板人": OWNER_ROLE,
        "任务档": list(TASK_TIERS),
        "主力模型集合": list(MAIN_MODELS),
        "模型名册": [dict(row) for row in MODEL_ROSTER],
        # 网页上给人照抄的命令行:本机这份 ticket.py 的路径(正斜杠,bash 与 PowerShell 都认)。
        "命令行": str(CORE_ROOT / "tools" / "tickets" / "ticket.py").replace("\\", "/"),
    }
