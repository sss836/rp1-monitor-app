"use strict";
const byId = (id) => document.getElementById(id);
let token = "", robots = [], selected = window.rp1Pages?.robotId() || null, catalogSignature = "", historyVersion = 0;
let pollBusy = false, curveBusy = false;
let curveData = null;
let platformAvailable = false;
const endpoint = (path) => new URL(path, document.baseURI);
const format = (value) => value == null || !Number.isFinite(value) ? "—" : value.toFixed(4);
const timeText = (value) => value ? new Date(value).toLocaleTimeString("zh-CN", { hour12: false }) : "未接收";
const setText = (id, text, status) => { const element = byId(id); element.textContent = text; if (status) element.className = status; };

function renderHealth(health, sourceKind = null) {
  const b = health?.battery || {}, h = health?.host || {}, m = health?.motors || {};
  const number = (value, unit, digits = 1) => value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(digits)} ${unit}`;
  setText("health-state", health?.overall?.label || "健康状态待确认", health?.overall?.state === "fault" ? "bad" : "warning");
  setText("health-detail", health?.overall?.detail || "当前没有实时健康数据，不能判定机器人健康。");
  setText("health-limit-note", health?.motor_map?.rows?.some((row) => row.readings.some((r) => r.limit_c != null)) ? "电机温度限值已配置" : "判据待配置");
  setText("battery-soc", number(b.soc, "%"));
  setText("battery-electrical", `电压 ${number(b.voltage, "V")} · 电流 ${number(b.current, "A")}`);
  const rail = (value) => value == null ? "未提供" : value ? "开启" : "关闭";
  const railText = b.rail48_enabled != null || b.rail5_enabled != null ? ` · 48V轨 ${rail(b.rail48_enabled)} · 5V轨 ${rail(b.rail5_enabled)}` : "";
  setText("battery-power", `电池供电开关 ${rail(b.power_enabled)}${railText} · V×A计算功率 ${number(b.derived_power, "W")}`);
  setText("battery-temperature", number(b.temperature, "°C"));
  setText("battery-protection", b.protection == null ? "保护位未提供" : `原始保护位 0x${Math.trunc(b.protection).toString(16)} · 位定义待机型核对`, b.protection ? "bad" : "muted");
  setText("host-temperature", number(h.max_temperature, "°C"));
  setText("host-thermal-zones", h.sensors?.filter((s) => s.temperature != null).map((s) => `${s.label} ${number(s.temperature, "°C")}`).join(" · ") || "主机温度未提供 / 已过期");
  setText("host-cpu", `CPU ${number(h.cpu_usage, "%")}`);
  setText("host-memory", `内存 ${number(h.memory_usage, "%")}`);
  const uptime = h.uptime_seconds;
  if (byId('robot-uptime')) {
    setText('robot-uptime', Number.isFinite(uptime) && uptime >= 0 ? `${Math.floor(uptime/86400)}天 ${String(Math.floor(uptime/3600)%24).padStart(2,'0')}:${String(Math.floor(uptime/60)%60).padStart(2,'0')}:${String(Math.floor(uptime)%60).padStart(2,'0')}` : '—');
    setText('robot-uptime-note', Number.isFinite(uptime) ? sourceKind === 'simulated' ? '模拟源本次启动时长 · 非实机' : '机器人主机 /proc/uptime · 采集快照' : '未提供有效开机时长');
  }
  setText("motor-temperature", number(m.max_temperature, "°C"));
  setText("motor-mos", `MOS ${number(m.max_mos_temperature, "°C")}`);
  setText("motor-coverage", `完整温度/故障数据 ${m.available || 0} / ${m.total || 0} 台电机`);
  const faults = m.faults || [];
  setText("motor-fault-count", faults.length ? `${faults.length} 项故障上报` : m.state === "observing" ? "当前故障码均为 0" : "故障数据未完整接入", faults.length ? "bad" : "warning");
  setText("motor-faults", faults.map((f) => `${typeof f.joint === "string" ? f.joint : `关节 ${f.joint}`}：${f.code}`).join(" · ") || "未收到故障不等于已确认无故障");
  const sources = byId("health-sources"); sources.replaceChildren();
  for (const [title, info] of [["电池", b], ["主机", h], ["电机", m]]) {
    const p = document.createElement("p"); p.textContent = `${title}：${info.detail || "尚未接入 / 当前状态未知"}`; sources.append(p);
  }
  renderMotorMap(health?.motor_map);
}

function renderMotorMap(view) {
  const group = byId("motor-map-regions"), list = byId("motor-map-rows");
  const scrollTop = list.scrollTop;
  const activeRegion = document.activeElement?.closest?.('[data-region]')?.dataset.region;
  const activeJoint = document.activeElement?.closest?.('[data-joint]')?.dataset.joint;
  const pendingFocus = byId('unmapped-joint-list')?.contains?.(document.activeElement) ? activeJoint : null;
  group.replaceChildren(); list.replaceChildren();
  setText("motor-map-status", view?.summary || "温度状态未知", view?.hot_count ? "bad" : "muted");
  setText("motor-map-note", view ? `${view.mapping_note} · ${view.mapping_coverage || ""}` : "连接中断或尚无数据，当前部位状态未知");
  setText("motor-map-meaning", view?.meaning || "不使用旧温度推断当前状态。");
  setText("motor-map-low", view?.scale_min_c == null ? "低温" : `${view.scale_min_c} °C`);
  setText("motor-map-high", view?.scale_max_c == null ? "高温" : `${view.scale_max_c} °C`);
  byId("motor-map").setAttribute("aria-label", `机器人温度分布 · ${view?.mapping_note || "当前未知"} · ${view?.summary || "无实时数据"}`);
  const jointMap = typeof window !== 'undefined' ? window.rp1JointMap : null;
  const points = jointMap ? jointMap.points(view) : view?.regions || [];
  const setup = byId('motor-map-setup');
  if (setup) {
    setup.hidden = points.length > 0;
    setup.textContent = view?.rows?.length ? `已登记 ${view.rows.length} 个关节，物理部位映射尚未确认。可按下方编号查看；核对关节名称、实机位置并配置温度限值后，图上才显示对应温度。` : '尚未接收到关节档案或有效观测，整机温度位置保持未知。';
  }
  for (const region of points) {
    const jointPoint = Number.isInteger(region.joint);
    const node = document.createElementNS("http://www.w3.org/2000/svg", "g");
    node.dataset.region = region.key; node.dataset.state = region.state;
    if (jointPoint) node.dataset.joint = String(region.joint);
    node.setAttribute("role", "button"); node.setAttribute("tabindex", "0");
    node.setAttribute("aria-label", `${jointPoint ? `joint.${region.joint} · ` : ''}${region.label}，${region.value == null ? "温度未知" : region.value.toFixed(1) + "摄氏度"}，查看关节曲线与告警`);
    const open = () => jointPoint ? window.rp1Thermal?.openJoint(region.joint) : window.rp1Thermal?.openRegion(region.key);
    node.addEventListener?.("click", open);
    node.addEventListener?.("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
    const title = document.createElementNS(node.namespaceURI, "title"); title.textContent = `${region.label}\n${region.detail}`;
    const ellipse = document.createElementNS(node.namespaceURI, "ellipse");
    const x = region.x * 960, y = region.y * 1707;
    for (const [key, value] of Object.entries({cx:x, cy:y, rx:jointPoint ? 39 : 66, ry:jointPoint ? 34 : 56, fill:region.color, "fill-opacity":0.8, stroke:region.state === "hot" ? "#ae283a" : "#65758c", "stroke-width":region.state === "hot" ? 7 : 3})) ellipse.setAttribute(key, value);
    const text = document.createElementNS(node.namespaceURI, "text");
    for (const [key, value] of Object.entries({x, y:y + (jointPoint ? 10 : 16), fill:region.text_color, "text-anchor":"middle", "font-size":jointPoint ? 30 : 50, "font-weight":700})) text.setAttribute(key, value);
    text.textContent = region.value == null ? "—" : `${region.value.toFixed(0)}°`;
    node.append(title, ellipse, text); group.append(node);
    if (jointPoint) {
      const index = document.createElementNS(node.namespaceURI, 'text'); index.textContent = `J${String(region.joint).padStart(2,'0')}`;
      for (const [key,value] of Object.entries({x,y:y-44,fill:'#263451','text-anchor':'middle','font-size':24,'font-weight':700,'class':'joint-map-index'})) index.setAttribute(key,value);
      node.append(index);
    }
    if (activeRegion === region.key) node.focus?.({preventScroll:true});
  }
  for (const row of view?.rows || []) {
    const li = document.createElement("li"); li.dataset.joint = row.joint; li.dataset.state = row.state;
    const swatch = document.createElement("span"); swatch.className = "motor-temperature-swatch"; swatch.style.backgroundColor = row.color; swatch.setAttribute("aria-hidden", "true");
    const copy = document.createElement("div"), name = document.createElement("button"), detail = document.createElement("small");
    name.type = "button"; name.className = "thermal-joint-open";
    name.addEventListener?.("click", () => window.rp1Thermal?.openJoint(row.joint));
    name.textContent = `${row.joint} · ${row.label}`; detail.textContent = row.detail;
    const state = document.createElement("span"); state.className = `motor-temperature-state ${row.state === "hot" ? "bad" : "muted"}`; state.textContent = row.status;
    copy.append(name, detail); li.append(swatch, copy, state); list.append(li);
    if (!activeRegion && pendingFocus == null && activeJoint === String(row.joint)) name.focus?.({preventScroll:true});
  }
  list.scrollTop = scrollTop;
  jointMap?.renderUnmapped(view, pendingFocus);
  if (typeof window !== 'undefined') window.rp1TemperatureView?.enhance(view);
}

function renderProcesses(robot, available = true, unavailableReason = '平台连接中断') {
  const rows = (robot.health?.processes?.items || []).map((row) => available ? row : {...row, state:"unknown", label_state:"未知", pid:null, kernel_state:null, cpu_percent:null, rss_mib:null, threads:null, observed_restarts:null, age_ms:null, detail:`${unavailableReason}；以下时间为旧观测，当前状态未知`});
  const running = rows.filter((row) => row.state === "running").length;
  setText("process-summary", rows.length ? `${robot.source_kind === "simulated" ? "模拟数据 · 非实机　|　" : ""}存活 ${running} / ${rows.length} · 观测时刻与 PID 可查看行提示` : "尚未配置核心进程名单；在机器人档案 processes 中声明实际 comm 后采集。", rows.length && running === rows.length ? "muted" : "warning");
  if (!available) setText("process-summary", `${unavailableReason}，当前进程状态未知。`, "warning");
  const body = byId("process-body"); body.replaceChildren();
  const evidence = byId("process-evidence"); evidence.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr"); tr.title = `${row.detail} · comm=${row.comm} · 观测 ${row.observed_at || "未知"}`;
    const number = (value, digits = 0, unit = "") => value == null ? "—" : `${value.toFixed(digits)}${unit}`;
    const cells = [`${row.label} / ${row.pid ?? "—"}`, `${row.label_state}${row.kernel_state ? ` · ${row.kernel_state}` : ""}`, number(row.cpu_percent, 1, "%"), number(row.rss_mib, 1), number(row.threads), number(row.observed_restarts), number(row.age_ms == null ? null : row.age_ms / 1000, 1, " s")];
    cells.forEach((text, index) => { const td = document.createElement("td"); td.textContent = text; if (index === 1) td.className = row.state === "running" ? "good" : "warning"; tr.append(td); });
    body.append(tr);
    const detail = document.createElement("p"); detail.textContent = `${row.label} · comm=${row.comm} · 观测 ${row.observed_at || "未知"}：${row.detail}`; evidence.append(detail);
  }
}

async function api(path, timeoutMs = 5000) {
  const response = await fetch(endpoint(path), { headers: token ? { Authorization: `Bearer ${token}` } : {}, credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(timeoutMs) });
  if (response.status === 401 || response.status === 403) { byId("auth").classList.remove("hidden"); throw new Error("需要平台登录或只读访问凭据"); }
  if (!response.ok) throw new Error(`平台请求失败 (${response.status})`);
  return response.json();
}

function chooseRobot(id) {
  if (id === selected) return;
  selected = id;
  window.rp1Operations?.changed();
  window.rp1Central?.changed();
  window.rp1Thermal?.changed();
  window.rp1Components?.changed();
  window.rp1HostCores?.changed();
  catalogSignature = "";
  historyVersion++;
  curveBusy = false;
  curveData = null;
  byId("chart").replaceChildren();
  byId("chart-empty").classList.remove("hidden");
  setText("chart-resolution", "切换机器人，等待历史数据");
  render();
  loadCurve(true);
}
window.rp1SelectRobot = chooseRobot;

// A declared joint remains visible while its feedback publisher is stopped.
function robotJointRoster(robot) {
  const configuration = robot?.monitoring_configuration;
  const declared = configuration?.source === 'unconfigured' ? [] : configuration?.configuration?.joints || [];
  const mapped = robot?.health?.motor_map?.rows || [];
  const observed = robot?.quality?.joint_names || [];
  const indices = new Set([...declared.map(item => item.index), ...mapped.map(item => item.joint), ...observed.map((_, index) => index), ...Object.keys(robot?.metrics || {}).filter(key => /^joint\.\d+\./.test(key)).map(key => Number(key.split('.')[1]))]);
  return [...indices].sort((a, b) => a-b).map(index => {
    const joint = declared.find(item => item.index === index), row = mapped.find(item => item.joint === index);
    return [index, observed[index] || joint?.joint_name || row?.label || joint?.label || `关节 ${index}`];
  });
}

function robotHistoryRange(robot, seconds) {
  const captured = Date.parse(robot?.captured_at), received = Date.parse(robot?.received_at), now = Date.now();
  const robotClock = robot?.source_kind === 'ros2' && robot?.transport?.freshness_verified === true && Number.isFinite(captured) && Number.isFinite(received) && Math.abs(now-received) <= 30000;
  const end = new Date(robotClock ? captured + Math.max(0, now-received) : now);
  return {start:new Date(end.getTime()-seconds*1000), end, basis:robotClock ? `机器人原始时间 · 传输年龄验证${robot.clock_unverified ? '，跨机时间未同步' : ''}` : '中央查询时间'};
}
function robotObservationAgeMs(robot) {
  const transport = robot?.transport;
  if (transport) return Number.isFinite(transport.elapsed_seconds) && transport.elapsed_seconds >= 0 ? transport.elapsed_seconds*1000 : Infinity;
  return ((robot?.age_seconds || 0) + Math.max(0,robot?.clock_offset_seconds || 0))*1000;
}
function robotRealtimeAllowed(robot) {
  return !!robot && (robot.transport ? robot.transport.freshness_verified === true : !robot.clock_unverified);
}
function robotHasCurrentObservations(robot) {
  if (!robot?.online || robot.enabled === false || !robotRealtimeAllowed(robot)) return false;
  const battery = robot.health?.battery || {}, host = robot.health?.host || {};
  return [battery.soc,battery.voltage,battery.current,battery.temperature,battery.protection,host.cpu_usage,host.memory_usage,host.max_temperature,host.uptime_seconds].some(Number.isFinite) || (robot.health?.motors?.items || []).some(item=>[item.temperature,item.mos_temperature,item.fault_code].some(Number.isFinite));
}
function robotMissingMotionTopics(robot) {
  const ages = robot?.quality?.topic_ages_ms || {};
  return [['/joint_states','关节反馈'],['/action','策略目标'],['/imu','IMU']].filter(([topic])=>!Number.isFinite(ages[topic]) || ages[topic]+robotObservationAgeMs(robot)>1500).map(([,label])=>label);
}
window.rp1RobotView = {joints:robotJointRoster, historyRange:robotHistoryRange, observationAgeMs:robotObservationAgeMs, realtimeAllowed:robotRealtimeAllowed, hasCurrentObservations:robotHasCurrentObservations, missingMotionTopics:robotMissingMotionTopics};

function renderRobotReadiness(robot) {
  const panel = byId('robot-readiness');
  if (!panel) return;
  panel.hidden = robot?.source_kind !== 'ros2';
  const list = byId('robot-readiness-items'); list.replaceChildren();
  if (panel.hidden) return;
  const issues = [];
  if (!platformAvailable || !robot.online) issues.push('只读采集尚未连接或已离线；检查网线、SSH 隧道和上位机后台转发。');
  if (robot.transport && !robot.transport.freshness_verified) issues.push('传输新鲜度证据已过期或尚未通过验证；实时数值保持未知，等待新的只读采集。');
  if (robot.clock_unverified) issues.push(robot.transport?.freshness_verified ? '跨机时间未同步；当前读数按传输年龄验证，曲线保留机器人原始时间。' : '跨机时间尚未验证；实时数值保持未知，需核对时钟或取得有效的传输年龄证据。');
  const missing = robotMissingMotionTopics(robot);
  if (missing.length) issues.push(`等待${missing.join('、')}观测；关节未使能或数据源尚未发布时可暂无反馈，不能仅凭缺失判定设备异常。监控只订阅，不改变使能状态。`);
  if (!robot.health?.motors?.total || robot.health.motors.available < robot.health.motors.total) issues.push('电机温度与故障反馈尚不完整；需要运控提供带实际关节名称的只读诊断数据。');
  if (!robot.monitoring_configuration?.configuration?.physical_mapping_verified) issues.push('物理部位映射待现场核对；保留关节编号，暂不把温度绘制到身体位置。');
  if (robot.health?.battery?.temperature == null || robot.health?.battery?.protection == null) issues.push('电池温度或保护位未提供；BMS 日志中的电量、电压和带符号电流仍可独立采集。');
  setText('robot-readiness-summary', platformAvailable && robotHasCurrentObservations(robot) && !robot.data_fresh ? '部分观测可用，缺失项保持未知；不代表整机健康或实机验收通过。' : issues.length ? '已区分采集连接与各项数据是否可用，缺失观测保持未知。' : '当前已接收只读观测；数据来源不等于实机验收通过。');
  for (const issue of issues) { const item = document.createElement('li'); item.textContent = issue; list.append(item); }
}

function render() {
  window.rp1Pages?.update(robots, selected, {unavailable:!platformAvailable});
  window.rp1Recordings?.update(robots, selected, {unavailable:!platformAvailable});
  window.rp1OverviewViews?.refresh();
  window.rp1Presentation?.update(robots, selected, {unavailable:!platformAvailable});
  setText("robot-count", String(robots.length));
  setText("online-count", platformAvailable ? String(robots.filter((robot) => robot.online).length) : "未知");
  setText("fresh-count", platformAvailable ? String(robots.filter((robot) => robot.online && robot.data_fresh).length) : "未知");
  const list = byId("robot-list");
  const focusedRobot = list.contains(document.activeElement) ? document.activeElement.dataset.robotId : null;
  list.replaceChildren();
  for (const robot of robots) {
    const button = document.createElement("button");
    button.className = `robot-button${robot.robot_id === selected ? " selected" : ""}`;
    button.dataset.robotId = robot.robot_id;
    button.setAttribute("aria-pressed", String(robot.robot_id === selected));
    const name = document.createElement("div"); name.className = "robot-name"; name.textContent = robot.name;
    const source = document.createElement("span"); source.className = "pill warning"; source.textContent = robot.source_kind === "simulated" ? "模拟数据 · 非实机" : "ROS2来源 · 待验收";
    const meta = document.createElement("div"); meta.className = "robot-meta";
    const id = document.createElement("span"); id.textContent = robot.robot_id;
    const state = document.createElement("span"); state.className = platformAvailable && robot.online ? "good" : "warning"; state.textContent = !platformAvailable ? "平台连接中断" : !robot.enabled ? "已停用" : robot.online ? "采集在线" : "离线 / 未连接";
    meta.append(id, state); button.append(name, source, meta);
    button.addEventListener("click", () => window.rp1Pages ? window.rp1Pages.navigate('overview', robot.robot_id) : chooseRobot(robot.robot_id)); list.append(button);
    if (focusedRobot === robot.robot_id) button.focus({ preventScroll: true });
  }
  const robot = robots.find((item) => item.robot_id === selected);
  window.rp1Components?.update(robot, platformAvailable);
  window.rp1HostCores?.update(robot, platformAvailable);
  renderRobotReadiness(robot);
  if (!robot) { renderHealth(null); renderProcesses({},false); byId("joints-body").replaceChildren(); return; }
  const timeVerified = robot.transport?.freshness_verified === true;
  const liveAvailable = platformAvailable && robotRealtimeAllowed(robot);
  renderHealth(liveAvailable ? robot.health : null, robot.source_kind);
  renderProcesses(robot, liveAvailable, platformAvailable ? '时钟或传输新鲜度未通过验证' : '平台连接中断');
  setText("robot-title", robot.name); setText("robot-id", robot.robot_id);
  setText("source-kind", robot.source_kind === "simulated" ? "模拟数据 · 非实机" : "ROS2来源 · 实机待验收");
  setText("connection-state", !platformAvailable ? "平台连接中断" : robot.online ? "采集端在线" : "离线 / 未连接", platformAvailable && robot.online ? "good" : "warning");
  setText("received-at", `最后实时接收 ${timeText(robot.received_at)}`);
  const partial = robotHasCurrentObservations(robot) && !robot.data_fresh;
  setText("data-state", !platformAvailable ? "实时状态未知" : robot.online && robot.data_fresh ? "数据新鲜" : partial ? "部分观测可用" : "过期 / 未提供", platformAvailable && robot.online && robot.data_fresh ? "good" : "warning");
  const ages = robot.quality.topic_ages_ms || {};
  const age = ages["/joint_states"];
  const missingMotion = robotMissingMotionTopics(robot);
  setText("data-age", platformAvailable && partial && missingMotion.length ? `当前未提供或过期：${missingMotion.join('、')}` : `关节接收年龄 ${age == null ? "未提供" : `${Math.round(age + (robot.age_seconds || 0) * 1000)} ms`}`);
  setText("cache-loss", `采集端缓存历史丢弃 ${robot.quality.cache_discarded_windows || 0} 个窗口`, robot.quality.cache_discarded_windows ? "warning" : "muted");
  setText("clock-state", !platformAvailable ? "状态待确认" : robot.transport && !timeVerified ? "传输观测未验证 / 已过期" : robot.clock_unverified ? timeVerified ? "传输年龄已验证" : "时钟待核对" : "主机时间", !platformAvailable || robot.clock_unverified || (robot.transport && !timeVerified) ? "warning" : "good");
  const difference = robot.clock_offset_seconds;
  setText('clock-detail', !platformAvailable ? '平台连接中断，时间质量未知' : `${Number.isFinite(difference) ? `中央接收 − 采集窗口 ${difference >= 0 ? '+' : ''}${difference.toFixed(2)} s；` : ''}${robot.clock_unverified ? '跨机时间未同步，时间差含传输延迟' : '主机接收时间，非传感器硬件时间'}`);
  const signature = robot.robot_id + robot.catalog.map((item) => item.key).join("|");
  if (signature !== catalogSignature) {
    catalogSignature = signature;
    const select = byId("metric-select"), previous = select.value;
    select.replaceChildren();
    for (const entry of robot.catalog) { const option = document.createElement("option"); option.value = entry.key; option.textContent = `${entry.label} ${entry.unit}`; select.append(option); }
    select.value = robot.catalog.some((entry) => entry.key === previous) ? previous : robot.catalog.some((entry) => entry.key === "battery.soc") ? "battery.soc" : robot.catalog[0]?.key || "";
    queueMicrotask(() => loadCurve(true));
  }
  const joints = robotJointRoster(robot);
  const body = byId("joints-body"); body.replaceChildren();
  for (const [index, name] of joints) {
    const row = document.createElement("tr");
    const values = [`${index} · ${name}`, ...["position", "velocity", "torque", "policy_target"].map((quantity) => {
      const metric = robot.metrics[`joint.${index}.${quantity}`];
      const age = (metric?.age_ms ?? Infinity) + robotObservationAgeMs(robot);
      return format(!platformAvailable || !robot.online || !robotRealtimeAllowed(robot) || age > 1500 || (quantity === "policy_target" && robot.source_kind === "ros2" && robot.quality.target_alignment === "unverified") ? null : metric?.last);
    }), ...["temperature", "mos_temperature", "fault_code"].map((quantity) => {
      const value = liveAvailable ? robot.health?.motors?.items?.find((item) => item.joint === index)?.[quantity] : null;
      return value == null ? "—" : quantity === "fault_code" ? String(Math.trunc(value)) : value.toFixed(1);
    })];
    for (const value of values) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
    body.append(row);
  }
}

async function poll() {
  if (pollBusy || document.hidden) return;
  pollBusy = true;
  try {
    const response = await api("api/v1/robot-monitors");
    platformAvailable = true;
    robots = response.items;
    const previousSelection = selected;
    const requested = window.rp1Pages?.robotId();
    if (requested) selected = requested;
    else if (!robots.some((robot) => robot.robot_id === selected)) selected = robots[0]?.robot_id || null;
    if (previousSelection !== selected) { window.rp1Operations?.changed(); window.rp1Central?.changed(); window.rp1Thermal?.changed(); window.rp1Components?.changed(); window.rp1HostCores?.changed(); }
    render();
    byId("error").classList.add("hidden");
    byId("auth").classList.add("hidden");
    setText("refresh-status", `更新于 ${timeText(response.as_of_at)}`);
  } catch (error) {
    platformAvailable = false;
    window.rp1Presentation?.unavailable();
    render();
    setText("error", `${error.message}。当前显示为历史快照，不能代表实时状态。`);
    byId("error").classList.remove("hidden");
    setText("refresh-status", "平台连接异常");
    setText("connection-state", "状态待确认", "warning");
    setText("data-state", "历史快照", "warning");
    renderHealth(null);
    renderProcesses(robots.find((robot) => robot.robot_id === selected) || {}, false);
  } finally { pollBusy = false; }
}

function drawCurve(data, chartId = "chart", emptyId = "chart-empty", resolutionId = "chart-resolution") {
  const svg = byId(chartId); svg.replaceChildren();
  const points = data.points || [], valid = points.flatMap((point) => [point.last, point.minimum, point.maximum]).filter((value) => value != null && Number.isFinite(value));
  byId(emptyId).classList.toggle("hidden", valid.length > 0);
  if (!valid.length) return;
  const width = Math.max(240, (svg.clientWidth || 1000) - 28), height = Math.max(200, (svg.clientHeight || 270) - 5), left = 65, right = 20, top = 12, bottom = 32;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  let min = Math.min(...valid), max = Math.max(...valid);
  if (max === min) { min -= .1; max += .1; }
  const padding = (max - min) * .1; min -= padding; max += padding;
  const times = points.map((point) => new Date(point.time).getTime()), first = times[0], last = Math.max(first + 1000, times[times.length - 1]);
  const x = (value) => left + (value - first) / (last - first) * (width - left - right);
  const y = (value) => height - bottom - (value - min) / (max - min) * (height - top - bottom);
  const node = (tag, attributes, text) => { const result = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [key, value] of Object.entries(attributes)) result.setAttribute(key, String(value)); if (text != null) result.textContent = text; svg.append(result); return result; };
  for (let index = 0; index <= 4; index++) {
    const value = min + (max - min) * index / 4, position = y(value);
    node("line", { x1: left, x2: width - right, y1: position, y2: position, class: "grid" });
    node("text", { x: left - 8, y: position + 4, "text-anchor": "end" }, value.toFixed(3));
  }
  const ticks = width < 480 ? 2 : 4;
  for (let index = 0; index <= ticks; index++) {
    const tick = first + (last - first) * index / ticks;
    node("text", { x: x(tick), y: height - 8, "text-anchor": index === ticks ? "end" : index === 0 ? "start" : "middle" }, new Date(tick).toLocaleTimeString("zh-CN", { hour12: false }));
  }
  let path = "", connected = false, missing = 0;
  points.forEach((point, index) => {
    const position = x(times[index]);
    if (point.minimum != null && point.maximum != null) node("line", { x1: position, x2: position, y1: y(point.minimum), y2: y(point.maximum), class: "range" });
    if (point.last == null) { connected = false; missing++; return; }
    const gap = point.missing_windows || point.gap_count;
    if (gap) { connected = false; missing++; node("circle", { cx: position, cy: y(point.last), r: 2, fill: "#2b59e8" }); }
    path += `${connected ? "L" : "M"}${position.toFixed(2)},${y(point.last).toFixed(2)} `; connected = true;
    if (gap) connected = false;
  });
  node("path", { d: path, class: "value" });
  setText(resolutionId, `${points.length} 个摘要点 · ${data.unit || "无量纲"}${missing ? ` · ${missing} 个含缺失窗口` : ""}${data.display_time_basis ? ` · ${data.display_time_basis}` : ''}`);
}

async function loadCurve(force = false) {
  if (window.rp1Pages && !window.rp1Pages.isActive('history')) return;
  if ((!force && curveBusy) || document.hidden || !selected || !byId("metric-select").value) return;
  const version = ++historyVersion, robot = selected, metric = byId("metric-select").value;
  curveBusy = true;
  const {start,end,basis} = robotHistoryRange(robots.find(item => item.robot_id === robot), Number(byId("range-select").value));
  const params = new URLSearchParams({ metric, start: start.toISOString(), end: end.toISOString(), max_points: "1200", tier: byId("curve-tier").value });
  try {
    const data = await api(`api/v1/robot-monitors/${encodeURIComponent(robot)}/telemetry?${params}`);
    if (version === historyVersion) { curveData = {...data,display_time_basis:basis}; if (!window.rp1Pages || window.rp1Pages.isActive('history')) drawCurve(curveData); }
  } catch (error) { if (version === historyVersion) setText("chart-resolution", `历史查询失败：${error.message}`); }
  finally { if (version === historyVersion) curveBusy = false; }
}

window.rp1RefreshView = view => {
  window.rp1Components?.activate(view);
  if (view === 'history') loadCurve(true);
  if (view === 'storage') window.rp1Central?.refresh();
};

byId("refresh-button").addEventListener("click", async () => { await poll(); await loadCurve(true); window.rp1Components?.refresh(); });
byId("auth-button").addEventListener("click", async () => { token = byId("viewer-token").value.trim(); byId("viewer-token").value = ""; await poll(); await loadCurve(true); });
function changeCurveSelection() {
  curveData = null; historyVersion++;
  byId("chart").replaceChildren(); byId("chart-empty").classList.remove("hidden");
  setText("chart-resolution", "正在查询所选数据"); loadCurve(true);
}
byId("metric-select").addEventListener("change", changeCurveSelection);
byId("range-select").addEventListener("change", changeCurveSelection);
byId("curve-tier").addEventListener("change", changeCurveSelection);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { poll(); loadCurve(true); } });
new ResizeObserver(() => { if (curveData && (!window.rp1Pages || window.rp1Pages.isActive('history'))) drawCurve(curveData); }).observe(byId("chart"));
poll().then(() => loadCurve(true));
setInterval(poll, 1000);
let lastCurvePoll = 0;
setInterval(() => {
  const range = Number(byId("range-select").value);
  const interval = range > 3600 ? 60000 : range > 600 ? 10000 : 2000;
  if (Date.now() - lastCurvePoll >= interval) { lastCurvePoll = Date.now(); loadCurve(); }
}, 1000);
