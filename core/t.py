#!/usr/bin/env python3
"""$T:工单台命令行的短写法,读写与 core/start.py 起的台面同一份数据。

    python core/t.py <子命令> [参数]          正式台数据(core/data/;设了 TICKET_DESK_ROOT 就用它)
    python core/t.py --demo <子命令> [参数]   演示台数据(core/demo-data/);--demo 只认放在第一个
    python core/t.py -h                       全部子命令

只做三件事,不加任何业务逻辑:
  ① 定数据目录(设 TICKET_DESK_ROOT,与 start.py 同一套规则);
  ② 输出统一 UTF-8 并逐行刷出(同 start.py);
  ③ 给 ticket.py 追加 --local:永远只读写本机这份数据,
     不跟随 TICKET_REMOTE / TICKET_ENV / remote.env 去连任何服务器。
其余参数原样交给 core/tools/tickets/ticket.py。连服务器(方式乙)直接用 ticket.py,见仓根 README。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import start  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    demo = bool(arguments) and arguments[0] == "--demo"
    if demo:
        arguments = arguments[1:]
    start.use_utf8_output()
    os.environ[start.DATA_ROOT_ENV] = str(start.data_root(demo))

    from tools.tickets.ticket import main as ticket_main

    return ticket_main([*arguments, "--local"])


if __name__ == "__main__":
    sys.exit(main())
