"""工单台:工单、对话线、员工名册与当前值面的命令行 + 本机 HTTP 服务。"""

# ★包入口不在导入时加载子模块:配置(位表等)在子模块加载时读一次,
#   测试要先把 TICKET_DESK_CONFIG 指到夹具再让子模块加载,所以这里按需取。
_EXPORTS = {
    "SLOTS": "model", "TASK_TIERS": "model", "TICKET_TYPES": "model", "TicketError": "model",
    "TicketStore": "store",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        from importlib import import_module

        return getattr(import_module(f"{__name__}.{_EXPORTS[name]}"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
