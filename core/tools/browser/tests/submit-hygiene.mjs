/* 单卡「交板·仓库卫生」块的真执行探针。
 *
 * 复检实撞:测试日志入仓只在并线侧被拦,判卷人没看见。交板侧现在报而不拦,
 * 命中清单必须画在单卡上(判卷人可见),没命中则一个字都不许出现。
 *
 * 跑法:node tools/browser/tests/submit-hygiene.mjs   (退 0 即通过)
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
  编号: "T-009501", 类型: "派单", 标题: "仓库卫生卡片探针", 所属总监位: "前端·页面接线",
  状态: "待判", 指派给: "", 交付项: [], 转交历史: [], 图片列表: [], 判语: "",
};
const failures = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures.push(`${name}\n    实际 ${JSON.stringify(actual)}\n    期望 ${JSON.stringify(expected)}`);
}

/* ① 有命中:块在,逐件列出,带改法。 */
const dirty = { ...base, 仓库卫生: { 时间: "2026-09-14T00:00:00+08:00", 命中: ["artifacts/run.log", "docs/evidence/raw.zip"] } };
const dirtyHtml = probe.ticketCard(dirty);
check("命中时块在", dirtyHtml.includes("交板·仓库卫生"), true);
check("文件1在", dirtyHtml.includes("artifacts/run.log"), true);
check("文件2在", dirtyHtml.includes("docs/evidence/raw.zip"), true);
check("带改法", dirtyHtml.includes("git rm --cached artifacts/run.log"), true);
check("标注报而不拦", dirtyHtml.includes("报而不拦"), true);
/* ② 零命中:块整个不出现。 */
const cleanHtml = probe.ticketCard({ ...base, 仓库卫生: { 时间: "x", 命中: [] } });
check("零命中不渲染", cleanHtml.includes("交板·仓库卫生"), false);
/* ③ 字段缺失(老单):也不许炸。 */
const oldHtml = probe.ticketCard({ ...base });
check("老单不渲染不炸", oldHtml.includes("交板·仓库卫生"), false);

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("submit-hygiene 探针:8 项全过");
