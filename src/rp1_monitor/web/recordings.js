(() => {
  const el = id => document.getElementById(id), base = '/api/v1/monitor-recordings';
  const embedded = !!document.querySelector?.('[data-page-panel="recordings"]');
  const visible = new Set(), initializedScopes = new Set(), colors = ['#4165a0', '#a55123', '#168064', '#9143a3', '#bd3552', '#087f8c'];
  let readCredential = '', accessRejected = false, scopedRobot = '', scopeRevision = 0, activePage = !embedded, platformUnavailable = false;
  let robots = [], rawContext = {}, stateBusy = false, requestedRecording = typeof location === 'undefined' ? '' : new URLSearchParams(location.search).get('recording_id') || '';
  let listRevision = 0, synchronizingRoute = false;
  let context = {}, records = [], nextCursor = null, catalog = [], loadingRecords = false;
  let curveController, queryRevision = 0, queriedSignature = '', reportReady = false;

  async function request(path, signal) {
    if (embedded) return api(path.replace(/^\/+/, ''), 30000);
    const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', headers: readCredential ? {Authorization: 'Bearer ' + readCredential} : {}, signal: signal || AbortSignal.timeout(30000)});
    if (response.status === 401 || response.status === 403) {
      accessRejected = true; el('read-access').hidden = false; invalidate('读取权限尚未验证，请填写网页只读访问凭据');
      throw new Error('需要有效的网页只读访问凭据');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求参数或服务不可用，请核对输入');
    return data;
  }
  function option(value, text) { const node = document.createElement('option'); node.value = value; node.textContent = text; return node; }
  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function number(value) { return finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: 4}) : '—'; }
  function time(value) {
    return value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '—';
  }
  function signature() {
    return JSON.stringify([el('history-robot').value, el('session').value, el('period').value, el('range-start').value, el('range-end').value, [...visible]]);
  }
  function invalidate(message = '查询条件已更改，请点击“查询曲线”') {
    queryRevision += 1; curveController?.abort(); curveController = null; queriedSignature = ''; reportReady = false;
    el('print-report').disabled = true; el('report-summary').hidden = true;
    el('charts').replaceChildren(); el('curve-table').replaceChildren(); el('curve-state').textContent = message;
    el('refresh-curves').disabled = false;
  }
  function fallbackMetric(key) {
    const tail = key.split('.').pop(), names = {motor_temperature: ['电机温度', '°C'], mos_temperature: ['MOS温度', '°C'], temperature: ['温度', '°C'], torque: ['上报力矩', 'N·m'], voltage: ['反馈电压', 'V'], current: ['电流', 'A'], soc: ['电量', '%'], position: ['反馈位置', 'rad'], velocity: ['反馈速度', 'rad/s'], tracking_rms: ['配对跟踪 RMS', 'rad'], policy_target: ['策略目标', 'rad']};
    const [name, unit] = names[tail] || [tail, ''];
    const prefix = key.startsWith('joint.') ? `关节 ${key.split('.')[1]} · ` : key.startsWith('battery.') ? '电池 · ' : '';
    return {key, label: prefix + name, unit};
  }
  function updateViewMetrics() {
    const robot = el('history-robot').value, session = el('session').value;
    const saved = new Set(records.filter(row => row.robot_id === robot && (!session || row.id === session)).flatMap(row => row.metrics || []));
    const entries = catalog.filter(metric => saved.has(metric.key));
    for (const key of [...visible]) if (!saved.has(key)) visible.delete(key);
    const scope = robot + '|' + session;
    if (!visible.size && !initializedScopes.has(scope)) for (const metric of entries.slice(0, 4)) visible.add(metric.key);
    if (entries.length) initializedScopes.add(scope);
    const root = el('view-metrics'); root.replaceChildren();
    for (const metric of entries) {
      const label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
      input.type = 'checkbox'; input.value = metric.key; input.checked = visible.has(metric.key);
      text.textContent = `${metric.label} / ${metric.unit || '无单位'}`;
      input.addEventListener('change', () => {
        if (input.checked && visible.size >= 12) { input.checked = false; el('curve-state').textContent = '最多选择12条曲线，请先取消其他指标'; return; }
        input.checked ? visible.add(metric.key) : visible.delete(metric.key); invalidate();
      });
      label.append(input, text); root.append(label);
    }
    el('metric-empty').hidden = entries.length > 0;
  }
  function persistSelection(recordingId = '') {
    if (!embedded || synchronizingRoute || !window.rp1Pages?.isActive('recordings')) return;
    const target = new URL(location.href);
    if (recordingId) target.searchParams.set('recording_id', recordingId); else target.searchParams.delete('recording_id');
    if (target.href !== location.href) history.replaceState(history.state, '', target.href);
  }
  function publishView() {
    if (!embedded) return;
    const row = records.find(record => record.id === el('session').value && record.robot_id === scopedRobot);
    if (!requestedRecording) persistSelection(row?.id || '');
    document.dispatchEvent(new CustomEvent('rp1:recording-view', {detail: {robotId: scopedRobot, recordingId: row?.id || '', runNumber: row?.run_number || ''}}));
  }
  function updateSessions(previous = '') {
    const rows = records.filter(row => row.robot_id === el('history-robot').value);
    el('session').replaceChildren(option('', '该机器人全部记录'), ...rows.map(row => option(row.id, `${row.source_kind === 'simulated' ? '[模拟] ' : ''}${row.run_number}`)));
    if (rows.some(row => row.id === previous)) el('session').value = previous;
    updateViewMetrics(); publishView();
  }
  function rebuild() {
    const before = signature(), robot = el('history-robot').value, session = el('session').value;
    const map = new Map((context.catalog || []).map(metric => [metric.key, metric]));
    for (const row of records) for (const key of row.metrics || []) if (!map.has(key)) map.set(key, fallbackMetric(key));
    catalog = [...map.values()];
    const robots = embedded ? [scopedRobot].filter(Boolean) : [...new Set([context.robot_id, ...records.map(row => row.robot_id)].filter(Boolean))];
    el('history-robot').replaceChildren(...(robots.length ? robots.map(id => option(id, id)) : [option('', '暂无记录')]));
    if (robots.includes(robot)) el('history-robot').value = robot;
    if (embedded) el('history-robot').value = scopedRobot;
    updateSessions(session);
    if (signature() !== before) invalidate('记录列表已更新，请查询曲线');
    el('record-list-state').textContent = `已加载 ${records.length} 次记录${nextCursor ? '，可继续加载更早编号' : ''}；“全部记录”曲线查询覆盖所选机器人的全部已保存历史。`;
    el('more-records').hidden = !nextCursor;
  }
  function renderContext() {
    const selectedRobot = robots.find(robot => robot.robot_id === scopedRobot);
    const sameSource = !embedded || rawContext.robot_id === scopedRobot;
    const scopedActive = !embedded || rawContext.active?.session?.robot_id === scopedRobot ? rawContext.active : null;
    context = embedded ? {...rawContext, robot_id: scopedRobot || null, active: scopedActive,
      connected: sameSource && !platformUnavailable && rawContext.connected,
      source_kind: sameSource ? rawContext.source_kind : selectedRobot?.source_kind,
      catalog: sameSource ? rawContext.catalog : [],
      pending_samples: sameSource ? rawContext.pending_samples : 0,
      error: scopedActive || sameSource ? rawContext.error : ''} : rawContext;
    const active = context.active, storage = context.history_database || (context.storage === 'postgresql' ? 'PostgreSQL 长期历史库' : '尚未配置长期历史库');
    const source = context.source_kind === 'simulated' ? '模拟数据 · 非实机' : context.source_kind === 'ros2' ? 'ROS 2 来源 · 仍待实机验收' : '暂无当前采集来源';
    el('source-state').textContent = `${storage} · ${source} · ${context.context_pending ? '正在核对当前机器人的记录状态' : context.control_status_unknown ? '中央历史查询，实时采集状态请在上位机确认' : context.connected ? '上位机正在接收' : '当前链路未接通，仍可查询历史'}`;
    el('source-state').classList.toggle('source-warning', !context.enabled || !context.connected || context.source_kind === 'simulated');
    el('record-state').textContent = (context.context_pending ? '正在读取当前机器人的记录状态…' : active ? `正在记录：${active.session.run_number}\n实际采集 ID：${active.session.robot_id}\n机器人标签：${active.session.robot_number}\n用例：${active.session.case_number} · 场地：${active.session.site}\n保存频率 ${active.query.frequency_hz} Hz · 累计采样 ${active.sequence} 点` : context.control_status_unknown ? '当前为中央只读服务，采集运行状态请在上位机确认。' : context.schema_ready === false ? '长期历史表尚未准备，请在上位机完成数据库配置。' : '当前机器人没有运行中的记录') + (context.control_status_unknown || context.context_pending ? '' : `\n待确认入库 ${context.pending_samples || 0} 点`) + (context.error ? '\n记录异常：' + context.error : '');
    if (embedded) document.dispatchEvent(new CustomEvent('rp1:recording-status', {detail: context}));
  }
  async function state() {
    if (stateBusy) return;
    stateBusy = true;
    const revision = scopeRevision, robot = embedded ? scopedRobot : el('history-robot').value;
    const current = () => revision === scopeRevision && robot === (embedded ? scopedRobot : el('history-robot').value);
    try {
      const result = await request(base + '/context' + (robot ? '?' + new URLSearchParams({robot_id: robot}) : ''));
      if (!current()) return;
      rawContext = result; renderContext();
    }
    catch (error) {
      if (!current()) return;
      if (embedded) {
        rawContext = {...rawContext, active: null, connected: false, context_pending: false, error: '记录状态读取失败', control_status_unknown: true};
        renderContext();
      }
      throw error;
    } finally {
      stateBusy = false;
      if (!current()) state().catch(fail);
    }
  }
  async function applyRequestedRecording() {
    if (!requestedRecording || !activePage) return;
    const row = records.find(record => record.id === requestedRecording && record.robot_id === el('history-robot').value);
    if (row) {
      requestedRecording = ''; el('session').value = row.id; el('period').value = 'all'; el('custom-range').hidden = true;
      visible.clear(); initializedScopes.delete(row.robot_id + '|' + row.id); updateViewMetrics(); publishView(); invalidate('正在读取所选记录…');
      if (visible.size) await curves();
    } else if (nextCursor) {
      await loadRecords(true);
    } else {
      requestedRecording = ''; persistSelection(); invalidate('当前机器人中未找到指定记录，请重新选择。');
    }
  }
  async function loadRecords(more = false) {
    if (loadingRecords || (embedded && (!activePage || !scopedRobot))) return;
    if (context.schema_ready === false) { records = []; nextCursor = null; rebuild(); el('curve-state').textContent = '长期历史表尚未准备，暂无可查询记录。'; return; }
    const revision = scopeRevision, requestRevision = ++listRevision, robot = scopedRobot;
    loadingRecords = true; el('more-records').disabled = true;
    try {
      const query = new URLSearchParams();
      if (embedded) query.set('robot_id', robot);
      if (more && nextCursor) query.set('before', nextCursor);
      const data = await request(base + (query.size ? '?' + query : ''));
      if (revision !== scopeRevision || requestRevision !== listRevision || (embedded && !activePage)) return;
      const firstLoad = records.length === 0, merged = new Map(records.map(row => [row.id, row]));
      for (const row of data.items) if (!embedded || row.robot_id === robot) merged.set(row.id, row);
      records = [...merged.values()].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at) || b.id.localeCompare(a.id));
      // A head refresh must preserve the older page cursor and already loaded sessions.
      if (more || firstLoad) nextCursor = data.next_cursor;
      rebuild();
    } finally {
      if (revision === scopeRevision && requestRevision === listRevision) { loadingRecords = false; el('more-records').disabled = false; }
    }
    if (revision === scopeRevision && requestRevision === listRevision) await applyRequestedRecording();
  }
  function totals(points, key) {
    let minimum = Infinity, maximum = -Infinity, valid = 0, total = 0;
    for (const point of points) {
      const value = point.values?.[key];
      if (!value) continue;
      if (finite(value.minimum)) minimum = Math.min(minimum, value.minimum);
      if (finite(value.maximum)) maximum = Math.max(maximum, value.maximum);
      valid += Number(value.valid) || 0; total += Number(value.total) || 0;
    }
    return {minimum: Number.isFinite(minimum) ? minimum : null, maximum: Number.isFinite(maximum) ? maximum : null, valid, total};
  }
  function draw(data) {
    const root = el('charts'), tbody = el('curve-table'); root.replaceChildren(); tbody.replaceChildren();
    const groups = new Map();
    for (const metric of data.metrics) { if (!groups.has(metric.unit)) groups.set(metric.unit, []); groups.get(metric.unit).push(metric); }
    for (const [unit, metrics] of groups) {
      let low = Infinity, high = -Infinity;
      for (const metric of metrics) {
        const summary = totals(data.points, metric.key);
        if (finite(summary.minimum)) low = Math.min(low, summary.minimum);
        if (finite(summary.maximum)) high = Math.max(high, summary.maximum);
        const row = document.createElement('tr');
        for (const value of [metric.label, unit || '—', number(summary.minimum), number(summary.maximum), `${summary.valid.toLocaleString()} / ${summary.total.toLocaleString()}`]) {
          const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
        }
        tbody.append(row);
      }
      if (!Number.isFinite(low)) { low = 0; high = 1; }
      if (low === high) { low -= 1; high += 1; }
      const figure = document.createElement('figure'), caption = document.createElement('figcaption');
      caption.textContent = `${unit || '无单位'} · 分桶均值 / 竖线为最小至最大范围`;
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 800 280'); svg.classList.add('record-chart'); svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', metrics.map(metric => metric.label).join('、') + '历史曲线，数值及完整性见下方表格');
      const make = (tag, attributes) => { const node = document.createElementNS(svg.namespaceURI, tag); for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value)); return node; };
      const t0 = Date.parse(data.start), span = Math.max(1, Date.parse(data.end) - t0);
      const x = value => 70 + (Date.parse(value) - t0) / span * 690, y = value => 220 - (value - low) / (high - low) * 190;
      for (let i = 0; i < 5; i += 1) {
        const yy = 30 + i * 190 / 4, text = make('text', {x: 6, y: yy + 4, fill: '#596a83', 'font-size': 11});
        text.textContent = number(high - i * (high - low) / 4);
        svg.append(make('line', {x1: 70, x2: 760, y1: yy, y2: yy, stroke: '#dce4f2'}), text);
      }
      const legend = document.createElement('div'); legend.className = 'legend';
      metrics.forEach((metric, index) => {
        const color = colors[index % colors.length], dash = index % 3 === 1 ? '6 3' : index % 3 === 2 ? '2 3' : '';
        const item = document.createElement('span'); item.style.borderBottom = `2px ${dash ? 'dashed' : 'solid'} ${color}`; item.textContent = metric.label; legend.append(item);
        let path = '', connected = false, count = 0;
        for (const point of data.points) {
          const value = point.values?.[metric.key];
          if (point.gap_before) connected = false;
          if (value && finite(value.minimum) && finite(value.maximum) && value.minimum !== value.maximum) svg.append(make('line', {x1: x(point.time), x2: x(point.time), y1: y(value.minimum), y2: y(value.maximum), stroke: color, opacity: 0.3}));
          if (!value || !finite(value.value)) { connected = false; continue; }
          path += `${connected ? 'L' : 'M'}${x(point.time).toFixed(2)},${y(value.value).toFixed(2)} `; connected = true; count += 1;
          if (data.points.length < 15) {
            const dot = make('circle', {cx: x(point.time), cy: y(value.value), r: 3, fill: color}), title = make('title', {});
            title.textContent = `${metric.label} · ${time(point.time)} · ${number(value.value)} ${unit}`; dot.append(title); svg.append(dot);
          }
        }
        svg.append(make('path', {d: path, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-dasharray': dash}));
        if (!count) { item.textContent += ' · 无完整有效点'; }
      });
      for (const [value, anchor, xx] of [[data.start, 'start', 70], [data.end, 'end', 760]]) {
        const text = make('text', {x: xx, y: 248, 'text-anchor': anchor, fill: '#596a83', 'font-size': 11}); text.textContent = time(value); svg.append(text);
      }
      const zone = make('text', {x: 760, y: 268, 'text-anchor': 'end', fill: '#596a83', 'font-size': 10}); zone.textContent = '北京时间 UTC+8'; svg.append(zone);
      figure.append(caption, svg, legend); root.append(figure);
    }
  }
  function report(data, query, session, queryRecords) {
    const root = el('report-metadata'); root.replaceChildren();
    const add = (name, value) => { const term = document.createElement('dt'), detail = document.createElement('dd'); term.textContent = name; detail.textContent = value; root.append(term, detail); };
    const sourceKinds = data.source_kinds || [...new Set(queryRecords.map(row => row.source_kind))];
    const source = sourceKinds.includes('simulated') ? '包含 simulated 模拟数据 · 不能作为实机测试结果' : sourceKinds.length === 1 && sourceKinds[0] === 'ros2' ? 'ROS 2 来源 · 仍待实机验收' : '来源以各次记录为准，未限定单次记录';
    el('report-source').textContent = source; el('report-source').classList.toggle('source-warning', sourceKinds.includes('simulated'));
    add('本次编号', session?.run_number || '未限定单次记录 · 所选机器人全部记录');
    add('用例编号', session?.case_number || '跨记录，请限定单次记录查看标签');
    add('机器人标签', session?.robot_number || '跨记录，请限定单次记录查看标签');
    add('实际采集 ID', query.get('robot_id')); add('测试场地', session?.site || '跨记录');
    if (data.session_count != null) add('关联记录数', `${data.session_count.toLocaleString()} 次${data.sessions_truncated ? '（元数据列表已截断，请限定单次记录查看完整标签）' : ''}`);
    add('设置保存频率', session ? `${session.frequency_hz} Hz（不等于源话题频率）` : '按各次记录的设置');
    add('记录起止', session ? `${time(session.started_at)} 至 ${session.ended_at ? time(session.ended_at) : '尚未结束'}` : '覆盖所选机器人已保存历史');
    add('请求查看范围', query.has('start') ? `${time(query.get('start'))} 至 ${time(query.get('end'))}` : '从首次已保存记录起全部');
    add('实际数据范围', `${time(data.start)} 至 ${time(data.end)} · 北京时间 UTC+8`);
    add('所选指标', data.metrics.map(metric => `${metric.label} [${metric.key}] / ${metric.unit || '无单位'}`).join('\n'));
    add('数据量与显示', `${data.count.toLocaleString()} 个保存采样点；${data.points.length.toLocaleString()} 个时间桶，桶宽约 ${number(data.interval_seconds)} 秒`);
    add('报告生成时间', time(new Date().toISOString()) + ' · 北京时间 UTC+8');
    const summaries = data.metrics.map(metric => totals(data.points, metric.key)), valid = summaries.reduce((sum, value) => sum + value.valid, 0), total = summaries.reduce((sum, value) => sum + value.total, 0);
    el('report-quality').textContent = `所选指标有效观测 ${valid.toLocaleString()} / ${total.toLocaleString()} 项；各指标完整性见下表。缺失保留断点，含缺失的时间桶不绘制均值连线。时间基准为电脑接收/记录时间，不代表源话题精确同步；策略目标不是最终电机目标。记录开启前的数据无法补录。本报告仅汇总监控观测，不给出正式测试通过或失败结论。`;
    el('report-summary').hidden = false; reportReady = true; el('print-report').disabled = false;
  }
  async function curves(automatic = false) {
    if (embedded && !activePage) return;
    if (automatic && curveController) return;
    const robot = el('history-robot').value;
    if (!robot || !visible.size) { invalidate('请先选择有记录的机器人与显示指标'); return; }
    const query = new URLSearchParams({robot_id: robot, max_points: '800'});
    for (const key of visible) query.append('metrics', key);
    const session = records.find(row => row.id === el('session').value);
    if (session) query.set('recording_id', session.id);
    const period = el('period').value;
    if (period === 'custom') {
      const start = el('range-start').value, end = el('range-end').value;
      if (!start || !end || !Number.isFinite(Date.parse(start + '+08:00')) || Date.parse(start + '+08:00') >= Date.parse(end + '+08:00')) { invalidate('请填写有效的开始和结束时间（北京时间）'); return; }
      query.set('start', new Date(start + '+08:00').toISOString()); query.set('end', new Date(end + '+08:00').toISOString());
    } else if (period !== 'all') {
      const end = new Date(); query.set('end', end.toISOString()); query.set('start', new Date(end - Number(period) * 1000).toISOString());
    }
    const querySignature = signature(), queryRecords = records.filter(row => row.robot_id === robot && (!session || row.id === session.id));
    curveController?.abort(); const controller = new AbortController(); curveController = controller; const revision = ++queryRevision;
    reportReady = false; el('print-report').disabled = true; el('refresh-curves').disabled = true; el('curve-state').textContent = '正在查询已保存的完整时段…';
    try {
      const data = await request(base + '/curves?' + query, controller.signal);
      if (revision !== queryRevision || querySignature !== signature()) return;
      el('charts').replaceChildren(); el('curve-table').replaceChildren(); el('report-summary').hidden = true;
      if (!data.count) { queriedSignature = querySignature; el('curve-state').textContent = '该区间没有已入库记录。可更换记录或扩大查看范围。'; return; }
      draw(data); report(data, query, session, queryRecords); queriedSignature = querySignature;
      el('curve-state').textContent = `覆盖 ${time(data.start)} 至 ${time(data.end)} · ${data.count.toLocaleString()} 个保存采样点 · 显示 ${data.points.length} 个时间桶（缺失和记录间隙保留断点）`;
    } catch (error) {
      if (revision === queryRevision && error.name !== 'AbortError') { el('charts').replaceChildren(); el('curve-table').replaceChildren(); el('report-summary').hidden = true; el('curve-state').textContent = '查询失败：' + error.message + '。请重试。'; }
    } finally {
      if (revision === queryRevision) { curveController = null; el('refresh-curves').disabled = false; }
    }
  }
  el('apply-read-credential')?.addEventListener('click', async () => {
    readCredential = el('read-credential').value.trim(); el('read-credential').value = '';
    if (!readCredential) { el('source-state').textContent = '请填写网页只读访问凭据'; return; }
    accessRejected = false; el('apply-read-credential').disabled = true;
    try { await init(); el('read-access').hidden = !accessRejected; }
    catch (error) { el('source-state').textContent = error.message + '。请核对只读凭据后重试。'; }
    finally { el('apply-read-credential').disabled = false; }
  });
  el('clear-read-credential')?.addEventListener('click', () => {
    readCredential = ''; el('read-credential').value = ''; invalidate('已清除本页输入的只读凭据'); el('read-access').hidden = false;
  });
  el('history-robot').addEventListener('change', () => { updateSessions(); invalidate(); });
  el('session').addEventListener('change', () => { requestedRecording = ''; updateViewMetrics(); publishView(); invalidate(); });
  el('period').addEventListener('change', () => { el('custom-range').hidden = el('period').value !== 'custom'; invalidate(); });
  for (const id of ['range-start', 'range-end']) el(id).addEventListener('change', () => invalidate());
  el('refresh-curves').addEventListener('click', () => curves());
  el('more-records').addEventListener('click', () => loadRecords(true).catch(error => { el('curve-state').textContent = '记录列表读取失败：' + error.message; }));
  el('print-report').addEventListener('click', () => { if (activePage && reportReady && queriedSignature === signature()) window.print(); });
  async function init() { await state(); await loadRecords(); if (visible.size && !queriedSignature) await curves(); }
  const fail = error => { el('source-state').textContent = error.message + '。请在当前工作台重试。'; };
  async function activate() {
    if (!embedded) return;
    const active = !!window.rp1Pages?.isActive('recordings');
    if (active === activePage) return;
    activePage = active;
    if (!active) { listRevision += 1; loadingRecords = false; invalidate('返回监控记录后重新查询'); return; }
    try {
      await loadRecords();
      if (visible.size && !queriedSignature) await curves();
    } catch (error) { fail(error); }
  }
  function update(items, selected, options = {}) {
    if (!embedded) return;
    robots = items; platformUnavailable = !!options.unavailable;
    const next = window.rp1Pages?.robotId?.() || selected || '';
    if (next !== scopedRobot) {
      // A new route may already carry this robot's historical record. Preserve it until scoped rows validate it.
      requestedRecording = new URLSearchParams(location.search).get('recording_id') || '';
      scopedRobot = next; scopeRevision += 1; listRevision += 1; loadingRecords = false;
      records = []; nextCursor = null; visible.clear(); initializedScopes.clear(); catalog = [];
      el('history-robot').replaceChildren(option(next, next || '尚未选择机器人'));
      updateSessions(); invalidate('切换机器人，等待读取其监控记录');
      rawContext = {robot_id: next, active: null, connected: false, context_pending: true};
      renderContext();
      state().catch(fail);
      activePage = false;
    }
    activate();
  }
  if (embedded) {
    el('history-robot').disabled = true;
    el('history-robot').closest?.('div')?.setAttribute('hidden', '');
    if (el('read-access')) el('read-access').hidden = true;
    window.rp1Recordings = {update, activate};
    document.addEventListener('rp1:page', () => {
      const showing = !!window.rp1Pages?.isActive('recordings');
      const fromURL = showing ? new URLSearchParams(location.search).get('recording_id') || '' : '';
      // History entries own their selection; a retained DOM session must not rewrite them.
      synchronizingRoute = true;
      update(robots, window.rp1Pages?.robotId?.(), {unavailable: platformUnavailable});
      if (showing) {
        requestedRecording = fromURL;
        el('session').value = '';
        updateViewMetrics(); invalidate('正在恢复当前页面的记录范围…'); publishView();
      }
      synchronizingRoute = false;
      if (showing && !loadingRecords) loadRecords().then(() => {
        if (activePage && visible.size && !queriedSignature) return curves();
      }).catch(fail);
    });
    document.addEventListener('rp1:recording-select', event => {
      if (event.detail?.robotId && event.detail.robotId !== scopedRobot) return;
      requestedRecording = event.detail?.recordingId || '';
      if (requestedRecording && activePage) {
        if (loadingRecords) return;
        loadRecords().catch(fail);
      }
    });
    state().catch(fail);
    update([], window.rp1Pages?.robotId?.());
  } else {
    init().catch(fail);
  }
  setInterval(async () => {
    if (document.hidden) return;
    try {
      await state();
      if (!activePage) return;
      await loadRecords();
      if (context.active && el('auto-refresh').checked && queriedSignature && queriedSignature === signature()) await curves(true);
    } catch (error) { el('source-state').textContent = '后台读取失败：' + error.message + '。历史查询可重试。'; }
  }, 5000);
})();
