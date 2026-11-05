"""十对产品副本逐字节相同；任意一边漏同步即失败。"""

from pathlib import Path

import pytest


仓根 = Path(__file__).resolve().parents[3]
副本对 = [
    ("desk/子代理/执行员.md", "hive-dispatch/references/执行员.md"),
    ("desk/子代理/高阶施工员.md", "hive-dispatch/references/高阶施工员.md"),
    ("desk/子代理/读件员.md", "hive-dispatch/references/读件员.md"),
    ("desk/子代理/施工员.md", "hive-dispatch/references/施工员.md"),
    ("desk/子代理/审计员.md", "hive-dispatch/references/审计员.md"),
    ("desk/模型对照表.md", "hive-dispatch/references/模型对照表.md"),
    ("desk/模板/当日口径.md", "subtraction-review/references/当日口径.md"),
    ("desk/模板/闸账.md", "subtraction-review/references/闸账.md"),
    ("desk/模板/代码规范.md", "first-window-requirements/references/代码规范骨架.md"),
    ("desk/流程/素材管理.md", "first-window-requirements/references/素材管理骨架.md"),
]


@pytest.mark.parametrize("真源,副本", 副本对, ids=[Path(真源).stem for 真源, _ in 副本对])
def test_十对副本逐字节同步(真源, 副本):
    assert (仓根 / 真源).read_bytes() == (仓根 / ".agents/skills" / 副本).read_bytes(), (
        f"副本未同步：{真源} ↔ .agents/skills/{副本}")
