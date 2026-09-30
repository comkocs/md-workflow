/* 探针用的位表配置:与服务端读同一份配置文件(环境变量 TICKET_DESK_CONFIG 优先,否则 core/desk_config.json),
 * 换算成服务端 /desk-config.js 下发的那个形状(tools/tickets/config.py 的 client_view)。
 * 探针在 vm 沙箱里载入 tickets.js 之前把它挂到 sandbox.TICKET_DESK_CONFIG 上——
 * 与浏览器里 desk-config.js 先于 tickets.js 载入是同一个次序。
 * ★pytest 跑这些探针时,tests/__init__.py 已把 TICKET_DESK_CONFIG 指到测试夹具位表,子进程继承它。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

export function loadDeskConfig() {
  const here = dirname(fileURLToPath(import.meta.url));
  const path = process.env.TICKET_DESK_CONFIG || join(here, "..", "..", "..", "..", "desk_config.json");
  const raw = JSON.parse(readFileSync(path, "utf8").replace(/^\uFEFF/, ""));
  const rows = raw.位表;
  const role = (name) => (rows.find((row) => row.角色 === name) || {}).名字;
  const relays = rows.filter((row) => row.只发需求);
  return {
    位名: rows.map((row) => row.名字),
    总编排位: role("总编排"),
    复检位: role("复检"),
    平台位: role("平台"),
    只分发不派单位: relays.map((row) => row.名字),
    对口: Object.fromEntries(relays.map((row) => [row.名字, row.对口])),
    拍板人: "设计者",
    任务档: raw.任务档,
    /* 三键停用(需求-023)后服务端不再下发这两键;夹具删键后这里兜成空表,别让探针拿到 undefined。 */
    主力模型集合: raw.主力模型集合 || [],
    模型名册: raw.模型名册 || [],
    命令行: "ticket.py",
  };
}
