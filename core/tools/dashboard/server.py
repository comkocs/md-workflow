"""运行看板入口：标准库、本机监听、后台增量索引。"""
import sys

# 禁止运行时向源码目录写入字节码，唯一写入处为缓存与备注。
sys.dont_write_bytecode = True

import argparse
import os
import subprocess
import threading
from pathlib import Path

# 隔离模式（python -I）不把脚本目录放进 sys.path，这里按脚本自身位置补上，
# 只用于定位同目录的「看板」包，不取当前工作目录，两种起法行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from 看板.接口 import 本机服务
from 看板.汇总 import 看板汇总
from 看板.索引器 import 索引器, 原子写


def main():
    parser = argparse.ArgumentParser(description="本机会话运行看板")
    parser.add_argument("--打开浏览器", "--open", dest="打开浏览器", action="store_true", help="服务就绪后打开本机浏览器")
    parser.add_argument("--端口", "--port", dest="端口", type=int, help="只尝试指定端口")
    parser.add_argument("--缓存目录", "--cache-dir", dest="缓存目录", help="数据缓存下的隔离目录，相对路径以看板根为准")
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    cache_root = (base / "数据缓存").resolve()
    cache_dir = (base / args.缓存目录).resolve() if args.缓存目录 else cache_root
    if args.缓存目录 and (cache_dir == cache_root or not cache_dir.is_relative_to(cache_root)):
        parser.error("指定缓存目录必须在数据缓存目录之下")
    if args.端口 is not None and not 1 <= args.端口 <= 65535:
        parser.error("端口必须在 1 至 65535 之间")
    index = 索引器(Path.home() / ".claude/projects", base, cache_dir=cache_dir)
    summary = 看板汇总(index)
    server = None
    for port in ([args.端口] if args.端口 is not None else range(8765, 8776)):
        try:
            server = 本机服务(("127.0.0.1", port), index, summary)
            break
        except OSError:
            continue
    if server is None:
        print("启动失败：约定端口均不可用。")
        return 1
    # 索引和缓存载入均在后台；端口就绪即可返回已完成的部分。
    try:
        index.开始()
        原子写(cache_dir / "服务.json", {"pid": os.getpid(), "port": server.server_port})
        print(f"运行看板已就绪，端口 {server.server_port}，进程 {os.getpid()}", flush=True)
        if args.打开浏览器:
            def 打开浏览器():
                subprocess.Popen(["cmd.exe", "/c", "start", "", f"http://127.0.0.1:{server.server_port}"], cwd=base)
            threading.Thread(target=打开浏览器, daemon=True).start()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        index.关闭()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
