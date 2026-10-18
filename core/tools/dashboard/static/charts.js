/* 运行看板图表：颜色固定跟随厂商与模型，图表外另有表格视图。 */
(() => {
  'use strict';
  const { ref, computed, onMounted, onBeforeUnmount, watch } = Vue;
  const U = BoardUI;
  const surface = '#151b25', ink = '#edf2f9', muted = '#aab6c8', grid = '#293346';
  const fontSize = name => typeof getComputedStyle === 'function' ? parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 12 : 12;
  const smallFont = fontSize('--fs-xs'), bodyFont = fontSize('--fs-sm');
  const fontFamily = '"Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif';
  const tooltip = { renderMode: 'richText', backgroundColor: '#202c3d', borderColor: '#40516b', textStyle: { color: ink, fontSize: bodyFont, fontFamily, fontWeight: 400 }, confine: true };
  const safeText = value => String(value ?? '').replace(/[{}]/g, '');
  const motionDuration = () => {
    if (typeof getComputedStyle !== 'function') return 240;
    return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--motion-panel')) || 0;
  };
  const base = () => ({ backgroundColor: 'transparent', animationDuration: motionDuration(), animationDurationUpdate: motionDuration(), textStyle: { color: muted, fontSize: smallFont, fontFamily, fontWeight: 400 }, aria: { enabled: true }, tooltip: { ...tooltip } });
  const axes = () => ({ axisLine: { lineStyle: { color: grid } }, axisTick: { show: false }, axisLabel: { color: muted, fontSize: smallFont }, splitLine: { lineStyle: { color: grid, width: 1, type: 'solid' } } });
  const formatDeskTime = epoch => {
    const date = new Date(epoch + 8 * 3600000);
    return `${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')} ${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')}`;
  };
  const epoch = text => {
    if (!text) return NaN;
    const match = String(text).match(/^(\d{2})-(\d{2}) (\d{2}):(\d{2})$/);
    if (!match) return Date.parse(text);
    const now = Date.now(), year = new Date(now + 8 * 3600000).getUTCFullYear();
    const parts = match.slice(1).map(Number);
    let result = Date.UTC(year, parts[0] - 1, parts[1], parts[2] - 8, parts[3]);
    if (result > now + 7 * 86400000) result = Date.UTC(year - 1, parts[0] - 1, parts[1], parts[2] - 8, parts[3]);
    return result;
  };
  const contextLabel = agent => U.hasContext(agent) ? ` · 上下文 ${U.contextTokens(agent.last_ctx)}` : '';
  const richContextLabel = agent => {
    if (!U.hasContext(agent)) return '';
    const role = U.contextRole(agent);
    return role === 'normal' ? `{normal|${contextLabel(agent)}}` : ` {${role}|●} {normal|${role === 'warning' ? '警告' : '严重'}${contextLabel(agent)}}`;
  };
  const contextRich = { normal: { color: muted, fontFamily, fontWeight: 400 }, name: { color: ink, fontFamily, fontWeight: 500 }, warning: { color: U.statusColors.warning, fontFamily }, serious: { color: U.statusColors.serious, fontFamily } };
  const contextTooltip = agent => `${U.hasContext(agent) ? `\n最近上下文 ${U.contextTokens(agent.last_ctx)}${agent.ctx_level === 'warn' ? ' · 警告' : agent.ctx_level === 'severe' ? ' · 严重' : ''}` : ''}\n峰值上下文 ${U.contextTokens(agent.peak_ctx)}`;
  const VizChart = {
    props: { option: Object, height: { type: Number, default: 300 }, label: String, reducedMotion: Boolean },
    emits: ['select'],
    setup(props, { emit }) {
      const host = ref(null), error = ref(''), halos = ref([]), hidden = ref(document.hidden);
      let chart, observer, geometryFrame, lastSnapshot = '', lastGeometry = '';
      const syncHalos = () => {
        if (!chart || document.hidden) return;
        const next = [];
        chart.getModel().eachSeries(series => {
          if (!['agent-tree', 'gantt-bars'].includes(series.id)) return;
          const data = series.getData();
          data.each(index => {
            const raw = data.getRawDataItem(index);
            if (raw?.running !== true) return;
            const element = data.getItemGraphicEl(index);
            const collect = item => {
              if (!['path', 'rect'].includes(item.type) || item.ignore || item.invisible) return;
              const rect = item.getBoundingRect().clone(); rect.applyTransform(item.getComputedTransform());
              if (![rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) || rect.width <= 0 || rect.height <= 0) return;
              next.push({ id: `${series.id}-${raw.agentId}`, kind: series.id === 'agent-tree' ? 'node' : 'bar', style: { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px`, color: item.style.fill } });
            };
            if (element?.isGroup) element.traverse(collect); else if (element) collect(element);
          });
        });
        const snapshot = JSON.stringify(next);
        if (snapshot !== lastGeometry) { halos.value = next; lastGeometry = snapshot; }
      };
      const queueGeometry = () => {
        if (geometryFrame || document.hidden) return;
        geometryFrame = requestAnimationFrame(() => { geometryFrame = 0; syncHalos(); });
      };
      const visibilityChanged = () => {
        hidden.value = document.hidden;
        if (hidden.value) { cancelAnimationFrame(geometryFrame); geometryFrame = 0; }
        else queueGeometry();
      };
      const update = () => {
        if (!chart || !props.option) return;
        try {
          const snapshot = JSON.stringify(props.option);
          if (snapshot !== lastSnapshot) {
            chart.setOption({ ...props.option, animation: !props.reducedMotion }, { notMerge: false, lazyUpdate: false, replaceMerge: ['series'] });
            lastSnapshot = snapshot; queueGeometry();
          }
          error.value = '';
        } catch (_) { error.value = '图表暂时无法绘制，请查看表格数据。'; }
      };
      onMounted(() => {
        if (!window.echarts) { error.value = '本地图表库未加载，请检查第三方库文件。'; return; }
        chart = echarts.init(host.value, null, { renderer: 'svg' });
        chart.on('click', params => { const id = params.data?.agentId; if (id) emit('select', id); });
        chart.on('finished', queueGeometry); chart.on('treeroam', queueGeometry); chart.on('datazoom', queueGeometry); chart.on('treeexpandandcollapse', queueGeometry);
        observer = new ResizeObserver(() => { chart?.resize(); queueGeometry(); }); observer.observe(host.value);
        document.addEventListener('visibilitychange', visibilityChanged);
        update();
      });
      watch(() => props.option, update);
      watch(() => props.reducedMotion, () => {
        chart?.setOption({ animation: !props.reducedMotion }); queueGeometry();
      });
      watch(() => props.height, () => Vue.nextTick(() => { chart?.resize(); queueGeometry(); }));
      onBeforeUnmount(() => { cancelAnimationFrame(geometryFrame); document.removeEventListener('visibilitychange', visibilityChanged); observer?.disconnect(); chart?.dispose(); chart = null; });
      return { host, error, halos, hidden };
    },
    template: `<div class="viz-shell"><div class="viz-stage" :style="{height:height + 'px'}"><div ref="host" class="viz" :style="{height:height + 'px'}" role="img" :aria-label="label"></div><div class="running-overlay" :class="{'overlay-still':reducedMotion,'overlay-hidden':hidden}" aria-hidden="true"><span v-for="halo in halos" :key="halo.id" class="running-halo" :class="halo.kind" :data-agent="halo.id" :style="halo.style"></span></div></div><p v-if="error" class="chart-error" role="status">{{error}}</p></div>`
  };
  // 字宽取隐藏 span 与隐藏 SVG 文字两者较大值:与图里的 SVG 文字同受浏览器最小字号 / 字体缩放影响,canvas 量不到这层。
  let measureHost = null, measureSvgText = null;
  const textWidth = (text, weight = 400) => {
    if (typeof document !== 'object' || !document.body) return Array.from(text).length * smallFont;
    if (!measureHost) {
      measureHost = document.createElement('span');
      Object.assign(measureHost.style, { position: 'absolute', left: '-9999px', top: '0', visibility: 'hidden', whiteSpace: 'pre', fontFamily, fontSize: `${smallFont}px` });
      document.body.appendChild(measureHost);
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('aria-hidden', 'true');
      Object.assign(svg.style, { position: 'absolute', left: '-9999px', top: '0', width: '10px', height: '10px', visibility: 'hidden' });
      measureSvgText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      measureSvgText.setAttribute('style', `font-family:${fontFamily};font-size:${smallFont}px;white-space:pre`);
      svg.appendChild(measureSvgText); document.body.appendChild(svg);
    }
    measureHost.style.fontWeight = String(weight); measureHost.textContent = text;
    measureSvgText.style.fontWeight = String(weight); measureSvgText.textContent = text;
    let svgWidth = 0;
    try { svgWidth = measureSvgText.getBBox().width; } catch (_) { svgWidth = 0; }
    return Math.max(measureHost.getBoundingClientRect().width, svgWidth);
  };
  // 子孙关系用上下货架铺满宽舞台:子代理及后代仍排成气球花,小花在内、大花在外;中央干线与逐行通道隔开主窗走线。
  // 球径按字量定,三档下限主窗 > 子代理 > 孙代理及更深;层级另用描边粗细、字重、连线粗细区分;球底色亮度按本会话总用量对数区间。
  const radialTiers = [
    { min: 58, chars: 6, lines: 1, weight: 600, border: 3, name: '主窗' },
    { min: 52, chars: 6, lines: 3, weight: 600, border: 2.5, name: '子代理' },
    { min: 32, chars: 5, lines: 3, weight: 400, border: 1, name: '孙代理' },
  ];
  // gap = 球间留白;车道余量取最小车道间距,扇口按各圈半径为来线留走廊,不再固定角度。
  const radialGap = 14, radialPad = 9, radialLaneMargin = 3.5, radialEdgeColors = ['#7fa2dc', '#8b7b5e'];
  const widthCache = new Map();
  const measured = (text, weight = 400) => {
    const key = `${weight}|${text}`;
    if (!widthCache.has(key)) { if (widthCache.size > 6000) widthCache.clear(); widthCache.set(key, textWidth(text, weight)); }
    return widthCache.get(key);
  };
  const radialLine = () => { textWidth('国'); return Math.max(Math.round(smallFont * 1.32), Math.ceil(measureHost?.getBoundingClientRect().height || 0)); };
  // 按实测字宽折行:拉丁串整段不拆(超宽才逐字拆),超出行数末行加省略号。
  const wrapName = (text, maxWidth, maxLines, weight) => {
    const pieces = [];
    (String(text).trim().match(/[A-Za-z0-9_.:#/+-]+|\s+|\S/gu) || []).forEach(token => {
      if (token.length > 1 && !/\s/.test(token) && measured(token, weight) > maxWidth) pieces.push(...Array.from(token)); else pieces.push(token);
    });
    const lines = [];
    let line = '';
    pieces.forEach(token => {
      if (/^\s+$/.test(token)) { if (line) line += ' '; return; }
      if (!line.trim() || measured(line + token, weight) <= maxWidth) line += token;
      else { lines.push(line.trimEnd()); line = token; }
    });
    if (line.trim()) lines.push(line.trimEnd());
    if (lines.length <= maxLines) return lines;
    const kept = lines.slice(0, maxLines);
    let last = kept[maxLines - 1];
    while (last && measured(`${last}…`, weight) > maxWidth) last = Array.from(last).slice(0, -1).join('');
    kept[maxLines - 1] = `${last}…`;
    return kept;
  };
  // 通用多圈:各圈基线取全部内圈外沿加留白,每项按自身子树半径参差摆放;两两约束的最长路径给出最紧角度序列。
  const radialShells = (items, inner, arc, closed, shells, lineWidth) => {
    const n = items.length, outer = Math.max(...shells), ringCount = new Set(shells).size;
    const evaluate = (rho, pushedShell) => {
      const d = new Array(n);
      let extent = inner - radialGap;
      for (let shell = 0; shell <= outer; shell++) {
        const baseline = extent + radialGap;
        items.forEach((item, i) => {
          if (shells[i] !== shell) return;
          d[i] = Math.max(baseline + item.R, shell === pushedShell ? rho : 0);
          extent = Math.max(extent, d[i] + item.R);
        });
      }
      const alpha = items.map((item, i) => Math.asin(Math.min(1, (item.R + radialGap / 2) / d[i])));
      const constraint = (i, j) => shells[i] === shells[j] ? alpha[i] + alpha[j] : alpha[shells[i] < shells[j] ? i : j];
      // 各圈按实际距离计算走廊半角,再加子树圆的半角,所有子树都避开来线射线。
      const corridor = d.map(distance => closed ? 0 : Math.asin(Math.min(1, (lineWidth / 2 + radialLaneMargin + radialGap / 2) / distance)));
      const margin = alpha.map((angle, i) => closed ? 0 : angle + corridor[i]);
      const theta = [...margin];
      for (let j = 1; j < n; j++) for (let i = 0; i < j; i++) theta[j] = Math.max(theta[j], theta[i] + constraint(i, j));
      let total = Math.max(...theta.map((angle, i) => angle + margin[i]));
      // 扇口变窄后也必须约束跨首尾球与射线,不能只检查正向角差。
      for (let j = 1; j < n; j++) for (let i = 0; i < j; i++) total = Math.max(total, theta[j] - theta[i] + constraint(j, i));
      return { d, alpha, theta, total, extent, shells, ringCount, push: pushedShell === outer ? '最外圈' : '最内圈' };
    };
    const tight = evaluate(0, outer);
    if (tight.total <= arc) return tight;
    // 分别只推最外圈或最内圈。内圈推远后的位移已包含在外圈基线中,绝不再次叠加 rho。
    // 两种模式各自二分求最小可行半径,再取外接圆较小者;外圈模式可能被固定内圈限制而无解。
    const solve = pushedShell => {
      if (pushedShell === outer && evaluate(Infinity, pushedShell).total >= arc - 1e-9) return null;
      let lo = 0, hi = inner + Math.max(...items.map(item => item.R));
      for (let guard = 0; evaluate(hi, pushedShell).total > arc && guard < 48; guard++) { lo = hi; hi *= 2; }
      if (evaluate(hi, pushedShell).total > arc) return null;
      for (let step = 0; step < 40; step++) { const mid = (lo + hi) / 2; if (evaluate(mid, pushedShell).total > arc) lo = mid; else hi = mid; }
      return evaluate(hi, pushedShell);
    };
    const choices = (outer ? [outer, 0] : [outer]).map(solve).filter(Boolean);
    if (!choices.length) throw new Error('径向多圈未找到可行半径');
    return choices.reduce((best, next) => next.extent < best.extent ? next : best);
  };
  // 气球式布局:自底向上算每棵子树外接圆,本地坐标以「朝外 = 0 弧度」摆子节点,放置时整体旋转到所在方向。
  const radialArrange = node => {
    node.R = node.r; node.slots = []; node.layout = '叶子'; node.shells = [];
    if (!node.kids.length) return;
    node.kids.forEach(radialArrange);
    const closed = node.depth === 0, arc = Math.PI * 2, inner = node.r + radialGap, lineWidth = node.depth === 1 ? 2.5 : 1.2;
    const items = node.kids.map(kid => ({ R: kid.R })), types = node.kids.map(kid => kid.kids.length ? 1 : 0), leaves = types.filter(type => !type).length, mixed = leaves > 0 && leaves < items.length;
    const candidates = [{ name: '单圈', shells: items.map(() => 0) }];
    // 交错分圈保留时间顺序,仍由两两约束最长路径求角;不改球中文字来压缩花。
    for (let count = 2; count <= Math.min(4, items.length); count++) candidates.push({ name: `${count}圈交错`, shells: items.map((_, i) => i % count) });
    if (mixed) {
      candidates.push({ name: '按类型双圈', shells: types });
      [2, ...(leaves >= 30 ? [3] : [])].forEach(count => {
        let leaf = 0;
        candidates.push({ name: count === 2 ? '叶子双圈加子树外圈' : '叶子三圈加子树外圈', shells: types.map(type => type ? count : leaf++ % count) });
      });
    }
    let best;
    candidates.forEach(candidate => {
      const next = { ...radialShells(items, inner, arc, closed, candidate.shells, lineWidth), name: candidate.name };
      if (!best || next.extent < best.extent - 1e-6 || Math.abs(next.extent - best.extent) <= 1e-6 && next.ringCount > best.ringCount) best = next;
    });
    // 放满可用整周,保留两端走廊;少量孙代理也使用朝主窗一侧,不再只朝外铺扇面。
    const scale = best.total > 0 ? arc / best.total : 1;
    node.kids.forEach((kid, i) => node.slots.push({ kid, d: best.d[i], theta: -Math.PI + best.theta[i] * scale }));
    node.R = Math.max(node.r, best.extent); node.layout = best.name; node.shells = best.shells; node.push = best.push;
  };
  const radialPlace = (node, x, y, dir) => {
    node.x = x; node.y = y;
    node.slots.forEach(slot => { const angle = dir + slot.theta; radialPlace(slot.kid, x + slot.d * Math.cos(angle), y + slot.d * Math.sin(angle), slot.dir ?? angle); });
  };
  const radialTrim = (points, first, last) => {
    const result = points.filter((p, i) => !i || Math.hypot(p.x - points[i - 1].x, p.y - points[i - 1].y) > 1e-6).map(p => ({ ...p }));
    if (result.length < 2) return [];
    const a = result[0], b = result[1], y = result[result.length - 2], z = result[result.length - 1];
    const head = Math.hypot(b.x - a.x, b.y - a.y), tail = Math.hypot(z.x - y.x, z.y - y.y);
    if (head <= first || tail <= last || result.length === 2 && head <= first + last) return [];
    result[0] = { x: a.x + (b.x - a.x) * first / head, y: a.y + (b.y - a.y) * first / head };
    result[result.length - 1] = { x: z.x - (z.x - y.x) * last / tail, y: z.y - (z.y - y.y) * last / tail };
    return result;
  };
  // 花朝上定向后量全部球外沿,朝下时整体旋转半周即可复用真实框;不以空扇口补成 2R 方格。
  const radialFlowerBox = node => {
    radialPlace(node, 0, 0, -Math.PI / 2);
    const members = [], visit = item => { members.push(item); item.kids.forEach(visit); };
    visit(node);
    const left = Math.min(...members.map(item => item.x - item.r)), right = Math.max(...members.map(item => item.x + item.r));
    const top = Math.min(...members.map(item => item.y - item.r)), bottom = Math.max(...members.map(item => item.y + item.r));
    return { left, right, top, bottom, w: right - left, h: bottom - top };
  };
  // 每行左右交替装真实框,上、下逐行外扩。半行至少容纳最大花,宽度系数仍按全览字号择优。
  const radialShelves = (root, aspect, multiplier = 1) => {
    const kids = [...root.kids].sort((a, b) => a.R - b.R || (epoch(a.agent.start) || 0) - (epoch(b.agent.start) || 0));
    const targetWidth = Math.max(2 * Math.max(0, ...kids.map(kid => kid.flowerBox.w + radialGap)), multiplier * Math.sqrt(kids.reduce((sum, kid) => sum + (kid.flowerBox.w + radialGap) * (kid.flowerBox.h + radialGap), 0) * aspect));
    const rows = [];
    let row;
    kids.forEach(kid => {
      if (!row) { row = { sign: rows.length % 2 ? 1 : -1, sides: [[], []], used: [0, 0], height: 0 }; rows.push(row); }
      let side = (row.sides[0].length + row.sides[1].length) % 2;
      const cell = kid.flowerBox.w + radialGap;
      if (row.sides[side].length && row.used[side] + cell > targetWidth / 2) {
        row = { sign: rows.length % 2 ? 1 : -1, sides: [[], []], used: [0, 0], height: 0 }; rows.push(row); side = 0;
      }
      row.sides[side].push(kid); row.used[side] += cell; row.height = Math.max(row.height, kid.flowerBox.h);
    });
    // 上下束复用同一组 x;左右各占半束。奇数/不对称时留空车道,保证所有车道落在主窗直径的 85% 内。
    const counts = [-1, 1].map(sign => [0, 1].map(side => rows.filter(row => row.sign === sign).reduce((n, row) => n + row.sides[side].length, 0)));
    const lanes = 2 * Math.max(0, ...counts.flat());
    root.r = Math.max(root.r, lanes * 3.5 / 1.7);
    const spacing = Math.max(3.5, Math.min(7, 1.7 * root.r / Math.max(1, lanes))), trunk = lanes * spacing + 2 * radialGap;
    root.x = 0; root.y = 0; root.R = root.r; root.slots = []; root.shells = []; root.layout = '上下货架与中央干线';
    const edge = { '-1': root.r + 16, '1': root.r + 16 }, routes = [];
    rows.forEach(row => {
      const count = Math.max(...row.sides.map(side => side.length)), channel = count * spacing + 2 * radialGap;
      const near = edge[row.sign], inner = near + channel;
      row.sides.forEach((side, index) => {
        let used = trunk / 2;
        side.forEach((kid, i) => {
          const box = kid.flowerBox, left = row.sign < 0 ? box.left : -box.right, right = row.sign < 0 ? box.right : -box.left;
          const x = index ? used - left : -used - right, y = row.sign * (inner + box.bottom);
          // dir 表示花朝外,扇口恰好反向朝主窗:上花 -π/2,下花 +π/2。
          radialPlace(kid, x, y, row.sign * Math.PI / 2);
          routes.push({ kid, sign: row.sign, side: index, channelY: row.sign * (near + radialGap + (count - i) * spacing) });
          used += kid.flowerBox.w + radialGap;
        });
      });
      edge[row.sign] = inner + row.height;
    });
    // 近花用靠本行的通道;干线按离开先后由外向内分配,横段只穿过已结束的竖段,不重叠也不交叉。
    [-1, 1].forEach(sign => [0, 1].forEach(side => {
      const bundle = routes.filter(route => route.sign === sign && route.side === side).sort((a, b) => Math.abs(a.channelY) - Math.abs(b.channelY));
      bundle.forEach((route, i) => {
        const x = (side ? 1 : -1) * (bundle.length - i - 0.5) * spacing, kid = route.kid;
        const source = { x, y: sign * Math.sqrt(Math.max(0, root.r ** 2 - x ** 2)) }, target = { x: kid.x, y: kid.y - sign * kid.r };
        const points = Math.abs(x - kid.x) < 1e-6 ? [source, target] : [source, { x, y: route.channelY }, { x: kid.x, y: route.channelY }, target];
        root.slots.push({ kid, route: points });
      });
    }));
    return { targetWidth, rows: rows.length, lanes, spacing, trunk };
  };
  const radialBounds = nodes => {
    const aura = node => node.r + (node.depth === 0 ? 16 : node.depth === 1 ? 7 : 0);
    const minX = Math.min(...nodes.map(node => node.x - aura(node))), maxX = Math.max(...nodes.map(node => node.x + aura(node)));
    const minY = Math.min(...nodes.map(node => node.y - aura(node))), maxY = Math.max(...nodes.map(node => node.y + aura(node)));
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  };
  const radialPath = points => {
    let path = `M ${points[0].x} ${points[0].y}`;
    for (let i = 1; i < points.length; i++) {
      const p = points[i], before = points[i - 1], after = points[i + 1];
      if (!after) { path += ` L ${p.x} ${p.y}`; continue; }
      const incoming = Math.hypot(p.x - before.x, p.y - before.y), outgoing = Math.hypot(after.x - p.x, after.y - p.y), radius = Math.min(6, incoming / 2, outgoing / 2);
      const a = { x: p.x - (p.x - before.x) * radius / incoming, y: p.y - (p.y - before.y) * radius / incoming }, b = { x: p.x + (after.x - p.x) * radius / outgoing, y: p.y + (after.y - p.y) * radius / outgoing };
      path += ` L ${a.x} ${a.y} Q ${p.x} ${p.y} ${b.x} ${b.y}`;
    }
    return path;
  };
  const radialLayoutCache = new Map();
  const radialView = (agents, uid, aspect = 16 / 9) => {
    if (!agents.length) return { nodes: [], edges: [], gradients: [], bbox: { x: -100, y: -100, w: 200, h: 200 } };
    const started = performance.now();
    const byId = new Map(agents.map(agent => [agent.id, agent]));
    const center = byId.get('main') || agents[0];
    const order = agent => { const at = epoch(agent.start); return Number.isFinite(at) ? at : Infinity; };
    const kidsOf = new Map();
    agents.forEach(agent => { if (agent !== center && byId.has(agent.parent) && agent.parent !== agent.id) (kidsOf.get(agent.parent) || kidsOf.set(agent.parent, []).get(agent.parent)).push(agent); });
    // 从中心走一遍;父链断或成环的代理挂到中心下,保证每个代理都上图。
    const reached = new Set([center.id]), queue = [center];
    for (let i = 0; i < queue.length; i++) (kidsOf.get(queue[i].id) || []).forEach(kid => { if (!reached.has(kid.id)) { reached.add(kid.id); queue.push(kid); } });
    const orphans = agents.filter(agent => !reached.has(agent.id));
    const logs = agents.map(agent => Math.log10(1 + U.totalTokens(agent))).filter(value => value > 0);
    const lo = logs.length ? Math.min(...logs) : 0, hi = logs.length ? Math.max(...logs) : 0;
    const line = radialLine(), cjk = measured('国'), titleSize = Math.round(bodyFont * 1.3), gradients = new Map(), nodes = [];
    const build = (agent, depth, seen) => {
      seen.add(agent.id);
      const tier = radialTiers[Math.min(depth, 2)], tierIndex = Math.min(depth, 2);
      const title = safeText(agent.title || agent.type).replace(/^\s*《[^》]*》\s*/, '') || '未命名代理';
      const model = agent.models.length ? U.shortModel(agent.models[0]) + (agent.models.length > 1 ? ' 等' : '') : '未知模型';
      const role = U.hasContext(agent) ? U.contextRole(agent) : 'normal';
      const meta = `${model}${U.hasContext(agent) ? ` · ${U.contextTokens(agent.last_ctx)}` : ''}`;
      const nameLines = depth === 0 && agent.id === 'main' ? ['主窗'] : wrapName(Array.from(title).slice(0, 30).join(''), tier.chars * cjk, depth === 0 ? 2 : tier.lines, tier.weight);
      const nameSize = depth === 0 ? titleSize : depth === 1 ? bodyFont + 1 : smallFont, nameHeight = Math.round(line * nameSize / smallFont);
      const rows = nameLines.map(text => ({ text, size: nameSize, weight: tier.weight, fill: ink, height: nameHeight, width: measured(text, tier.weight) * nameSize / smallFont, parts: null }));
      const metaParts = role === 'normal' ? [{ text: meta, fill: muted }] : [{ text: '● ', fill: U.statusColors[role] }, { text: meta, fill: muted }];
      rows.push({ size: smallFont, weight: 400, height: line + 2, width: measured(metaParts.map(part => part.text).join('')), parts: metaParts });
      const total = rows.reduce((sum, row) => sum + row.height, 0);
      let y = -total / 2, need = 0;
      rows.forEach(row => { need = Math.max(need, Math.hypot(row.width / 2, Math.max(Math.abs(y), Math.abs(y + row.height)))); row.y = Math.round((y + row.height / 2 + (row.parts ? 1 : 0)) * 10) / 10; y += row.height; });
      const color = U.modelColor(agent.models[0]);
      if (!gradients.has(color)) gradients.set(color, `${uid}-g${gradients.size}`);
      const usage = Math.log10(1 + U.totalTokens(agent));
      const node = { id: agent.id, agent, depth, tier: tierIndex, tierName: depth === 0 ? '主窗' : depth === 1 ? '子代理' : depth === 2 ? '孙代理' : `第 ${depth} 层代理`, title, r: Math.ceil(Math.max(tier.min, need + radialPad)), border: tier.border, color, gradient: gradients.get(color), tint: Math.round((0.5 + 0.5 * (usage > 0 && hi > lo ? (usage - lo) / (hi - lo) : 0.5)) * 100) / 100, rows, running: agent.running === true, kids: [] };
      node.aria = `${node.tierName} ${title} · ${meta}${role === 'normal' ? '' : role === 'warning' ? ' · 上下文警告' : ' · 上下文严重'}`;
      nodes.push(node);
      const kids = [...(kidsOf.get(agent.id) || []), ...(depth === 0 ? orphans : [])].filter(kid => !seen.has(kid.id)).sort((a, b) => order(a) - order(b));
      kids.forEach(kid => { if (!seen.has(kid.id)) node.kids.push(build(kid, depth + 1, seen)); });
      return node;
    };
    const root = build(center, 0, new Set());
    const key = JSON.stringify([aspect, nodes.map(node => [node.id, node.agent.parent, node.r, node.agent.start])]);
    let cached = radialLayoutCache.get(key), metrics;
    if (cached) {
      const lookup = new Map(nodes.map(node => [node.id, node]));
      cached.geometry.forEach(saved => { const node = lookup.get(saved.id); Object.assign(node, saved, { slots: saved.slots.map(slot => ({ ...slot, kid: lookup.get(slot.kid) })) }); });
      metrics = { ...cached.metrics, cacheHit: true };
    } else {
      root.kids.forEach(radialArrange);
      root.kids.forEach(kid => { kid.flowerBox = radialFlowerBox(kid); });
      const internalMs = performance.now() - started;
      // 舞台尺寸归一化成 aspect × 1,评分与 min(舞台宽/图宽,舞台高/图高) 等价。
      // 候选各自从文字所需主窗半径起算,避免上一个候选撑大的主窗污染下一版。
      const baseRadius = root.r, candidates = [];
      let best;
      for (const multiplier of [1, 1.2, 1.4, 1.6, 1.8, 2.2]) {
        root.r = baseRadius;
        const shelves = radialShelves(root, aspect, multiplier), bbox = radialBounds(nodes), score = Math.min(aspect / bbox.w, 1 / bbox.h);
        candidates.push({ multiplier, bbox, score });
        if (!best || score > best.score + 1e-12) best = { multiplier, score, shelves, geometry: nodes.map(node => ({ x: node.x, y: node.y, r: node.r })), slots: root.slots };
      }
      nodes.forEach((node, i) => Object.assign(node, best.geometry[i])); root.slots = best.slots; root.R = root.r;
      metrics = { aspect, internalMs, totalMs: performance.now() - started, ...best.shelves, multiplier: best.multiplier, candidates, selected: '上下货架与中央干线', cacheHit: false };
      cached = { metrics, geometry: nodes.map(node => ({ id: node.id, r: node.r, x: node.x, y: node.y, layout: node.layout, shells: node.shells, push: node.push, slots: node.slots.map(slot => ({ ...slot, kid: slot.kid.id })) })) };
      if (radialLayoutCache.size >= 24) radialLayoutCache.delete(radialLayoutCache.keys().next().value);
      radialLayoutCache.set(key, cached);
    }
    const edges = [];
    nodes.forEach(node => node.slots.forEach(({ kid, route }) => {
      const points = route || radialTrim([{ x: node.x, y: node.y }, { x: kid.x, y: kid.y }], node.r + 3, kid.r + 3), main = kid.depth === 1;
      edges.push({ id: `${node.id}>${kid.id}`, from: node.id, to: kid.id, main, points, path: radialPath(points), color: radialEdgeColors[main ? 0 : 1], width: main ? 2.5 : 1.2, opacity: main ? 0.85 : 0.8 });
    }));
    metrics = { ...metrics, renderMs: performance.now() - started };
    nodes.forEach(node => { node.transform = `translate(${Math.round(node.x * 10) / 10} ${Math.round(node.y * 10) / 10})`; delete node.kids; delete node.slots; });
    return { nodes, edges, metrics, gradients: [...gradients].map(([color, id]) => ({ id, color })), bbox: radialBounds(nodes) };
  };
  let radialSeq = 0;
  const RadialTree = {
    props: { agents: { type: Array, default: () => [] }, label: String, reducedMotion: Boolean },
    emits: ['select'],
    setup(props, { emit }) {
      const stage = ref(null), width = ref(0), height = ref(480), k = ref(1), tx = ref(0), ty = ref(0), tip = ref(null), hidden = ref(document.hidden), dragging = ref(false), error = ref('');
      const uid = `radial${++radialSeq}`, pad = 28, aspect = ref(16 / 9);
      const maxHeight = () => Math.max(640, (window.innerHeight || 900) - 120);
      const view = computed(() => {
        try { error.value = ''; return radialView(props.agents, uid, aspect.value); } catch (_) { error.value = '关系图暂时无法绘制，请查看表格数据。'; return radialView([], uid); }
      });
      let observer, drag = null, moved = false, lastIds = new Set(), layoutWidth = 0, lastMaxHeight = 0;
      // 舞台高度是输出,不参与重排触发;宽度按上次布局值累计超过 5% 才换缓存键。
      // 换键后只由 watch(view) 复位,避免 resize 与 watch 对同次布局重复复位。
      const syncSize = () => {
        const next = stage.value?.clientWidth || 0, cap = maxHeight();
        if (!next || next === width.value && cap === lastMaxHeight) return;
        width.value = next; lastMaxHeight = cap;
        if (!layoutWidth || Math.abs(next / layoutWidth - 1) >= 0.05) {
          layoutWidth = next;
          const ratio = next / cap;
          if (ratio !== aspect.value) { aspect.value = ratio; return; }
        }
        if (!moved) resetView();
      };
      const fitScale = () => { const box = view.value.bbox; return Math.min((width.value - 2 * pad) / box.w, (height.value - 2 * pad) / box.h); };
      const frame = (scale, focusX, focusY) => { k.value = scale; tx.value = width.value / 2 - scale * focusX; ty.value = height.value / 2 - scale * focusY; };
      // 初始视图:能放下就整图居中(最多原大);放不下按 0.5 倍起、以主窗为中心,其余拖动或「全览」看。
      const resetView = () => {
        const box = view.value.bbox, natural = Math.min(1, (width.value - 2 * pad) / box.w, (maxHeight() - 2 * pad) / box.h), scale = Math.max(0.5, natural);
        height.value = Math.round(Math.min(maxHeight(), Math.max(420, box.h * scale + 2 * pad)));
        if (scale > natural) frame(scale, 0, 0); else frame(scale, box.x + box.w / 2, box.y + box.h / 2);
        moved = false;
      };
      const zoomAt = (x, y, factor) => {
        const next = Math.min(3, Math.max(Math.min(0.2, fitScale() * 0.8), k.value * factor));
        tx.value = x - (x - tx.value) * next / k.value; ty.value = y - (y - ty.value) * next / k.value; k.value = next; moved = true; tip.value = null;
      };
      const fitAll = () => { const box = view.value.bbox; frame(Math.min(3, fitScale()), box.x + box.w / 2, box.y + box.h / 2); moved = true; };
      const actualSize = () => zoomAt(width.value / 2, height.value / 2, 1 / k.value);
      const halos = computed(() => view.value.nodes.filter(node => node.running).map(node => {
        const r = (node.r + 4) * k.value;
        return { id: node.id, style: { left: `${tx.value + node.x * k.value - r}px`, top: `${ty.value + node.y * k.value - r}px`, width: `${r * 2}px`, height: `${r * 2}px`, color: node.color } };
      }));
      const local = event => { const rect = stage.value.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top }; };
      const showTip = (node, event) => {
        if (dragging.value) return;
        const at = local(event), agent = node.agent;
        const text = `${node.tierName} · ${node.title}\n${agent.models.map(safeText).join(' / ') || '未知模型'}\n合计用量 ${U.number(U.totalTokens(agent))}${contextTooltip(agent)}\n点击打开代理详情`;
        tip.value = { text, style: at.x > width.value * 0.6 ? { right: `${width.value - at.x + 16}px`, top: `${at.y + 16}px` } : { left: `${at.x + 16}px`, top: `${at.y + 16}px` } };
      };
      const down = event => {
        if (event.button !== 0 || event.target.closest('.radial-tools')) return;
        drag = { x: event.clientX, y: event.clientY, tx: tx.value, ty: ty.value, id: event.target.closest('.radial-node')?.dataset.agent || '' };
        stage.value.setPointerCapture?.(event.pointerId);
      };
      const move = event => {
        if (!drag) return;
        const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
        if (!dragging.value && Math.hypot(dx, dy) < 5) return;
        dragging.value = true; tip.value = null; moved = true;
        tx.value = drag.tx + dx; ty.value = drag.ty + dy;
      };
      const up = event => {
        if (!drag) return;
        const click = !dragging.value && event.type === 'pointerup' ? drag.id : '';
        drag = null; dragging.value = false;
        if (click) emit('select', click);
      };
      const wheel = event => { event.preventDefault(); const at = local(event); zoomAt(at.x, at.y, Math.exp(-event.deltaY * 0.0015)); };
      const background = event => { if (!event.target.closest('.radial-node, .radial-tools')) fitAll(); };
      const visibilityChanged = () => { hidden.value = document.hidden; };
      watch(view, next => {
        const ids = new Set(next.nodes.map(node => node.id)), kept = [...lastIds].filter(id => ids.has(id)).length;
        if (!moved || kept < lastIds.size / 2) resetView();
        lastIds = ids;
      });
      onMounted(() => {
        syncSize(); resetView(); lastIds = new Set(view.value.nodes.map(node => node.id));
        observer = new ResizeObserver(syncSize);
        observer.observe(stage.value);
        window.addEventListener('resize', syncSize);
        stage.value.addEventListener('wheel', wheel, { passive: false });
        document.addEventListener('visibilitychange', visibilityChanged);
      });
      onBeforeUnmount(() => { observer?.disconnect(); window.removeEventListener('resize', syncSize); stage.value?.removeEventListener('wheel', wheel); document.removeEventListener('visibilitychange', visibilityChanged); });
      const select = id => emit('select', id);
      return { stage, width, height, k, tx, ty, tip, hidden, dragging, error, view, halos, showTip, down, move, up, background, fitAll, actualSize, zoomIn: () => zoomAt(width.value / 2, height.value / 2, 1.25), zoomOut: () => zoomAt(width.value / 2, height.value / 2, 0.8), select, edgeColors: radialEdgeColors, tiers: radialTiers };
    },
    template: `<div class="viz-shell"><div ref="stage" class="viz-stage radial-stage" :class="{dragging}" :data-metrics="JSON.stringify(view.metrics)" :style="{height:height + 'px'}" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="up" @dblclick="background" @pointerleave="tip = null"><p v-if="error || !view.nodes.length" class="chart-error radial-empty" role="status">{{error || '所选范围内暂无代理'}}</p><svg v-else class="radial-svg" :width="width" :height="height" role="img" :aria-label="label"><defs><radialGradient v-for="g in view.gradients" :key="g.id" :id="g.id" cx="50%" cy="50%" r="50%" fx="66%" fy="30%"><stop offset="0%" :stop-color="g.color" stop-opacity=".62"></stop><stop offset="100%" :stop-color="g.color" stop-opacity=".14"></stop></radialGradient></defs><g :transform="'translate(' + tx + ' ' + ty + ') scale(' + k + ')'"><g class="radial-edges"><path v-for="e in view.edges" :key="e.id" :d="e.path" :data-from="e.from" :data-to="e.to" :data-main="e.main" :data-points="JSON.stringify(e.points)" :stroke="e.color" :stroke-width="e.width" :stroke-opacity="e.opacity"></path></g><g v-for="n in view.nodes" :key="n.id" class="radial-node" :class="'tier-' + n.tier" :transform="n.transform" :data-agent="n.id" :data-layout="n.layout" :data-shells="n.shells.join(',')" :data-push="n.push" tabindex="0" role="button" :aria-label="n.aria" @keydown.enter.prevent="select(n.id)" @keydown.space.prevent="select(n.id)" @pointerenter="showTip(n, $event)" @pointermove="showTip(n, $event)" @pointerleave="tip = null"><template v-if="n.tier === 0"><circle class="radial-aura outer" :r="n.r + 15" :stroke="n.color"></circle><circle class="radial-aura" :r="n.r + 7" :stroke="n.color"></circle></template><circle v-if="n.tier === 1" class="radial-aura child" :r="n.r + 6" :stroke="n.color"></circle><circle class="radial-focus" :r="n.r + 4"></circle><circle class="radial-base" :r="n.r"></circle><circle :r="n.r" :fill="'url(#' + n.gradient + ')'" :opacity="n.tint"></circle><circle class="radial-ring" :class="{'radial-ring-soft':n.tier === 2}" :r="n.r" :stroke="n.color" :stroke-width="n.border"></circle><text class="radial-text" text-anchor="middle"><tspan v-for="(row, i) in n.rows" :key="i" x="0" :y="row.y" dominant-baseline="central" :font-size="row.size" :font-weight="row.weight" :fill="row.fill"><template v-if="row.parts"><tspan v-for="(part, j) in row.parts" :key="j" :fill="part.fill">{{part.text}}</tspan></template><template v-else>{{row.text}}</template></tspan></text></g></g></svg><div class="running-overlay" :class="{'overlay-still':reducedMotion,'overlay-hidden':hidden}" aria-hidden="true"><span v-for="halo in halos" :key="halo.id" class="running-halo node" :data-agent="halo.id" :style="halo.style"></span></div><div class="radial-legend" aria-hidden="true"><span><i class="tier-0"></i>主窗</span><span><i class="tier-1"></i>子代理</span><span><i class="tier-2"></i>孙代理及更深</span><span><i class="radial-line main" :style="{borderColor:edgeColors[0]}"></i>主窗连线</span><span><i class="radial-line child" :style="{borderColor:edgeColors[1]}"></i>子孙连线</span></div><div class="radial-tools"><button class="button ghost small" @click="fitAll">全览</button><button class="button ghost small" @click="actualSize">原大</button><button class="button ghost small" aria-label="放大" @click="zoomIn">+</button><button class="button ghost small" aria-label="缩小" @click="zoomOut">−</button></div><div v-if="tip" class="radial-tip" :style="tip.style">{{tip.text}}</div></div></div>`
  };
  const gantt = agents => {
    const sorted = [...agents].sort((a, b) => (epoch(a.start) || 0) - (epoch(b.start) || 0));
    const labels = new Map(sorted.map(agent => {
      const title = safeText(agent.title || agent.type).replace(/^\s*《[^》]*》\s*/, '');
      return [agent.id, `${agent.id === 'main' ? '主窗' : '└ ' + agent.depth + ' 层'} · ${Array.from(title).slice(0, 14).join('')} · ${agent.models.map(U.shortModel).join(' / ') || '未知模型'}${contextLabel(agent)}`];
    }));
    const richLabels = new Map(sorted.map(agent => {
      const plain = labels.get(agent.id), suffix = contextLabel(agent);
      return [agent.id, (suffix ? plain.slice(0, -suffix.length) : plain) + richContextLabel(agent)];
    }));
    const labelWidth = Math.max(330, ...[...labels.values()].map(label => Array.from(label).reduce((width, char) => width + (/[^\x00-\x7f]/.test(char) ? smallFont : smallFont * 0.65), 80)));
    const fullLabel = id => {
      const agent = sorted.find(item => item.id === id);
      return agent ? `${safeText(agent.title)}\n${agent.models.map(safeText).join(' / ')}${contextTooltip(agent)}` : '';
    };
    const bars = sorted.map((agent, index) => {
      const start = epoch(agent.start), finish = epoch(agent.end);
      const end = Number.isFinite(finish) ? finish : start + Math.max(1, Number(agent.minutes) || 1) * 60000;
      return { value: [index, start, Math.max(start + 60000, end)], agentId: agent.id, running: agent.running === true, last_ctx: agent.last_ctx, peak_ctx: agent.peak_ctx, ctx_level: agent.ctx_level, name: agent.title, itemStyle: { color: U.modelColor(agent.models[0]) } };
    }).filter(item => Number.isFinite(item.value[1]));
    const gaps = [], calls = [];
    sorted.forEach((agent, index) => {
      agent.gaps?.forEach(gap => { if (Number.isFinite(epoch(gap.at))) gaps.push({ value: [epoch(gap.at), index], agentId: agent.id, name: `失效 · ${U.number(gap.seconds)} 秒` }); });
      agent.long_calls?.forEach(call => { if (Number.isFinite(epoch(call.at))) calls.push({ value: [epoch(call.at), index], agentId: agent.id, name: `长调用 · ${U.number(call.seconds)} 秒 · ${safeText(call.tool)}` }); });
    });
    return { ...base(), dataZoom: [{ type: 'inside', xAxisIndex: 0, filterMode: 'weakFilter', zoomOnMouseWheel: true, moveOnMouseMove: true }], grid: { left: labelWidth, right: 28, top: 38, bottom: 50 }, legend: { data: ['失效', '长调用'], textStyle: { color: muted }, top: 0 }, tooltip: { ...tooltip, trigger: 'item', formatter: params => {
      if (params.componentType === 'yAxis') return fullLabel(String(params.value));
      if (params.seriesType === 'scatter') return `${safeText(params.data.name)}\n${formatDeskTime(params.value[0])}`;
      const agent = sorted[params.value[0]];
      return `${safeText(agent.title)}\n${agent.models.map(U.shortModel).join(' / ')}${contextTooltip(agent)}\n${formatDeskTime(params.value[1])} — ${formatDeskTime(params.value[2])}\n${U.duration(agent.minutes)} · ${U.number(U.totalTokens(agent))} token\n点击打开代理详情`;
    } }, xAxis: { ...axes(), type: 'time', min: bars.length ? Math.min(...bars.map(item => item.value[1])) - 60000 : undefined, max: bars.length ? Math.max(...bars.map(item => item.value[2])) + 60000 : undefined, axisLabel: { color: muted, formatter: value => formatDeskTime(value).slice(6), hideOverlap: true }, splitNumber: 6 }, yAxis: { ...axes(), type: 'category', inverse: true, triggerEvent: true, data: sorted.map(agent => agent.id), axisLabel: { color: muted, fontSize: smallFont, margin: 12, rich: contextRich, formatter: id => richLabels.get(id) || id }, tooltip: { show: true, ...tooltip, formatter: params => fullLabel(String(params.value)) }, splitLine: { show: false } }, series: [
      { id: 'gantt-bars', type: 'custom', name: '代理运行', renderItem: (params, api) => {
        const start = api.coord([api.value(1), api.value(0)]), end = api.coord([api.value(2), api.value(0)]);
        const height = Math.min(20, api.size([0, 1])[1] * 0.6);
        const rect = echarts.graphic.clipRectByRect({ x: start[0], y: start[1] - height / 2, width: Math.max(4, end[0] - start[0]), height }, params.coordSys);
        return rect ? { type: 'rect', shape: { ...rect, r: [0, 4, 4, 0] }, style: { fill: bars[params.dataIndex]?.itemStyle.color }, emphasis: { style: { opacity: 0.75 } } } : null;
      }, encode: { x: [1, 2], y: 0 }, data: bars },
      { id: 'gantt-gaps', name: '失效', type: 'scatter', symbol: 'diamond', symbolSize: 12, itemStyle: { color: '#fab219', borderColor: surface, borderWidth: 2 }, data: gaps, z: 5 },
      { id: 'gantt-calls', name: '长调用', type: 'scatter', symbol: 'triangle', symbolSize: 12, itemStyle: { color: '#ec835a', borderColor: surface, borderWidth: 2 }, data: calls, z: 6 }
    ] };
  };
  const concurrency = points => ({ ...base(), grid: { left: 50, right: 26, top: 25, bottom: 42 }, tooltip: { ...tooltip, trigger: 'axis', axisPointer: { type: 'cross', label: { show: false } }, formatter: items => `${safeText(items[0]?.axisValue)}\n在跑代理 ${U.number(items[0]?.value)} 个` }, xAxis: { ...axes(), type: 'category', boundaryGap: false, data: points.map(point => point.t), axisLabel: { color: muted, formatter: value => value.slice(6), hideOverlap: true } }, yAxis: { ...axes(), type: 'value', minInterval: 1, name: '代理数', nameTextStyle: { color: muted } }, series: [{ id: 'concurrency', name: '在跑代理', type: 'line', data: points.map(point => point.n), lineStyle: { width: 2, color: '#3987e5' }, itemStyle: { color: '#3987e5', borderColor: surface, borderWidth: 2 }, symbolSize: 8, showSymbol: false, areaStyle: { color: '#3987e5', opacity: 0.1 } }] });
  const hourly = (models, vendor) => {
    const rows = models.filter(row => U.vendorOf(row.model).id === vendor.id);
    const buckets = new Map();
    rows.forEach(row => row.hourly?.forEach(point => {
      const time = epoch(point.t);
      if (Number.isFinite(time)) buckets.set(time, (buckets.get(time) || 0) + Number(point.tokens || 0));
    }));
    const times = [...buckets.keys()].sort((a, b) => a - b);
    const data = [];
    if (times.length) for (let time = times[0]; time <= times.at(-1); time += 3600000) data.push([time, buckets.get(time) || 0]);
    const crossDay = times.length > 1 && formatDeskTime(times[0]).slice(0, 5) !== formatDeskTime(times.at(-1)).slice(0, 5);
    return { ...base(), useUTC: true, grid: { left: 62, right: 24, top: 20, bottom: 44 }, tooltip: { ...tooltip, trigger: 'axis', axisPointer: { type: 'cross', label: { show: false } }, formatter: items => `${formatDeskTime(items[0]?.value?.[0] ?? Number(items[0]?.axisValue))}\n${vendor.name} · ${U.number(items[0]?.value?.[1])} token` }, xAxis: { ...axes(), type: 'time', min: times[0], max: times.at(-1), minInterval: 3600000, splitNumber: 5, axisLabel: { color: muted, formatter: value => crossDay ? formatDeskTime(value) : formatDeskTime(value).slice(6), hideOverlap: true } }, yAxis: { ...axes(), type: 'value', axisLabel: { color: muted, formatter: U.compact } }, series: [{ id: `hourly-${vendor.id}`, type: 'line', name: vendor.name, data, lineStyle: { width: 2, color: vendor.color }, itemStyle: { color: vendor.color, borderColor: surface, borderWidth: 2 }, symbolSize: 8, showSymbol: false, areaStyle: { color: vendor.color, opacity: 0.1 } }] };
  };
  const modelOrder = models => [...models].sort((a, b) => U.vendors.indexOf(U.vendorOf(a.model)) - U.vendors.indexOf(U.vendorOf(b.model)) || String(a.model).localeCompare(String(b.model)));
  const groupSlots = new Map();
  const groupSlices = (rows, scope) => {
    if (!groupSlots.has(scope)) groupSlots.set(scope, new Map());
    const slots = groupSlots.get(scope), result = [], other = { name: '其他分组', value: 0, color: U.vendors[4].color };
    [...rows].sort((a, b) => String(a.id || a.name).localeCompare(String(b.id || b.name), 'zh-CN')).forEach(row => {
      const key = String(row.id || row.name);
      if (!slots.has(key) && slots.size < 4) slots.set(key, slots.size);
      if (slots.has(key)) result.push({ name: row.name, value: row.tokens, color: U.vendors[slots.get(key)].color });
      else other.value += Number(row.tokens || 0);
    });
    if (other.value > 0) result.push(other);
    return result;
  };
  const distribution = (rows, unit, monetary) => {
    const data = rows.filter(row => row.value != null && Number(row.value) > 0).map(row => ({ name: safeText(row.name), value: Number(row.value), itemStyle: { color: row.color, borderColor: surface, borderWidth: 2 } }));
    return { ...base(), tooltip: { ...tooltip, trigger: 'item', formatter: params => `${safeText(params.name)}\n${unit} ${monetary ? U.money(params.value) : U.number(params.value)} · ${params.percent}%` }, series: [{ id: 'stats-distribution', type: 'pie', radius: ['53%', '78%'], center: ['50%', '49%'], stillShowZeroSum: false, minAngle: 1, avoidLabelOverlap: true, label: { show: false }, emphasis: { scale: false, label: { show: false } }, data }], graphic: data.length ? [] : [{ type: 'text', left: 'center', top: 'middle', style: { text: '暂无可统计数据', fill: muted, font: `400 ${smallFont}px ${fontFamily}` } }] };
  };
  const statsTrend = (trend, models) => {
    const points = trend.points || [], ordered = modelOrder(models);
    return { ...base(), grid: { left: 72, right: 25, top: 36, bottom: 56 }, tooltip: { ...tooltip, trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: items => {
      const point = points[items[0]?.dataIndex];
      if (!point) return '';
      return `${safeText(point.t)}\n请求 ${U.number(point.requests)} · Token ${U.number(point.tokens)}\n金额折算 ${U.money(point.usd)}\n${ordered.filter(row => Number(point.by_model?.[row.model]) > 0).map(row => `${safeText(row.model)} ${U.number(point.by_model[row.model])}`).join('\n')}`;
    } }, xAxis: { ...axes(), type: 'category', data: points.map(point => point.t), axisLabel: { color: muted, fontSize: smallFont, hideOverlap: true } }, yAxis: { ...axes(), type: 'value', name: 'Token', axisLabel: { color: muted, formatter: U.compact } }, series: ordered.map(row => ({ id: `stats-trend-${row.model}`, name: row.model, type: 'bar', stack: 'tokens', barMaxWidth: 24, itemStyle: { color: U.modelColor(row.model), borderColor: surface, borderWidth: 1 }, emphasis: { focus: 'series' }, data: points.map(point => Number(point.by_model?.[row.model]) || 0) })) };
  };
  window.BoardCharts = { VizChart, RadialTree, gantt, concurrency, hourly, modelOrder, groupSlices, distribution, statsTrend, epoch, 台面: formatDeskTime };
})();
