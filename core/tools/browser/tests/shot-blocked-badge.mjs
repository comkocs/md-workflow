/* 单卡「欠真登录图」徽标真执行探针。
 *
 * 取图受阻的单要在卡上一眼看得见(标题旁徽标,悬停看受阻来由),没有标记的单一个字都不许出。
 *
 * 跑法:node tools/browser/tests/shot-blocked-badge.mjs   (退 0 即通过)
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import { loadDeskConfig } from "./lib/desk-config.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "tickets.js"), "utf8");

const store = new Map();
const stubElement = () => new Proxy({}, {
  get: (target, key) => (key in target ? target[key] : (key === "classList" ? { add(){}, remove(){} } : () => {})),
  set: (target, key, value) => { target[key] = value; return true; },
});
const sandbox = {
  TICKET_DESK_CONFIG: loadDeskConfig(),
  location: { protocol: "https:", href: "https://desk/" },
  document: {
    querySelector: () => stubElement(),
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => stubElement(),
    body: null,
  },
  indexedDB: { open: () => ({}) },
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  setTimeout, clearTimeout, console, fetch: async () => ({}),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
const exported = "app,ticketCard";
vm.runInContext(`${source}\n;globalThis.__probe = {${exported}};`, sandbox, { filename: "tickets.js" });
const probe = sandbox.__probe;

probe.app.data = { items: [], slots: {}, staff: { 总监位: {} }, threads: {} };
const base = {
  编号: "T-009601", 类型: "派单", 标题: "欠图卡片探针", 所属总监位: "前端·页面接线",
  状态: "待判", 指派给: "", 交付项: [], 转交历史: [], 图片列表: [], 判语: "",
};
const failures = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures.push(`${name}\n    实际 ${JSON.stringify(actual)}\n    期望 ${JSON.stringify(expected)}`);
}

const marked = { ...base, 欠真登录图: { 时间: "x", 说明: "员工窗够不着取图机", 提交人: "前端·页面接线-07" } };
const markedHtml = probe.ticketCard(marked);
check("标记时徽标在", markedHtml.includes("欠真登录图"), true);
check("悬停带受阻来由", markedHtml.includes("员工窗够不着取图机"), true);
check("徽标类名在", markedHtml.includes("shot-blocked-badge"), true);
const cleanHtml = probe.ticketCard({ ...base });
check("无标记不渲染", cleanHtml.includes("欠真登录图"), false);

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("shot-blocked-badge 探针:4 项全过");
