"""扩展 server_deploy 的用例。

★与核心用例同一份测试夹具位表:这一句必须在任何 tools.tickets 子模块被导入之前执行。
"""

import os
from pathlib import Path

os.environ["TICKET_DESK_CONFIG"] = str(
    Path(__file__).resolve().parents[3] / "tools" / "tickets" / "tests" / "fixtures" / "desk_config.json"
)
