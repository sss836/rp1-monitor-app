/* Read-only presentation of the existing robot snapshot. No invented test results. */
'use strict';
(() => {
  const el = id => document.getElementById(id);
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let userPaused = false, fleetSignature = '', available = false;

  function motionState() {
    const paused = userPaused || reduced.matches;
    document.body.dataset.motion = paused ? 'paused' : 'running';
    document.body.dataset.hidden = String(document.hidden);
    const toggle = el('motion-toggle');
    if (toggle) {
      toggle.textContent = reduced.matches ? '已减少动画' : userPaused ? '播放动效' : '暂停动效';
      toggle.setAttribute('aria-pressed', String(paused));
      toggle.disabled = reduced.matches;
    }
    document.dispatchEvent(new CustomEvent('rp1:motion', {detail:{paused,reduced:reduced.matches}}));
  }
  el('motion-toggle')?.addEventListener('click', () => { userPaused = !userPaused; motionState(); });
  reduced.addEventListener('change', motionState);
  document.addEventListener('visibilitychange', motionState);
  motionState();

  const nav = el('primary-nav'), navToggle = el('nav-toggle');
  function closeNav() {
    if (nav) nav.dataset.open = 'false';
    navToggle?.setAttribute('aria-expanded', 'false');
    navToggle?.setAttribute('aria-label', '展开导航');
  }
  navToggle?.addEventListener('click', () => {
    const open = nav?.dataset.open !== 'true';
    if (nav) nav.dataset.open = String(open);
    navToggle.setAttribute('aria-expanded', String(open));
    navToggle.setAttribute('aria-label', open ? '收起导航' : '展开导航');
  });
  nav?.querySelectorAll('a').forEach(link => link.addEventListener('click', closeNav));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && nav?.dataset.open === 'true') { closeNav(); navToggle?.focus(); }
  });
  el('nav-diagnose')?.addEventListener('click', () => el('ops-open')?.click());

  const number = (value, unit) => Number.isFinite(value) ? `${value.toFixed(1)}${unit}` : '未知';
  const source = robot => robot?.source_kind === 'simulated' ? '模拟数据 · 非实机' : robot ? 'ROS 2 来源 · 实机待验收' : '来源待确认';
  const fresh = robot => available && robot?.enabled && robot.online && robot.data_fresh;
  const partial = robot => available && !fresh(robot) && !!window.rp1RobotView?.hasCurrentObservations(robot);
  function text(id, value) { if (el(id)) el(id).textContent = value; }
  function node(tag, content, className) {
    const item = document.createElement(tag);
    if (content != null) item.textContent = content;
    if (className) item.className = className;
    return item;
  }
  function buildFleet(robots, selectedId) {
    const wrapper = el('fleet-coverflow')?.querySelector('.swiper-wrapper');
    if (!wrapper) return;
    const signature = JSON.stringify(robots.map(robot => [robot.robot_id,robot.name,robot.source_kind]));
    if (signature !== fleetSignature) {
      fleetSignature = signature;
      wrapper.replaceChildren();
      for (const robot of robots) {
        const slide = node('article', null, 'swiper-slide');
        slide.dataset.robotId = robot.robot_id;
        const button = node('button', null, 'fleet-card');
        button.type = 'button'; button.setAttribute('aria-pressed', String(robot.robot_id === selectedId));
        const top = node('div', null, 'fleet-card-top');
        top.append(node('span', source(robot), 'pill warning'), node('span', '', 'card-state'));
        const image = node('img'); image.src = './assets/brand/robot-wave.webp';
        image.alt = 'RP1 静态品牌示意，非实时姿态'; image.width = 960; image.height = 1707; image.loading = 'lazy';
        const bottom = node('div', null, 'fleet-card-bottom');
        bottom.append(node('span', '电机最高温度'), node('strong', '未知'));
        button.append(top, image, node('h3', robot.name), node('span', robot.robot_id, 'fleet-card-id'), bottom);
        button.addEventListener('click', () => window.rp1Pages ? window.rp1Pages.navigate('overview', robot.robot_id) : window.rp1SelectRobot?.(robot.robot_id));
        slide.append(button); wrapper.append(slide);
      }
      text('fleet-position', robots.length ? `1 / ${robots.length}` : '尚未登记机器人');
      document.dispatchEvent(new CustomEvent('rp1:fleet-ready', {detail:{selectedId}}));
    }
    const empty = el('fleet-empty');
    if (empty) empty.hidden = robots.length > 0;
    for (const slide of wrapper.querySelectorAll('.swiper-slide')) {
      const robot = robots.find(item => item.robot_id === slide.dataset.robotId);
      if (!robot) continue;
      const button = slide.querySelector('button');
      button.setAttribute('aria-pressed', String(robot.robot_id === selectedId));
      const state = slide.querySelector('.card-state');
      state.textContent = !available ? '平台连接中断' : !robot.enabled ? '已停用' : fresh(robot) ? '数据新鲜' : partial(robot) ? '部分数据可用' : robot.online ? '观测过期' : '离线';
      state.className = 'card-state ' + (fresh(robot) ? 'good' : 'warning');
      slide.querySelector('.fleet-card-bottom strong').textContent = number(fresh(robot) || partial(robot) ? robot.health?.motors?.max_temperature : null, '°C');
    }
    window.rp1Showcase?.updateFleet(selectedId);
    // Native buttons remain useful if the optional enhancement cannot be loaded.
    if (!el('fleet-coverflow')?.classList.contains('swiper-initialized')) {
      const index = robots.findIndex(robot => robot.robot_id === selectedId);
      text('fleet-position', robots.length ? `${index + 1} / ${robots.length}` : '尚未登记机器人');
      if (el('fleet-prev')) el('fleet-prev').disabled = index <= 0;
      if (el('fleet-next')) el('fleet-next').disabled = index < 0 || index >= robots.length - 1;
    }
  }
  let latestRobots = [], latestSelection = null;
  function nativeFleet(direction) {
    if (el('fleet-coverflow')?.classList.contains('swiper-initialized')) return;
    const index = latestRobots.findIndex(robot => robot.robot_id === latestSelection) + direction;
    if (latestRobots[index]) window.rp1SelectRobot?.(latestRobots[index].robot_id);
  }
  el('fleet-prev')?.addEventListener('click', () => nativeFleet(-1));
  el('fleet-next')?.addEventListener('click', () => nativeFleet(1));

  function update(robots, selectedId, options = {}) {
    latestRobots = robots; latestSelection = selectedId;
    if (Object.prototype.hasOwnProperty.call(options, 'unavailable')) available = !options.unavailable;
    const robot = robots.find(item => item.robot_id === selectedId), health = fresh(robot) || partial(robot) ? robot.health : null;
    text('hero-robot-name', robot?.name || '等待机器人数据');
    text('hero-robot-id', robot?.robot_id || '未选择设备');
    text('hero-source', source(robot));
    const dof = robot?.monitoring_configuration?.configuration?.dof || robot?.health?.motor_map?.dof;
    text('hero-dof', dof ? `${dof} 自由度` : '待确认');
    text('hero-temperature', number(health?.motors?.max_temperature, '°C'));
    const missing = window.rp1RobotView?.missingMotionTopics(robot) || [];
    text('hero-note', !available ? '平台连接中断，实时状态未知。请重试连接。' : !robot ? '等待接入设备，未接收观测的数据保持未知。' : fresh(robot) ? (robot.source_kind === 'simulated' ? '当前为模拟观测，仅用于软件展示与验证。' : '已接收 ROS 2 观测，实机验收与物理映射仍须核对。') : partial(robot) ? `当前部分观测可用；${missing.length ? missing.join('、')+'未提供或过期' : 'ROS 数据完整性待核对'}。不代表整机健康或实机验收通过。` : '当前设备离线、停用或观测已过期，实时状态未知。');
    el('hero-temperature')?.classList.toggle('bad', !!health?.motor_map?.hot_count);
    if (!available) {
      text('robot-count', robots.length || '未知'); text('online-count', '未知'); text('fresh-count', '未知');
    }
    buildFleet(robots, selectedId);
  }
  window.rp1Presentation = { update, unavailable:() => update(latestRobots,latestSelection,{unavailable:true}) };
  // The existing renderer will provide current data on its next poll.
  if (typeof robots !== 'undefined') update(robots, typeof selected === 'undefined' ? null : selected);
})();
