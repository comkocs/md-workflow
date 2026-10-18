/* 运行看板入口：无构建链，本地依赖，本地样例与真实接口共用视图。 */
(() => {
  'use strict';
  const { createApp, ref, reactive, computed, onMounted, onBeforeUnmount } = Vue;
  const U = BoardUI, C = BoardCharts;
  const isMock = new URLSearchParams(location.search).get('mock') === '1';
  const mockNotes = { projects: new Map(), sessions: new Map() };
  const routeFromHash = () => {
    try {
      const url = new URL(location.hash.slice(1) || '/', location.origin);
      const parts = url.pathname.split('/').filter(Boolean);
      return { page: parts[0] === 'project' ? 'project' : parts[0] === 'session' ? 'session' : parts[0] === 'models' ? 'models' : parts[0] === 'stats' ? 'stats' : 'overview', id: decodeURIComponent(parts[1] || ''), project: url.searchParams.get('project') || '' };
    } catch (_) { return { page: 'overview', id: '', project: '' }; }
  };
  const applyNotes = (path, payload) => {
    if (path === '/api/projects') return payload.map(row => ({ ...row, ...mockNotes.projects.get(row.id) }));
    if (path === '/api/sessions') return payload.map(row => ({ ...row, ...mockNotes.sessions.get(row.id) }));
    if (path.startsWith('/api/session/')) return { ...payload, summary: { ...payload.summary, ...mockNotes.sessions.get(payload.summary.id) } };
    return payload;
  };
  const request = async (path, { signal, method = 'GET', body } = {}) => {
    const url = new URL(path, location.origin);
    if (isMock && method === 'POST') {
      const match = url.pathname.match(/^\/api\/(projects|sessions)\/([^/]+)\/note$/);
      if (!match) throw new Error('样例模式不支持该操作');
      const response = await fetch('mock/note-ok.json', { signal, cache: 'no-store', credentials: 'same-origin' });
      if (!response.ok) throw new Error('样例确认数据读取失败');
      const payload = await response.json();
      if (!payload.ok) throw new Error('样例未确认保存');
      const store = mockNotes[match[1]], id = decodeURIComponent(match[2]);
      store.set(id, { ...store.get(id), ...body });
      return payload;
    }
    let target = new URL(path.slice(1), new URL('.', location.href));
    if (isMock) {
      const routes = { '/api/status': 'mock/status.json', '/api/projects': 'mock/projects.json', '/api/models': 'mock/models.json', '/api/stats': 'mock/stats.json' };
      target = routes[url.pathname];
      if (url.pathname === '/api/sessions') target = `mock/sessions/${encodeURIComponent(url.searchParams.get('project') || '')}.json`;
      if (url.pathname.startsWith('/api/session/')) target = `mock/session/${encodeURIComponent(decodeURIComponent(url.pathname.slice('/api/session/'.length)))}.json`;
      if (!target) throw new Error('未找到对应样例接口');
    }
    const response = await fetch(target, { signal, method, cache: 'no-store', credentials: 'same-origin', headers: method === 'POST' ? { 'Content-Type': 'application/json' } : {}, ...(body ? { body: JSON.stringify(body) } : {}) });
    if (!response.ok) { const error = new Error(`请求失败（状态码 ${response.status}）`); error.status = response.status; throw error; }
    const payload = await response.json();
    if (isMock && url.pathname === '/api/stats') return url.searchParams.has('project') ? payload.projects[url.searchParams.get('project')] || payload.empty : payload.global;
    return isMock ? applyNotes(url.pathname, payload) : payload;
  };
  const tokenColumns = [
    { key: 'input', label: '未缓存输入', format: 'tokens', numeric: true },
    { key: 'cache_read', label: '缓存读', format: 'tokens', numeric: true },
    { key: 'cache_write', label: '缓存写', format: 'tokens', numeric: true },
    { key: 'output', label: '输出', format: 'tokens', numeric: true },
    { key: 'hit_rate', label: '命中率', format: 'percent', numeric: true },
    { key: 'usd_paid', label: '实付美元', format: 'money', numeric: true },
    { key: 'usd_ref', label: '官方价参考美元', format: 'money', numeric: true }
  ];
  const app = createApp({
    components: { SortTable: U.SortTable, InlineNote: U.InlineNote, SessionNoteEditor: U.SessionNoteEditor, VizChart: C.VizChart, RadialTree: C.RadialTree },
    setup() {
      const route = ref(routeFromHash()), hours = ref(24), paused = ref(false), hidden = ref(document.hidden), loading = ref(false), initialized = ref(false), error = ref(''), notice = ref(''), status = ref(null);
      const projects = ref([]), sessions = ref([]), detail = ref(null), models = ref([]), selectedAgentId = ref(''), drawer = ref(null), saving = reactive(new Set());
      const stats = ref(null), statsUnavailable = ref(false), statsBasis = ref('tokens'), statsGroup = ref('by_project');
      const reducedMotion = ref(matchMedia('(prefers-reduced-motion: reduce)').matches);
      const layout = ref(localStorage.getItem('运行看板.版面') === '宽屏' ? '宽屏' : '标准');
      const tableMode = reactive({ tree: false, gantt: false, concurrency: false, hourly: false });
      let controller, refreshTimer, requestNumber = 0, noticeTimer, drawerPreviousFocus;
      const saveControllers = new Set();
      const selectedProject = computed(() => projects.value.find(project => project.id === (route.value.page === 'project' ? route.value.id : route.value.project)));
      const pageTitle = computed(() => route.value.page === 'overview' ? '项目总览' : route.value.page === 'models' ? '模型用量' : route.value.page === 'stats' ? (route.value.project ? `${selectedProject.value?.name || '本项目'} · 历史统计` : '全局历史统计') : route.value.page === 'project' ? selectedProject.value?.name || '项目窗口' : detail.value?.summary.alias || detail.value?.summary.title || '窗口详情');
      const projectStats = computed(() => ({ sessions: projects.value.reduce((sum, p) => sum + Number(p.sessions || 0), 0), running: projects.value.reduce((sum, p) => sum + Number(p.running || 0), 0), paid: U.sumMoney(projects.value, 'usd_paid'), sub: projects.value.reduce((sum, p) => sum + Number(p.tokens_sub || 0), 0), alerts: projects.value.reduce((sum, p) => sum + Number(p.alerts || 0), 0) }));
      const sessionRows = computed(() => sessions.value.map(row => ({ ...row, display_title: row.alias || row.title, period: `${row.start || '—'} → ${row.end || '在跑'}` })).sort((a, b) => Number(b.running) - Number(a.running) || (C.epoch(b.end) || 0) - (C.epoch(a.end) || 0)));
      const agents = computed(() => (detail.value?.agents || []).map(agent => ({ ...agent, models: agent.models || [], gaps: agent.gaps || [], long_calls: agent.long_calls || [], total_tokens: U.totalTokens(agent) })));
      const selectedAgent = computed(() => agents.value.find(agent => agent.id === selectedAgentId.value));
      const ganttOption = computed(() => C.gantt(agents.value));
      const concurrencyOption = computed(() => C.concurrency(detail.value?.concurrency || []));
      const ganttHeight = computed(() => Math.max(260, agents.value.length * 32 + 100));
      const hourlyCards = computed(() => U.vendors.map(vendor => ({ ...vendor, option: C.hourly(models.value, vendor), rows: models.value.filter(model => U.vendorOf(model.model).id === vendor.id).flatMap(model => (model.hourly || []).map(point => ({ ...point, model: model.model }))) })));
      const modelTotals = computed(() => ({ tokens: models.value.reduce((sum, model) => sum + U.totalTokens(model), 0), paid: U.sumMoney(models.value, 'usd_paid'), reference: U.sumMoney(models.value, 'usd_ref') }));
      const statsTotals = computed(() => stats.value?.totals || {});
      const statsModels = computed(() => C.modelOrder(stats.value?.models || []));
      const modelDistribution = computed(() => C.distribution(statsModels.value.map(row => ({ name: row.model, value: row[statsBasis.value], color: U.modelColor(row.model) })), statsBasis.value === 'usd' ? '金额' : 'Token', statsBasis.value === 'usd'));
      const activeGroup = computed(() => route.value.project && statsGroup.value === 'by_project' ? 'by_agent_type' : statsGroup.value);
      const statsGroups = computed(() => (stats.value?.groups?.[activeGroup.value] || []).map(row => ({ ...row, name: row.name || row.type || row.role || row.id })));
      const groupChartRows = computed(() => C.groupSlices(statsGroups.value, activeGroup.value));
      const groupDistribution = computed(() => C.distribution(groupChartRows.value, 'Token', false));
      const statsTrend = computed(() => C.statsTrend(stats.value?.trend || {}, statsModels.value));
      const tokenByVendor = computed(() => U.vendors.map(vendor => ({ name: vendor.name, tokens: (stats.value?.models || []).filter(row => U.vendorOf(row.model).id === vendor.id).reduce((sum, row) => sum + Number(row.tokens || 0), 0) })).filter(row => row.tokens > 0));
      const rangeText = computed(() => hours.value === 0 ? '全部历史' : `近 ${hours.value} 小时`);
      const readStats = async (range, project, signal) => {
        try { return { data: await request(`/api/stats?hours=${range}${project ? '&project=' + encodeURIComponent(project) : ''}`, { signal }), unavailable: false }; }
        catch (exception) { if ([404, 405, 501].includes(exception.status)) return { data: null, unavailable: true }; throw exception; }
      };
      const indexing = computed(() => {
        const current = status.value?.indexing;
        if (current?.active !== true) return null;
        const total = Math.max(0, Number(current.bytes_total) || 0), done = Math.max(0, Number(current.bytes_done) || 0);
        return { percent: total > 0 ? Math.max(0, Math.min(100, Math.floor(done / total * 100))) : null, doneFiles: Math.max(0, Number(current.done_files) || 0), totalFiles: Math.max(0, Number(current.total_files) || 0) };
      });
      const refreshLabel = computed(() => hidden.value ? '标签页隐藏 · 已停拉' : paused.value ? '已暂停自动刷新' : loading.value ? '正在读取最新快照' : '每 10 秒自动刷新');
      const refresh = async () => {
        if (document.hidden) return;
        const serial = ++requestNumber;
        controller?.abort(); controller = new AbortController();
        const activeController = controller;
        const snapshot = { ...route.value }, range = hours.value;
        const endpoint = snapshot.page === 'overview' ? `/api/projects?hours=${range}` : snapshot.page === 'project' ? `/api/sessions?project=${encodeURIComponent(snapshot.id)}&hours=${range}` : snapshot.page === 'session' ? `/api/session/${encodeURIComponent(snapshot.id)}` : snapshot.page === 'stats' ? null : `/api/models?hours=${range}`;
        loading.value = true;
        const timeout = setTimeout(() => activeController.abort('timeout'), 18000);
        try {
          const [newStatus, payload, projectContext, statsResult] = await Promise.all([
            request('/api/status', { signal: activeController.signal }),
            endpoint ? request(endpoint, { signal: activeController.signal }) : Promise.resolve(null),
            snapshot.page === 'project' || snapshot.page === 'session' || snapshot.page === 'stats' && snapshot.project ? request(`/api/projects?hours=${range}`, { signal: activeController.signal }) : Promise.resolve(null),
            snapshot.page === 'overview' || snapshot.page === 'stats' ? readStats(range, snapshot.page === 'stats' ? snapshot.project : '', activeController.signal) : Promise.resolve(null)
          ]);
          if (serial !== requestNumber || document.hidden) return;
          status.value = newStatus;
          if (projectContext) projects.value = projectContext;
          if (statsResult) { stats.value = statsResult.data; statsUnavailable.value = statsResult.unavailable; }
          if (snapshot.page === 'overview') projects.value = payload.map(row => isMock ? { ...row, ...mockNotes.projects.get(row.id) } : row);
          if (snapshot.page === 'project') sessions.value = payload.map(row => isMock ? { ...row, ...mockNotes.sessions.get(row.id) } : row);
          if (snapshot.page === 'session') detail.value = payload;
          if (snapshot.page === 'models') models.value = payload;
          error.value = ''; initialized.value = true;
        } catch (exception) {
          if (serial !== requestNumber || document.hidden) return;
          if (activeController.signal.aborted && activeController.signal.reason !== 'timeout') return;
          error.value = activeController.signal.reason === 'timeout' ? '接口读取超时，已保留上次数据，可手动重试。' : `${exception.message || '暂时无法读取数据'}，已保留上次数据。`;
        } finally {
          clearTimeout(timeout);
          if (serial === requestNumber) loading.value = false;
        }
      };
      const flash = text => { notice.value = text; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { notice.value = ''; }, 4500); };
      const saveNote = async (kind, row, field, value) => {
        const key = `${kind}-${row.id}`;
        if (saving.has(key)) return;
        const changes = typeof field === 'object' ? field : { [field]: value };
        const body = kind === 'projects' ? { name: row.name || row.default_name || '', note: row.note || '', ...changes } : { alias: row.alias || '', note: row.note || '', ...changes };
        saving.add(key);
        const saveController = new AbortController(); saveControllers.add(saveController);
        const timer = setTimeout(() => saveController.abort(), 12000);
        try {
          const response = await request(`/api/${kind}/${encodeURIComponent(row.id)}/note`, { method: 'POST', body, signal: saveController.signal });
          if (!response.ok) throw new Error('接口未确认保存');
          Object.assign(row, body);
          if (kind === 'sessions') {
            const original = sessions.value.find(item => item.id === row.id);
            if (original) Object.assign(original, body);
            if (detail.value?.summary.id === row.id) Object.assign(detail.value.summary, body);
          } else {
            const original = projects.value.find(item => item.id === row.id);
            if (original) Object.assign(original, body);
          }
          flash(isMock ? '已更新样例内存，刷新整个页面后恢复。' : '已保存备注。');
        } catch (_) { error.value = '备注保存失败，未改变原数据，请重试。'; }
        finally { clearTimeout(timer); saveControllers.delete(saveController); saving.delete(key); }
      };
      const openProject = project => { location.hash = `/project/${encodeURIComponent(project.id)}`; };
      const openSession = session => { location.hash = `/session/${encodeURIComponent(session.id)}?project=${encodeURIComponent(route.value.id)}`; };
      const closeDrawer = () => { selectedAgentId.value = ''; document.body.classList.remove('drawer-open'); drawerPreviousFocus?.focus(); };
      const openAgent = async id => {
        if (!agents.value.some(agent => agent.id === id)) return;
        drawerPreviousFocus = document.activeElement; selectedAgentId.value = id;
        document.body.classList.add('drawer-open'); await Vue.nextTick(); drawer.value?.focus();
      };
      const routeChanged = () => {
        closeDrawer(); route.value = routeFromHash(); initialized.value = false; error.value = '';
        if (route.value.page === 'session') detail.value = null;
        if (route.value.page === 'project') sessions.value = [];
        stats.value = null; statsUnavailable.value = false;
        statsGroup.value = route.value.project ? 'by_agent_type' : 'by_project';
        refresh();
      };
      const visibilityChanged = () => {
        hidden.value = document.hidden;
        if (hidden.value) { requestNumber++; controller?.abort(); loading.value = false; }
        else if (!paused.value) refresh();
      };
      const rangeChanged = () => { refresh(); };
      const setLayout = value => { layout.value = value === '宽屏' ? '宽屏' : '标准'; try { localStorage.setItem('运行看板.版面', layout.value); } catch (_) {} };
      const togglePause = () => { paused.value = !paused.value; if (paused.value) { requestNumber++; controller?.abort(); loading.value = false; } else refresh(); };
      const trapDrawer = event => {
        if (event.key === 'Escape') { closeDrawer(); return; }
        if (event.key !== 'Tab') return;
        const items = [...drawer.value.querySelectorAll('button, a[href], input, textarea, [tabindex="0"]')].filter(el => !el.disabled);
        if (!items.length) { event.preventDefault(); return; }
        const first = items[0], last = items.at(-1);
        if (event.shiftKey && (document.activeElement === first || document.activeElement === drawer.value)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      };
      onMounted(() => {
        refresh(); refreshTimer = setInterval(() => { if (!paused.value && !document.hidden && !loading.value) refresh(); }, 10000);
        window.addEventListener('hashchange', routeChanged); document.addEventListener('visibilitychange', visibilityChanged);
      });
      onBeforeUnmount(() => {
        clearInterval(refreshTimer); clearTimeout(noticeTimer); controller?.abort(); saveControllers.forEach(control => control.abort());
        window.removeEventListener('hashchange', routeChanged); document.removeEventListener('visibilitychange', visibilityChanged); document.body.classList.remove('drawer-open');
      });
      const sessionColumns = [
        { key: 'display_title', label: '别名 / 自动标题', wide: true }, { key: 'note', label: '备注', wide: true }, { key: 'platform', label: '平台' }, { key: 'version', label: '版本' }, { key: 'start', label: '开始' }, { key: 'end', label: '结束' },
        { key: 'minutes', label: '时长', format: 'duration', numeric: true }, { key: 'running', label: '状态', format: 'boolean' }, { key: 'main_model', label: '主窗模型' }, { key: 'agents', label: '代理数', format: 'number', numeric: true }, { key: 'peak_ctx', label: '峰值上下文', format: 'tokens', numeric: true },
        { key: 'usd_paid', label: '实付美元', format: 'money', numeric: true }, { key: 'tokens_sub', label: '订阅用量', format: 'tokens', numeric: true }, { key: 'usd_ref', label: '官方价参考美元', format: 'money', numeric: true }, { key: 'alerts', label: '报警', format: 'number', numeric: true }
      ];
      const agentColumns = [{ key: 'title', label: '代理标题', wide: true }, { key: 'models', label: '模型' }, { key: 'depth', label: '层级', numeric: true }, { key: 'parent', label: '父代理' }, { key: 'start', label: '开始' }, { key: 'end', label: '结束' }, { key: 'minutes', label: '时长', format: 'duration', numeric: true }, { key: 'total_tokens', label: '总用量', format: 'tokens', numeric: true }, { key: 'running', label: '状态' }];
      const detailModelColumns = [{ key: 'model', label: '模型' }, { key: 'billing', label: '计费口径', format: 'billing' }, { key: 'agents', label: '代理数', format: 'number', numeric: true }, { key: 'turns', label: '回合数', format: 'number', numeric: true }, ...tokenColumns];
      const modelColumns = [{ key: 'model', label: '模型' }, { key: 'billing', label: '计费口径', format: 'billing' }, { key: 'requests', label: '请求数', format: 'number', numeric: true }, ...tokenColumns];
      const ticketColumns = [{ key: 'ticket', label: '工单 / 直派' }, { key: 'agents', label: '代理数', format: 'number', numeric: true }, { key: 'turns', label: '回合数', format: 'number', numeric: true }, ...tokenColumns];
      const groupColumns = key => [{ key, label: key === 'depth' ? '层级' : '代理类型' }, { key: 'agents', label: '代理数', format: 'number', numeric: true }, { key: 'turns', label: '回合数', format: 'number', numeric: true }, ...tokenColumns];
      const drawerFields = computed(() => {
        const a = selectedAgent.value;
        if (!a) return [];
        return [['代理标识', a.id], ['父代理', a.parent || '无（主窗）'], ['层级', a.depth], ['专用类型', a.type], ['完整标题', a.title], ['实际模型', a.models.join(' / ')], ['开始', a.start], ['结束', a.end || '尚未结束'], ['时长', U.duration(a.minutes)], ['回合数', U.number(a.turns)], ['未缓存输入', U.number(a.input)], ['输出', U.number(a.output)], ['缓存读', U.number(a.cache_read)], ['缓存写', U.number(a.cache_write)], ['命中率', U.percent(a.hit_rate)], ['实付美元', U.money(a.usd_paid)], ['官方价参考美元', a.price_missing || a.usd_ref == null ? '—（价目表缺此模型）' : U.money(a.usd_ref)], ['工具调用', U.number(a.tool_calls)], ['构建次数', U.number(a.builds)], ['测试次数', U.number(a.tests)], ['起客户端次数', U.number(a.launches)], ['按名杀进程次数', U.number(a.kill_by_name)], ['接口报错数', U.number(a.api_errors)], ...(U.hasContext(a) ? [['最近上下文', U.contextTokens(a.last_ctx)], ['上下文状态', a.ctx_level === 'warn' ? '警告' : a.ctx_level === 'severe' ? '严重' : '正常']] : []), ['峰值上下文', U.contextTokens(a.peak_ctx)], ['当前状态', a.running ? '在跑' : '已结束']];
      });
      const statsModelColumns = [{ key: 'model', label: '模型' }, { key: 'billing', label: '计费方式' }, { key: 'requests', label: '请求', numeric: true, format: 'number' }, { key: 'tokens', label: 'Token', numeric: true, format: 'tokens' }, ...['input', 'output', 'cache_read', 'cache_write'].map(key => tokenColumns.find(col => col.key === key)), { key: 'usd', label: '金额折算', numeric: true, format: 'money' }];
      const statsGroupColumns = [{ key: 'name', label: '分组' }, { key: 'requests', label: '请求', numeric: true, format: 'number' }, { key: 'tokens', label: 'Token', numeric: true, format: 'tokens' }, { key: 'usd', label: '金额折算', numeric: true, format: 'money' }];
      const statsTrendColumns = [{ key: 't', label: '时段' }, { key: 'requests', label: '请求', numeric: true, format: 'number' }, { key: 'tokens', label: 'Token', numeric: true, format: 'tokens' }, { key: 'usd', label: '金额折算', numeric: true, format: 'money' }];
      return { ...U, stats, statsUnavailable, statsBasis, statsGroup, statsTotals, statsModels, modelDistribution, activeGroup, statsGroups, groupChartRows, groupDistribution, statsTrend, tokenByVendor, rangeText, statsModelColumns, statsGroupColumns, statsTrendColumns, isMock, route, hours, paused, hidden, loading, initialized, error, notice, status, projects, sessions, detail, models, saving, reducedMotion, indexing, tableMode, drawer, layout, setLayout, selectedProject, selectedAgent, selectedAgentId, pageTitle, projectStats, sessionRows, agents, ganttOption, concurrencyOption, ganttHeight, hourlyCards, modelTotals, refreshLabel, refresh, saveNote, openProject, openSession, openAgent, closeDrawer, rangeChanged, togglePause, trapDrawer, sessionColumns, agentColumns, detailModelColumns, modelColumns, ticketColumns, groupColumns, drawerFields,
        gapColumns: [{ key: 'at', label: '失效时刻' }, { key: 'seconds', label: '间隔秒数', format: 'number', numeric: true }, { key: 'cache_write', label: '重新缓存用量', format: 'tokens', numeric: true }],
        callColumns: [{ key: 'at', label: '开始时刻' }, { key: 'seconds', label: '持续秒数', format: 'number', numeric: true }, { key: 'tool', label: '工具名' }, { key: 'cmd', label: '命令摘录', wide: true }],
        concurrencyColumns: [{ key: 't', label: '台面时刻' }, { key: 'n', label: '在跑代理数', format: 'number', numeric: true }],
        hourlyColumns: [{ key: 't', label: '台面时刻' }, { key: 'model', label: '模型' }, { key: 'tokens', label: '用量', format: 'tokens', numeric: true }]
      };
    }
  });
  app.config.errorHandler = () => {
    const host = document.getElementById('fatal-error');
    if (host) { host.hidden = false; host.textContent = '页面组件发生异常，请刷新重试；如仍异常，请检查本地前端文件与接口数据。'; }
  };
  app.mount('#app');
})();
