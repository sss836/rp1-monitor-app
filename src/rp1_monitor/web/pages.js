/* Independent, bookmarkable robot pages. APIs remain the source of device access. */
(() => {
  const labels = {fleet:'机器人列表', overview:'整机概览', battery:'电池', joints:'关节状态', host:'主机负载', recordings:'监控记录', temperature:'关节热像', history:'连续监控曲线', processes:'核心进程', storage:'告警', diagnostics:'诊断助手'};
  const el = id => document.getElementById(id);
  const panels = [...document.querySelectorAll('[data-page-panel]')];
  const links = [...document.querySelectorAll('a[data-page]')];
  const base = new URL(document.baseURI);
  let items = [], selection = null, available = false, loaded = false, switchSignature = '', diagnosticRobot = null;
  let recordingContext = null, recordingContextAt = 0, viewedRecord = null;
  function parse() {
    const prefix = base.pathname.endsWith('/') ? base.pathname : base.pathname + '/';
    const path = location.pathname.startsWith(prefix) ? location.pathname.slice(prefix.length).replace(/\/$/, '') : '!';
    if (!path || path === 'robots') return {page:'fleet', id:null};
    if (path === 'recordings') return {page:'recordings', id:new URL(location.href).searchParams.get('robot_id') || null};
    const parts = path.split('/');
    try {
      if (parts.length === 3 && parts[0] === 'robots' && parts[2] !== 'fleet' && labels[parts[2]]) return {page:parts[2], id:decodeURIComponent(parts[1])};
    } catch (_) { /* Malformed URLs have no valid device context. */ }
    return {page:null, id:null};
  }
  let route = parse();
  function url(page, id = route.id || selection) {
    return new URL(page === 'fleet' ? 'robots' : !id ? page === 'recordings' ? 'recordings' : 'robots' : `robots/${encodeURIComponent(id)}/${page}`, base).href;
  }
  function isActive(page) {
    return route.page === page && (page === 'fleet' || items.some(item => item.robot_id === route.id));
  }
  function paint() {
    document.documentElement?.setAttribute('data-workspace-page',route.page || 'unknown');
    const robot = items.find(item => item.robot_id === (route.id || selection));
    const valid = route.page && (route.page === 'fleet' || robot?.robot_id === route.id);
    const waiting = !!route.page && route.page !== 'fleet' && !valid && (!loaded || !available || route.page === 'recordings' && !route.id);
    panels.forEach(panel => { panel.hidden = !valid || panel.dataset.pagePanel !== route.page; });
    el('page-not-found').hidden = !!valid || waiting;
    el('page-loading').hidden = !waiting && (loaded || route.page !== 'fleet');
    el('page-loading').textContent = !available && loaded ? '平台连接中断，等待恢复后核对当前机器人。' : route.page === 'recordings' && !route.id && loaded ? '暂无已登记机器人，请先在上位机接通采集。' : '正在读取设备状态…';
    el('page-title').textContent = labels[route.page] || '页面不存在';
    document.title = `${labels[route.page] || '页面不存在'}${route.id ? ' · ' + route.id : ''} · RP1 整机监控`;
    el('workspace-device-id').textContent = robot?.robot_id || route.id || '尚未登记机器人';
    el('workspace-source').textContent = robot ? robot.source_kind === 'simulated' ? '模拟数据 · 非实机' : 'ROS2来源 · 待实机验收' : '来源待确认';
    const dof = robot?.health?.motor_map?.dof ?? robot?.monitoring_configuration?.configuration?.dof;
    el('workspace-profile').textContent = dof ? `${dof} 自由度` : '机型以设备档案为准';
    el('workspace-status').textContent = !available ? '实时状态未知' : !robot ? '请选择已登记机器人' : !robot.enabled ? '已停用' : robot.online ? robot.data_fresh ? '采集在线 · 数据新鲜' : window.rp1RobotView?.hasCurrentObservations(robot) ? '采集在线 · 部分数据可用' : '采集在线 · 数据过期' : '离线 / 未连接';
    const signature = JSON.stringify(items.map(item => [item.robot_id, item.name]));
    if (signature !== switchSignature) {
      switchSignature = signature;
      const options = items.map(item => new Option(`${item.name} · ${item.robot_id}`, item.robot_id));
      el('robot-switch').replaceChildren(new Option(items.length ? '选择机器人' : '暂无登记设备', ''), ...options);
    }
    el('robot-switch').value = robot?.robot_id || '';
    const navPage = {temperature:'joints', processes:'host', history:'recordings'}[route.page] || route.page;
    links.forEach(link => {
      const page = link.dataset.page, disabled = page !== 'fleet' && !robot;
      link.href = url(page, robot?.robot_id);
      link.setAttribute('aria-disabled', String(disabled));
      if (valid && (page === route.page || link.dataset.nav && page === navPage)) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    paintRecord(robot);
    el('nav-diagnose').disabled = !robot;
    if (valid && route.page === 'diagnostics' && selection === route.id) {
      if (diagnosticRobot !== route.id && window.rp1Operations?.openInline) {
        diagnosticRobot = route.id;
        window.rp1Operations.openInline();
      }
    } else {
      diagnosticRobot = null;
      if (el('ops-dialog').open) el('ops-dialog').close();
    }
  }
  function paintRecord(robot) {
    const label = el('header-record-label'), number = el('header-record-number');
    if (!label || !number) return;
    label.textContent = '当前记录';
    if (!robot || route.page === 'fleet') number.textContent = '选择机器人查看';
    else if (!available) number.textContent = '连接中断 · 记录状态未知';
    else if (viewedRecord?.robotId === robot.robot_id && viewedRecord.page === route.page && viewedRecord.recordingId) {
      label.textContent = '查看记录'; number.textContent = viewedRecord.runNumber || '正在读取记录编号…';
    } else if (!recordingContext || recordingContext.context_pending || Date.now()-recordingContextAt > 15000) number.textContent = '记录状态待确认';
    else if (recordingContext.active?.session?.robot_id === robot.robot_id) {
      label.textContent = '正在记录'; number.textContent = recordingContext.active.session.run_number;
    } else if (recordingContext.control_status_unknown) number.textContent = '实时记录状态请在上位机确认';
    else number.textContent = '当前未开启记录';
    number.title = number.textContent;
  }
  function enter(focus = true) {
    viewedRecord = null;
    for (const id of ['thermal-dialog', 'ops-model-dialog']) if (el(id)?.open) el(id).close();
    paint();
    if (route.id && route.id !== selection) window.rp1SelectRobot?.(route.id);
    if (focus) { el('page-title').setAttribute('tabindex', '-1'); el('page-title').focus({preventScroll:true}); window.scrollTo({top:0, behavior:'instant'}); }
    document.dispatchEvent(new CustomEvent('rp1:page', {detail:{page:route.page, robotId:route.id}}));
    window.rp1RefreshView?.(route.page);
    window.rp1Showcase?.updateFleet?.(selection);
  }
  function navigate(page, id = route.id || selection) {
    if (!labels[page] || (page !== 'fleet' && !id)) return;
    const target = url(page, id);
    if (target === location.href) return;
    history.pushState({}, '', target);
    route = {page, id:page === 'fleet' ? null : id};
    enter();
  }
  window.rp1Pages = {navigate, url, robotId:() => route.id, currentPage:() => route.page, isActive,
    update(robots, selected, options = {}) {
      items = robots; selection = selected; available = !options.unavailable;
      if (available) loaded = true;
      if (route.page === 'recordings' && !route.id && available) {
        const id = items.find(item=>item.robot_id === selection)?.robot_id || items[0]?.robot_id;
        if (id) { route = {page:'recordings',id}; history.replaceState({},'',url('recordings',id)); }
      }
      paint();
    }};
  links.forEach(link => link.addEventListener('click', event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (link.getAttribute('aria-disabled') !== 'true') {
      const record = link.id === 'header-record-link' && viewedRecord?.robotId === route.id ? viewedRecord : null;
      navigate(link.dataset.page);
      if (record?.recordingId) document.dispatchEvent(new CustomEvent('rp1:recording-select',{detail:record}));
    }
  }));
  el('robot-switch').addEventListener('change', event => { if (event.target.value) navigate(route.page && route.page !== 'fleet' ? route.page : 'overview', event.target.value); });
  document.addEventListener('rp1:recording-status', event => {
    recordingContext = event.detail; recordingContextAt = Date.now();
    paintRecord(items.find(item=>item.robot_id === (route.id || selection)));
  });
  for (const type of ['rp1:recording-view','rp1:component-context']) document.addEventListener(type,event => {
    if (event.detail.robotId !== route.id) return;
    viewedRecord = {...event.detail,page:route.page};
    paintRecord(items.find(item=>item.robot_id === route.id));
  });
  window.addEventListener('popstate', () => { route = parse(); enter(); });
  document.addEventListener('DOMContentLoaded', paint);
  paint();
})();
