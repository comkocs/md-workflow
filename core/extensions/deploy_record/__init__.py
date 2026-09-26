"""扩展:上服记录(deploy-record)。

部署脚本在上服的后置闸通过之后自动建一张「上服记录」单:免员工窗、免判、免复检,
直接落终态,并把部署头写进当前值面。取证(独图/试玩)另开取证单(evidence-ticket,留在核心里)。

打开:配置文件「启用扩展」里写 "deploy_record"。打开后多出:
  · 命令行子命令 deploy-record;
  · 服务端方法 TicketService.deploy_record;
  · POST /api/action 的 op "deploy-record"(部署脚本就走这一条)。
"""

from __future__ import annotations

import argparse
from typing import Any

# ★包导入时不碰 tools.tickets:配置(位表)在 tools.tickets 子模块加载时读一次,
#   测试要先把 TICKET_DESK_CONFIG 指到夹具;所以这里的依赖一律放进函数里按需导入。

CLI_COMMANDS = frozenset({"deploy-record"})
DEPLOY_REPOS = ("server", "engine")
DEPLOY_ACTOR = "部署脚本"


def deploy_record(
    self: Any,
    head: str,
    probes: str,
    tickets: list[str] | tuple[str, ...] | str | None = None,
    repo: str = "server",
    actor: str = DEPLOY_ACTOR,
) -> dict[str, Any]:
    """上服记录:部署脚本在后置闸通过之后自动建的一张单,免员工窗、免判。

    ★为什么免判:它记的是**已经发生的事实**(线上现在跑的是哪个头、探针什么结果),
      不是要谁去做的活。给它开一扇窗、再走一遍判卷复检,是拿流程空转。
    ★取证(独图/试玩)**另开一张取证单**,取不到图不挡上服:
      上服成没成看探针,不看有没有人截到图。两件事绑在一起,会让一批部署单
      卡在「待独图」上,而线上其实早就跑起来了。

    建完直接落终态,并把部署头写进当前值面——那一格靠人手抄,抄漏就全台面读到旧值。
    """
    from tools.tickets.model import PLATFORM_SLOT, REVIEW_SLOT, TIER_LOW, TicketError, normalize_lines, now_text
    from tools.tickets.service import SHOT_EXEMPT

    head = str(head or "").strip()
    if not head:
        raise TicketError("上服记录必须写明部署头(提交号)。")
    if repo not in DEPLOY_REPOS:
        raise TicketError("上服记录的仓只能是 server 或 engine。")
    rows = normalize_lines(tickets)
    listed = "、".join(rows) if rows else "（未列批内单）"
    record = self.create_dispatch(
        PLATFORM_SLOT if repo == "server" else REVIEW_SLOT,
        f"上服记录 · {repo} {head}",
        [f"部署脚本自动建于 {now_text()}"],
        "线上工单台服务(部署脚本 update.sh 在后置闸通过之后自动建)",
        assign="",
        task_tier=TIER_LOW,
        context_lines=0,
        deliverables=[],
        internal=True,
        system_generated=True,
    )
    record["部署类"] = "上服记录"
    record["部署头"] = head
    record["部署仓"] = repo
    record["批内单"] = rows
    record["接线证据"] = {
        "文字": f"批内单:{listed}", "验证命令": "extensions/server_deploy/update.sh",
        "原样输出": str(probes or "").strip(), "图片列表": [],
    }
    # 免判免复检:它是既成事实的记录,不是活。直接落「实机复验过」——
    # 那是终态里语义最贴的一个(线上真跑起来了,探针是证据),而且自动退役那条链认它。
    record["状态"] = "实机复验过"
    record["判卷人"] = "部署脚本(免判)"
    record["判语"] = f"上服记录免判:线上 {repo} 头 = {head}；探针见原样输出。"
    record["复检人"] = "部署脚本(免复检)"
    record["复验"] = {
        "复验人": DEPLOY_ACTOR, "时间": now_text(), "结论": "过",
        "闸输出": str(probes or "").strip(), "说明": "后置闸(服务端口真在听)通过之后自动建。",
    }
    record["实机图标记"] = SHOT_EXEMPT
    record["免独图原因"] = "上服记录免图;取证(独图/试玩)另开取证单——取不到图不挡上服。"
    self.store.save_ticket(record, "deploy-record", actor, f"上服记录 · {repo} {head}")
    # 部署头自动写进当前值面。以前这一格靠人手抄,抄漏全台面读到旧值。
    key = "deploy_head_server" if repo == "server" else "deploy_head_engine"
    try:
        self.state_set(key, head, REVIEW_SLOT)
    except TicketError as error:  # pragma: no cover - 值面写失败不该把上服记录也废掉
        record["备注"] = (record.get("备注", "") + f"\n值面 {key} 没写成:{error}").strip()
        self.store.save_ticket(record, "note", actor, f"值面 {key} 没写成")
    return record


def add_cli_parsers(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("deploy-record", help="记一次上服(扩展 deploy_record)")
    parser.add_argument("--head", required=True, help="线上现在跑的提交号")
    parser.add_argument("--probes", default="", help="探针原样输出:服务 active / 端口在听 / 首页 302 …")
    parser.add_argument(
        "--repo", default="server", choices=list(DEPLOY_REPOS),
        help="server 记主仓部署头(写值面 deploy_head_server);engine 记第二个仓(写 deploy_head_engine)",
    )
    parser.add_argument("--ticket", dest="batch_tickets", action="append", default=[], help="批内单号,可重复")
    parser.add_argument("--by", default=DEPLOY_ACTOR)


def run_cli(service: Any, args: argparse.Namespace) -> tuple[Any, str] | None:
    if args.command != "deploy-record":
        return None
    from tools.tickets.ticket import compact_ticket

    ticket = service.deploy_record(args.head, args.probes, args.batch_tickets, args.repo, args.by)
    return ticket, compact_ticket(ticket) + "\n上服记录已建,免判免复检;部署头已写进当前值面。取证请另开取证单。"


def _http_deploy_record(service: Any, data: dict[str, Any]) -> dict[str, Any]:
    # 部署脚本在后置闸通过之后 POST 这一条。★ticket 在这条路上是空的:单还不存在,正是这里要建出来的。
    return service.deploy_record(
        str(data.get("head", "")), str(data.get("probes", "")),
        data.get("tickets"), str(data.get("repo", "server")) or "server",
        str(data.get("by", "")) or DEPLOY_ACTOR,
    )


HTTP_OPS = {"deploy-record": _http_deploy_record}


def activate() -> None:
    from tools.tickets.service import TicketService

    TicketService.deploy_record = deploy_record  # type: ignore[attr-defined]


def deactivate() -> None:
    from tools.tickets.service import TicketService

    if getattr(TicketService, "deploy_record", None) is deploy_record:
        del TicketService.deploy_record  # type: ignore[attr-defined]
