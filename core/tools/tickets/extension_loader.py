"""按配置加载扩展(core/extensions/<名字>/)。

默认一个都不加载。要用哪个,就在配置文件的「启用扩展」里写它的目录名;
ticket.py 建命令行解析器、服务端起服务时各调一次 load_configured(),按名导入。
测试里用 activate()/deactivate() 显式开关,不改配置文件。

扩展模块可以提供的钩子(都可省):
  CLI_COMMANDS      frozenset[str]   它加的子命令名;这些命令一律按「写命令」加锁执行。
  add_cli_parsers(commands)          往 argparse 的子命令表里加它的子命令。
  run_cli(service, args)             处理自己的子命令,返回 (载荷, 文本);不是它的命令返回 None。
  HTTP_OPS          dict[str, fn]    POST /api/action 的 op 名 → fn(service, data)。
  activate() / deactivate()          打开/关闭时的附加动作(例如给 TicketService 挂方法)。
"""

from __future__ import annotations

import argparse
import sys
from importlib import import_module
from types import ModuleType
from typing import Any, Callable

from .config import CORE_ROOT, ENABLED_EXTENSIONS

EXTENSIONS_DIR = CORE_ROOT / "extensions"
_ACTIVE: dict[str, ModuleType] = {}


def available() -> list[str]:
    """extensions/ 下所有带 __init__.py 的子目录名。"""
    if not EXTENSIONS_DIR.is_dir():
        return []
    return sorted(
        child.name for child in EXTENSIONS_DIR.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file() and not child.name.startswith("_")
    )


def activate(name: str) -> ModuleType:
    """打开一个扩展。重复打开无副作用;目录不存在就报错并列出有哪些。"""
    if name in _ACTIVE:
        return _ACTIVE[name]
    if not (EXTENSIONS_DIR / name / "__init__.py").is_file():
        raise RuntimeError(
            f"配置里启用的扩展「{name}」不存在:{EXTENSIONS_DIR / name}。"
            f"现有扩展:{'、'.join(available()) or '(一个都没有)'}。"
        )
    root = str(CORE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    module = import_module(f"extensions.{name}")
    hook = getattr(module, "activate", None)
    if callable(hook):
        hook()
    _ACTIVE[name] = module
    return module


def deactivate(name: str) -> None:
    module = _ACTIVE.pop(name, None)
    hook = getattr(module, "deactivate", None) if module else None
    if callable(hook):
        hook()


def load_configured() -> list[str]:
    """按配置文件「启用扩展」逐个打开;返回当前已打开的扩展名。"""
    for name in ENABLED_EXTENSIONS:
        activate(name)
    return sorted(_ACTIVE)


def active() -> list[str]:
    return sorted(_ACTIVE)


def cli_commands() -> set[str]:
    names: set[str] = set()
    for module in _ACTIVE.values():
        names |= set(getattr(module, "CLI_COMMANDS", ()))
    return names


def add_cli_parsers(commands: argparse._SubParsersAction) -> None:
    for module in _ACTIVE.values():
        hook = getattr(module, "add_cli_parsers", None)
        if callable(hook):
            hook(commands)


def run_cli(service: Any, args: argparse.Namespace) -> tuple[Any, str] | None:
    for module in _ACTIVE.values():
        hook = getattr(module, "run_cli", None)
        if callable(hook):
            result = hook(service, args)
            if result is not None:
                return result
    return None


def http_op(op: str) -> Callable[[Any, dict[str, Any]], Any] | None:
    for module in _ACTIVE.values():
        handler = getattr(module, "HTTP_OPS", {}).get(op)
        if handler:
            return handler
    return None
