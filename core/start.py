#!/usr/bin/env python3
"""一条命令起工单台(方式甲):文件存储、明文只听 127.0.0.1、不登录,浏览器打开直接是台面。

    python core/start.py              正式台面:数据在 core/data/,端口 8787
    python core/start.py --demo       演示台面:数据在 core/demo-data/,端口 8788;目录为空时先播种再起
    python core/start.py --port 9000  换端口

只用 Python 标准库。Ctrl+C 停服务。
命令行读写同一份数据用 core/t.py(见仓根 README「起工单台」)。
上服务器(方式乙:SQLite 库 + 证书 + 登录)不走这里,见 core/extensions/server_deploy/上服清单.md。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import socket
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from tools.tickets.store import DATA_ROOT_ENV, DEFAULT_ROOT  # noqa: E402

DATA_DIR = DEFAULT_ROOT              # 正式台:与 ticket.py 不设 TICKET_DESK_ROOT 时是同一处(core/data/)
DEMO_DIR = CORE / "demo-data"        # 演示台:单独一处,与正式数据互不相干
DEFAULT_PORT = 8787
DEMO_PORT = 8788                     # 与正式台错开,两个台可以同时开
TICKET_NUMBER = re.compile(r"\bT-\d{6}\b")


def use_utf8_output() -> None:
    """输出统一 UTF-8 并逐行刷出。

    Windows 上 Git Bash 等终端走的是管道:Python 默认按本机代码页输出(中文会乱),
    而且整块缓冲——服务起好那一行「工单台服务已启动」会一直憋在缓冲里不出来。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", line_buffering=True)


def data_root(demo: bool, root: str | None = None, environ: dict[str, str] | None = None) -> Path:
    """数据目录:显式 --root 优先;演示台固定 core/demo-data/;正式台认 TICKET_DESK_ROOT,不设就是 core/data/。"""
    environ = os.environ if environ is None else environ
    if root:
        return Path(root).resolve()
    if demo:
        return DEMO_DIR
    configured = environ.get(DATA_ROOT_ENV, "").strip()
    return Path(configured).resolve() if configured else DATA_DIR


def port_in_use(port: int) -> bool:
    """本机这个端口上已经有服务在听吗。

    ★Windows 上第二个服务能在同一端口再绑一次而不报错,请求却全落到先起的那个——
      看上去起好了,浏览器里其实是另一份数据。所以起服之前先探一下。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def is_empty(root: Path) -> bool:
    return not root.exists() or (root.is_dir() and not any(root.iterdir()))


class DemoSeedError(RuntimeError):
    pass


def run_cli(*arguments: str) -> str:
    """在本进程里跑一条 ticket.py 命令(带 --local),失败就当场报出原话。"""
    from tools.tickets.ticket import main as ticket_main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ticket_main([*arguments, "--local"])
    if code != 0:
        raise DemoSeedError(
            f"播种命令失败(退出码 {code}):ticket.py {' '.join(arguments)}\n{out.getvalue()}{err.getvalue()}".rstrip()
        )
    return out.getvalue()


def demo_slots() -> tuple[str, str, str]:
    """演示用的三个位,全部取自当前配置:总编 = 角色为总编排的位;两位总监 = 位表里前两个普通位。"""
    from tools.tickets.model import CONDUCTOR_SLOT, DISPATCH_FORBIDDEN_SLOTS, PLATFORM_SLOT, REVIEW_SLOT, SLOTS

    special = {CONDUCTOR_SLOT, REVIEW_SLOT, PLATFORM_SLOT, *DISPATCH_FORBIDDEN_SLOTS}
    directors = [slot for slot in SLOTS if slot not in special]
    if len(directors) < 2:
        raise DemoSeedError("配置的位表里能派单的普通位不足两个,演示数据没法播种。")
    return CONDUCTOR_SLOT, directors[0], directors[1]


DEMO_TASKBOOK = """# {title}

演示用任务书:由 core/start.py --demo 播种时写出,不是真任务。

