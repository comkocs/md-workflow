"""工单系统测试。

★测试一律用夹具位表(tests/fixtures/desk_config.json),不读仓里的 desk_config.json:
  用例钉的是十五位、两个只发需求位这一整套前提,跟着随仓配置漂就全乱了。
  这一句必须在任何 tools.tickets 子模块被导入之前执行——配置在模块加载时读一次。
  子进程(ticket.py、node 探针)继承这个环境变量,跟本进程读的是同一份。
"""

import os
from pathlib import Path

os.environ["TICKET_DESK_CONFIG"] = str(Path(__file__).resolve().parent / "fixtures" / "desk_config.json")
