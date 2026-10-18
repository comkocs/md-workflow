/* 运行看板通用组件。 */
(() => {
  'use strict';
  const { ref, computed, watch, nextTick } = Vue;
  const number = value => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(Number(value) || 0);
  const compact = value => {
    const n = Number(value) || 0;
    return n >= 1e8 ? `${(n / 1e8).toFixed(2)} 亿` : n >= 1e4 ? `${(n / 1e4).toFixed(1)} 万` : number(n);
  };
  const contextTokens = value => Number(value) >= 10000 ? `${(Number(value) / 10000).toFixed(1)} 万` : `${number(value)} token`;
  const hasContext = agent => agent.last_ctx != null && Number.isFinite(Number(agent.last_ctx)) && Number(agent.last_ctx) >= 0;
  const contextRole = agent => agent.ctx_level === 'warn' ? 'warning' : agent.ctx_level === 'severe' ? 'serious' : 'normal';
  const statusColors = { warning: '#fab219', serious: '#ec835a' };
  const money = value => value == null || !Number.isFinite(Number(value)) ? '—' : `$${Number(value).toFixed(2)}`;
  const sumMoney = (rows, key) => {
    const known = rows.filter(row => !row.price_missing && row[key] != null && Number.isFinite(Number(row[key])));
    return known.length ? known.reduce((sum, row) => sum + Number(row[key]), 0) : rows.length ? null : 0;
  };
  const percent = value => value == null ? '—' : `${((Number(value) || 0) * 100).toFixed(1)}%`;
  const duration = value => {
    const n = Number(value) || 0;
    return n >= 60 ? `${Math.floor(n / 60)} 小时 ${Math.round(n % 60)} 分` : `${n.toFixed(1)} 分钟`;
  };
  const bytes = value => {
    const n = Number(value) || 0;
    return n >= 1048576 ? `${(n / 1048576).toFixed(2)} MB` : n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${number(n)} B`;
  };
  const totalTokens = row => ['input', 'output', 'cache_read', 'cache_write'].reduce((sum, key) => sum + (Number(row[key]) || 0), 0);
  const vendors = [
    { id: 'claude', name: 'Claude', color: '#3987e5' },
    { id: 'gpt', name: 'GPT', color: '#d95926' },
    { id: 'glm', name: 'GLM', color: '#199e70' },
    { id: 'deepseek', name: 'DeepSeek', color: '#c98500' },
    { id: 'other', name: '其他', color: '#d55181' }
  ];
  const vendorOf = model => {
    const name = String(model || '').toLowerCase();
    return vendors.find(v => v.id !== 'other' && name.startsWith(`${v.id}-`)) || vendors[4];
  };
  const shortModel = model => {
    const name = String(model || '未知模型');
    if (/opus/i.test(name)) return 'Opus';
    if (/sonnet/i.test(name)) return 'Sonnet';
    if (/haiku/i.test(name)) return 'Haiku';
    return name.replace(/^claude-/, '').replace(/^gpt-6(?:\.1)?-/, '').replace(/^deepseek-/, 'DeepSeek ');
  };
  const modelColor = model => {
    const name = String(model || '').toLowerCase();
    if (name.startsWith('claude-')) return /haiku/.test(name) ? '#2a78d6' : /sonnet/.test(name) ? '#3987e5' : '#5598e7';
    if (name.startsWith('gpt-')) return /sol/.test(name) ? '#c84e20' : /luna/.test(name) ? '#ba481b' : '#d95926';
    if (name.startsWith('glm-')) return /flash/.test(name) ? '#14835c' : '#199e70';
    return vendorOf(name).color;
  };
  const billingLabel = value => /sub|subscription|订阅/i.test(String(value)) ? '订阅 · 仅参考折算' : '按量 · 实付美元';
  const severity = kind => {
    if (/按名杀|上下文过线/.test(kind)) return { role: 'critical', icon: '×', label: '危急' };
    if (/长调用|标题不符|接口报错/.test(kind)) return { role: 'serious', icon: '!', label: '严重' };
    return { role: 'warning', icon: '△', label: '警告' };
  };
  const format = (value, type) => {
    if (value == null || value === '') return '—';
    if (type === 'money') return money(value);
    if (type === 'tokens') return number(value);
    if (type === 'number') return number(value);
    if (type === 'percent') return percent(value);
    if (type === 'duration') return duration(value);
    if (type === 'billing') return billingLabel(value);
    if (type === 'boolean') return value ? '在跑' : '已结束';
    return Array.isArray(value) ? value.join(' / ') : String(value);
  };
  const priceMissing = (row, col) => col.format === 'money' && (row.price_missing === true || row[col.key] == null);
  const cellText = (row, col) => priceMissing(row, col) ? '—' : format(row[col.key], col.format);
  const cellTitle = (row, col) => priceMissing(row, col) ? '价目表缺此模型' : format(row[col.key], col.format);
  const SortTable = {
    props: { rows: { type: Array, default: () => [] }, columns: { type: Array, default: () => [] }, caption: String, clickable: Boolean, rowKey: { type: String, default: 'id' }, initialSort: String },
    emits: ['row-click'],
    setup(props, { emit }) {
      const sort = ref(props.initialSort || '');
      const ascending = ref(true);
      const sorted = computed(() => {
        if (!sort.value) return props.rows;
        const key = sort.value;
        return [...props.rows].sort((a, b) => {
          const left = a[key], right = b[key];
          const value = typeof left === 'number' || typeof left === 'boolean' ? Number(left || 0) - Number(right || 0) : String(left ?? '').localeCompare(String(right ?? ''), 'zh-CN', { numeric: true });
          return ascending.value ? value : -value;
        });
      });
      const toggle = key => { ascending.value = sort.value === key ? !ascending.value : true; sort.value = key; };
      const open = row => { if (props.clickable) emit('row-click', row); };
      return { sort, ascending, sorted, toggle, open, format, cellText, cellTitle, shortModel, modelColor };
    },
    template: `<div class="table-scroll"><table><caption class="sr-only">{{caption || '数据表'}}，点击列标题排序</caption><thead><tr><th v-for="col in columns" :key="col.key" :class="{numeric:col.numeric}" :aria-sort="sort === col.key ? (ascending ? 'ascending' : 'descending') : 'none'"><button class="sort-button" @click="toggle(col.key)">{{col.label}} <span aria-hidden="true">{{sort === col.key ? (ascending ? '↑' : '↓') : '↕'}}</span></button></th><th v-if="$slots.actions" scope="col">编辑</th></tr></thead><tbody><tr v-for="(row,index) in sorted" :key="row[rowKey] || index" :class="{'clickable-row':clickable}" @click="open(row)"><td v-for="(col,ci) in columns" :key="col.key" :class="{numeric:col.numeric,'wide-cell':col.wide}" :title="cellTitle(row,col)"><button v-if="ci === 0 && clickable" class="row-link" @click.stop="open(row)">{{cellText(row,col)}}</button><template v-else-if="col.key === 'model'"><span class="swatch" :style="{background:modelColor(row[col.key])}" aria-hidden="true"></span>{{row[col.key]}}</template><span v-else-if="col.key === 'running'" class="status-pill" :class="row.running ? 'good' : 'neutral'"><span class="status-symbol" :class="{'live-dot':row.running}" aria-hidden="true">{{row.running ? '' : '○'}}</span>{{row.running ? '在跑' : '已结束'}}</span><span v-else>{{cellText(row,col)}}</span></td><td v-if="$slots.actions" @click.stop><slot name="actions" :row="row"></slot></td></tr><tr v-if="!rows.length"><td :colspan="columns.length + ($slots.actions ? 1 : 0)" class="empty-cell">所选范围内暂无数据</td></tr></tbody></table></div>`
  };
  const InlineNote = {
    props: { value: String, label: String, multiline: Boolean, maxlength: { type: Number, default: 1000 }, saving: Boolean },
    emits: ['save'],
    setup(props, { emit }) {
      const editing = ref(false), draft = ref(''), field = ref(null);
      const begin = async () => { draft.value = props.value || ''; editing.value = true; await nextTick(); field.value?.focus(); };
      const cancel = () => { editing.value = false; };
      const save = () => { emit('save', draft.value.trim()); editing.value = false; };
      watch(() => props.saving, value => { if (!value) draft.value = props.value || ''; });
      return { editing, draft, field, begin, cancel, save };
    },
    template: `<form v-if="editing" class="inline-editor" @submit.prevent="save" @click.stop><label class="sr-only">{{label}}</label><textarea v-if="multiline" ref="field" v-model="draft" :maxlength="maxlength" :aria-label="label" rows="2" @keydown.esc.prevent="cancel"></textarea><input v-else ref="field" v-model="draft" :maxlength="maxlength" :aria-label="label" @keydown.esc.prevent="cancel"><div class="editor-actions"><button class="button primary small" type="submit" :disabled="saving">保存</button><button class="button ghost small" type="button" @click="cancel">取消</button></div></form><button v-else class="inline-note" :class="{'note-empty':!value}" :disabled="saving" :title="'修改' + label" @click.stop="begin"><span>{{value || ('添加' + label)}}</span><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m16 3 5 5L9 20l-6 1 1-6Z M14 5l5 5"/></svg><span class="sr-only">修改{{label}}</span></button>`
  };
  const SessionNoteEditor = {
    props: { row: { type: Object, required: true }, saving: Boolean },
    emits: ['save'],
    setup(props, { emit }) {
      const editing = ref(false), alias = ref(''), note = ref(''), panel = ref(null), aliasField = ref(null);
      let previousFocus;
      const close = () => { editing.value = false; document.body.classList.remove('note-editor-open'); previousFocus?.focus(); };
      const open = async () => {
        previousFocus = document.activeElement; alias.value = props.row.alias || ''; note.value = props.row.note || ''; editing.value = true;
        document.body.classList.add('note-editor-open'); await nextTick(); aliasField.value?.focus();
      };
      const save = () => { emit('save', { alias: alias.value.trim(), note: note.value.trim() }); close(); };
      const keys = event => {
        if (event.key === 'Escape') { event.preventDefault(); close(); return; }
        if (event.key !== 'Tab') return;
        const fields = [...panel.value.querySelectorAll('button, input, textarea')].filter(field => !field.disabled);
        const first = fields[0], last = fields.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      };
      Vue.onBeforeUnmount(() => { if (editing.value) document.body.classList.remove('note-editor-open'); });
      return { editing, alias, note, panel, aliasField, open, close, save, keys };
    },
    template: `<button class="button ghost small note-edit-button" :disabled="saving" title="编辑窗口别名与备注" aria-label="编辑窗口别名与备注" aria-haspopup="dialog" @click.stop="open"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m16 3 5 5L9 20l-6 1 1-6Z M14 5l5 5"/></svg></button><teleport to="body"><div v-if="editing" class="note-editor-backdrop" @click.self="close" @keydown="keys"><section ref="panel" class="note-editor-panel" role="dialog" aria-modal="true" :aria-labelledby="'note-editor-' + row.id"><header><h2 :id="'note-editor-' + row.id">编辑窗口</h2><button class="button ghost icon-button" title="关闭窗口编辑" aria-label="关闭窗口编辑" @click="close">×</button></header><form @submit.prevent="save"><label>窗口别名<input ref="aliasField" v-model="alias" maxlength="200" placeholder="留空则使用自动标题"></label><label>窗口备注<textarea v-model="note" maxlength="1000" rows="4" placeholder="记录该窗口的用途或说明"></textarea></label><div class="editor-actions"><button class="button primary" type="submit" :disabled="saving">保存</button><button class="button ghost" type="button" @click="close">取消</button></div></form></section></div></teleport>`
  };
  window.BoardUI = { number, compact, contextTokens, hasContext, contextRole, statusColors, money, sumMoney, percent, duration, bytes, totalTokens, vendors, vendorOf, modelColor, shortModel, billingLabel, severity, format, priceMissing, cellText, cellTitle, SortTable, InlineNote, SessionNoteEditor };
})();
