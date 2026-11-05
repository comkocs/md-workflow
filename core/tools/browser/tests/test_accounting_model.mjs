// 需求-032:真调用离线记账入口,与服务端同场景同键。
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../tickets.js', import.meta.url), 'utf8');
const names = ['platformOf', 'accountingModel', 'recordWebRework'];
const functions = names.map(name => source.split('\n').find(line =>
  line.startsWith(`function ${name}(`) || line.startsWith(`async function ${name}(`)
)).filter(Boolean).join('\n');
for (const [actual, platform, expected] of [
  ['', 'claude', 'claude-未标'],
  ['待定', 'codex', 'codex-未标'],
  ['', '', '未标平台-未标'],
  ['Sol__High', 'claude', 'sol-high'],
]) {
  const member = {员工名: '测试位-01', 平台: platform};
  const app = {data: {staff: {}, slots: {停用阈值: {同位: 3, 全项目: 5}}}};
  let saved;
  const context = vm.createContext({app, WINDOW_PLATFORMS: ['claude', 'codex', 'vscode', 'zcode'],
    staffFor: () => [member], writeJson: async (_handle, _name, data) => {saved = data;}});
  vm.runInContext(functions, context);
  const ticket = {实际模型: actual, 所属总监位: '测试位', 指派给: member.员工名};
  await context.recordWebRework(ticket);
  assert.deepEqual(Object.keys(saved?.模型记分 || {}), [expected]);
  assert.equal(saved.模型记分[expected].测试位, 1);
}
console.log('4 passed · 全过');
