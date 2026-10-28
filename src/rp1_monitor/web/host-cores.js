/* Current per-CPU observations reuse the existing snapshot; no extra polling. */
(() => {
  const el = id => document.getElementById(id);
  const groups = [['big','大核'],['little','小核'],['unknown','未识别']];
  const firstSeen = new WeakMap();
  let robot = null, available = false, updatedAt = 0;
  function node(tag, text, css) {
    const item = document.createElement(tag);
    if (text != null) item.textContent = text;
    if (css) item.className = css;
    return item;
  }
  function render() {
    const matches = !!robot?.robot_id && (typeof selected === 'undefined' || robot.robot_id === selected);
    const host = matches ? robot.health?.host || {} : {};
    const cores = (host.cores || []).filter(core=>Number.isInteger(core.index) && core.index >= 0).sort((a,b)=>a.index-b.index);
    const verified = window.rp1RobotView?.realtimeAllowed(robot) ?? (robot?.transport ? robot.transport.freshness_verified === true : !robot?.clock_unverified);
    const live = matches && available && robot.enabled !== false && robot.online && verified;
    const elapsed = Math.max(0,Date.now()-updatedAt);
    let known = 0;
    for (const [key,title] of groups) {
      const items = cores.filter(core=>(['big','little'].includes(core.group) ? core.group : 'unknown') === key);
      const section = el('host-core-'+key), list = el('host-core-'+key+'-list');
      section.hidden = !items.length;
      el('host-core-'+key+'-title').textContent = `${title} · ${items.length} 核`;
      list.replaceChildren();
      for (const core of items) {
        const valid = live && Number.isFinite(core.usage_percent) && core.usage_percent >= 0 && core.usage_percent <= 100 && Number.isFinite(core.age_ms) && core.age_ms >= 0 && core.age_ms+elapsed <= 5000;
        if (valid) known++;
        const card = node('article',null,'host-core-card');
        card.dataset.coreIndex = String(core.index); card.dataset.state = valid ? 'observed' : 'unknown';
        const heading = node('div',null,'host-core-card-heading');
        heading.append(node('h5',core.label || `CPU ${core.index}`),node('strong',valid ? `${core.usage_percent.toFixed(1)} %` : '未知'));
        const track = node('div',null,'host-core-track'), fill = node('i');
        track.setAttribute('aria-hidden','true');
        fill.style.transform = `scaleX(${valid ? core.usage_percent/100 : 0})`;
        track.append(fill);
        const age = node('small',valid ? `观测年龄 ${((core.age_ms+elapsed)/1000).toFixed(1)} s` : '缺失、过期或连接未验证');
        card.append(heading,track,age); list.append(card);
      }
    }
    el('host-core-count').textContent = cores.length ? `${cores.length} 核 · ${known} 个有效观测` : '核心清单待提供';
    el('host-core-source').textContent = !matches ? '来源待确认' : robot.source_kind === 'simulated' ? '模拟数据 · 非实机' : 'ROS 2 来源 · 实机待验收';
    el('host-core-empty').hidden = cores.length > 0;
    el('host-core-empty').textContent = !matches ? '等待当前机器人的逐核观测。' : !available ? '平台连接中断，当前核心清单待确认。' : '当前采集端尚未上报逐核负载，请更新机器人采集端；不根据机型推测核心数量。';
    el('host-cpu-topology-note').textContent = host.cpu_topology_note || '尚未提供大小核识别依据；未能确认的核心归入“未识别”。';
    const message = !matches ? '等待设备数据' : !available ? '平台连接中断，逐核实时值未知。' : !robot.online ? '采集端离线，逐核实时值未知。' : !verified ? '时间或传输新鲜度未通过验证，逐核实时值未知。' : cores.length ? known === cores.length ? '逐核实时观测可用；CPU 使用率不代表整机健康。' : known ? '部分核心缺失或已过期，未知值不补零。' : '当前逐核观测未知，缺失不补零。' : '等待采集端提供每个 CPU 的内核采样。';
    if (el('host-core-state').textContent !== message) el('host-core-state').textContent = message;
  }
  function update(snapshot, connected) {
    robot = snapshot || null; available = !!connected;
    if (robot && !firstSeen.has(robot)) firstSeen.set(robot,Date.now());
    updatedAt = robot ? firstSeen.get(robot) : 0;
    render();
  }
  function changed() { robot = null; available = false; updatedAt = 0; render(); }
  function tick() { if (!document.hidden && window.rp1Pages?.isActive('host')) render(); }
  document.addEventListener('rp1:page',render);
  document.addEventListener('visibilitychange',()=>{if (!document.hidden) render();});
  window.rp1HostCores = {update,changed,tick};
  render();
})();