第 0 步:读本单(t.py --demo show <单号>)。
第 1 步:{body}
收尾:按单上交付项交板,附验证命令与原样输出。
"""


def seed_demo(root: Path) -> list[str]:
    """往空的演示目录里播种:一个总编、两位总监、三张不同状态的单(新建 / 已认领 / 关闭),员工三名。

    ★全部经 ticket.py 自己的命令播,不手写任何工单数据文件——建单闸、状态机、名册规则一条不绕。
      唯一手写的是三份演示任务书(单的输入文档,放在 <数据目录>/任务书/ 下,不是工单库的一部分)。
    """
    from tools.tickets.config import MAIN_MODELS, TIER_LOW, TIER_MID, TIER_TOP

    conductor, first, second = demo_slots()
    model = MAIN_MODELS[0]
    books = root / "任务书"
    books.mkdir(parents=True, exist_ok=True)

    def taskbook(name: str, title: str, body: str) -> str:
        path = books / f"{name}.md"
        path.write_text(DEMO_TASKBOOK.format(title=title, body=body), encoding="utf-8")
        return str(path)

    def new(name: str, slot: str, title: str, body: str, *extra: str) -> str:
        output = run_cli(
            "new", "--slot", slot, "--title", title, "--by", slot,
            "--source", "演示:需求-示例", "--taskbook", taskbook(name, title, body), *extra,
        )
        found = TICKET_NUMBER.search(output)
        if not found:
            raise DemoSeedError(f"建单回显里没找到单号:\n{output}")
        return found.group(0)

    def core_file(*parts: str) -> str:
        # 交付项写本仓的绝对路径:本机模式交板时相对路径按「当前目录」核,从哪起台都不该影响演示数据。
        return CORE.joinpath(*parts).as_posix()

    def hire(slot: str) -> str:
        return run_cli("staff", "new", "--slot", slot, "--tool", model).splitlines()[0].strip()

    first_a, first_b, second_a = hire(first), hire(first), hire(second)

    # ① 新建:用户可感知单,已指派、还没认领。
    fresh = new(
        "演示单-新建", first, "台面顶栏加一行「怎么起台」提示", "在网页顶栏加一行起台命令提示。",
        "--player-facing", "--tier", TIER_MID, "--consumer", "浏览器里的工单台首页",
        "--deliverable", core_file("tools", "browser", "index.html"), "--assign", first_b,
    )

    # ② 已认领:内部单,员工已认领在做;总编在这一位的对话线上留一句。
    claimed = new(
        "演示单-已认领", second, "list 子命令加按指派人筛选", "给 list 加 --assignee 筛选。",
        "--internal", "--tier", TIER_TOP, "--consumer", "各位总监在命令行里查自己名下的单",
        "--deliverable", core_file("tools", "tickets", "ticket.py"), "--assign", second_a,
    )
    run_cli("claim", claimed, "--by", second_a)
    run_cli("say", "--slot", second, "--by", conductor, "--ref", claimed,
            f"演示:{claimed} 已认领,做完按任务书交板。")

    # ③ 关闭:内部单走完全程——认领 → 交板 → 免判卷 → settle 收口 → 总编 live → 关单。
    done = new(
        "演示单-关闭", first, "工单卡片状态色与图例对齐", "把卡片状态色改成与图例一致。",
        "--internal", "--tier", TIER_LOW, "--context-lines", "300",
        "--consumer", "浏览器里的工单卡片", "--deliverable", core_file("tools", "browser", "tickets.css"), "--assign", first_a,
    )
    run_cli("claim", done, "--by", first_a)
    run_cli("submit", done, "--evidence", "演示数据:这里本该贴真实验证", "--verify-command", "echo 演示",
            "--raw-output", "演示")
    run_cli("set", done, "--exempt-judging", "是", "--by", first)
    run_cli("settle", done, "--by", first_a, "--fact", "演示数据:没有真提交,提交号是占位", "--main-commit", "0000000")
    run_cli("live", done, "--by", conductor, "--shot", "同图")
    run_cli("close", done, "--by", first)
    return [fresh, claimed, done]


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        prog="start.py",
        description="一条命令起工单台(方式甲:文件存储、只听 127.0.0.1、不登录)。Ctrl+C 停服务。",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="起演示台面:数据在 core/demo-data/(与正式数据分开),目录为空时先播种一个总编、两位总监、三张不同状态的单;"
             "已播过就直接起。与 ticket.py 的 demo --archive 子命令(归档旧演示单)无关",
    )
    parser.add_argument("--port", type=int, help=f"端口;默认正式台 {DEFAULT_PORT}、演示台 {DEMO_PORT}")
    parser.add_argument(
        "--root",
        help="数据目录;默认正式台 = 环境变量 TICKET_DESK_ROOT 或 core/data/,演示台 = core/demo-data/",
    )
    parser.add_argument("--open", action="store_true", help="起好后自动打开浏览器")
    args = parser.parse_args(argv)

    root = data_root(args.demo, args.root)
    port = args.port if args.port is not None else (DEMO_PORT if args.demo else DEFAULT_PORT)
    if not 0 <= port <= 65535:
        print("拦下:端口必须在 0 到 65535 之间。", file=sys.stderr)
        return 2
    if port and port_in_use(port):
        print(f"拦下:本机端口 {port} 上已经有服务在听(多半是已经起过一个工单台);换一个端口:--port <别的数>。",
              file=sys.stderr)
        return 2

    os.environ[DATA_ROOT_ENV] = str(root)
    shown_root = str(root).replace("\\", "/")
    if args.demo and is_empty(root):
        print(f"演示数据目录是空的,先播种:{shown_root}")
        try:
            numbers = seed_demo(root)
        except BaseException as exc:
            # 目录是本次从空建起来的,半截数据留着只会让下次误以为「已播过」。
            shutil.rmtree(root, ignore_errors=True)
            if isinstance(exc, DemoSeedError):
                print(f"拦下:{exc}", file=sys.stderr)
                return 2
            raise
        for line in run_cli("list").splitlines():
            if any(line.startswith(number) for number in numbers):
                print(f"  {line}")

    cli = ((sys.executable or "python") + " " + str(CORE / "t.py")).replace("\\", "/")
    if args.demo and not args.root:
        cli += " --demo"
    elif args.root or root != DATA_DIR:
        cli = f"(先设环境变量 {DATA_ROOT_ENV}={shown_root}) {cli}"
    print(f"工单台 · {'演示台面' if args.demo else '正式台面'} · 方式甲(文件存储 · 只听 127.0.0.1 · 不登录)")
    print(f"数据目录:{shown_root}")
    print(f"命令行读写同一份数据:{cli} <子命令>")
    print("停服务:Ctrl+C")

    from tools.tickets.ticket import main as ticket_main

    serve = ["serve", "--host", "127.0.0.1", "--port", str(port), "--local"]
    return ticket_main([*serve, "--open"] if args.open else serve)


if __name__ == "__main__":
    sys.exit(main())
