"use strict";
(() => {
  let busy = false, version = 0, storageCache = null, lastStorageRead = 0;
  const bytes = value => value == null ? "—" : value < 1024 ? `${value} B` : value < 1024**2 ? `${(value/1024).toFixed(1)} KiB` : value < 1024**3 ? `${(value/1024**2).toFixed(1)} MiB` : `${(value/1024**3).toFixed(2)} GiB`;
  const labels = {normal:"当前正常", pending:"等待持续确认", active:"告警中", unknown:"观测未知"};
  const eventLabels = {opened:"触发", recovered:"恢复", observation_lost:"观测中断", observation_restored:"恢复观测"};
  function renderAlerts(result) {
    setText("central-limit-note", `${result.temperature_limits_configured ? "温度限值已按服务端策略配置" : "当前未配置温度限值"}。规则确认正常才记录恢复；失联、过期或规则移除保留为未知。模拟事件不代表实机故障。`);
    const items = result.items || [], root = byId("central-alerts"); root.replaceChildren();
    const stale = result.worker_error || items.some(item => !item.evaluation_fresh);
    setText("central-alert-count", stale ? "评估过期 / 异常" : `未恢复 ${items.filter(item=>item.is_open).length} · 观测未知 ${items.filter(item=>item.state === "unknown").length}`);
    for (const item of items) {
      const details = document.createElement("details"); details.className = "central-rule";
      const summary = document.createElement("summary"), state = document.createElement("span"), title = document.createElement("span");
      state.className = !item.evaluation_fresh || item.state !== "normal" ? "warning" : "good";
      state.textContent = !item.evaluation_fresh ? "评估已过期" : labels[item.state];
      title.textContent = item.rule_key;
      summary.append(state, title);
      const text = document.createElement("p"); text.textContent = item.evidence.detail;
      const pre = document.createElement("pre"); pre.textContent = JSON.stringify({evaluated_at:item.evaluated_at, opened_at:item.opened_at, is_open:item.is_open, evidence:item.evidence}, null, 2);
      details.append(summary, text, pre); root.append(details);
    }
    if (!items.length) { const p = document.createElement("p"); p.textContent = "尚无规则评估，当前状态未知。"; root.append(p); }
    if (result.worker_error) { const p = document.createElement("p"); p.textContent = result.worker_error; root.prepend(p); }
  }
  function renderEvents(result) {
    const root = byId("central-events"); root.replaceChildren();
    for (const item of result.items || []) {
      const p = document.createElement("p");
      p.textContent = `${new Date(item.occurred_at).toLocaleString("zh-CN", {hour12:false})} · ${eventLabels[item.event_kind]} · ${item.rule_key} · ${item.evidence.source_kind === "simulated" ? "模拟" : "ROS2待验收"}`;
      p.title = item.evidence.detail; root.append(p);
    }
    if (!result.items?.length) root.textContent = "暂无状态变化记录；不等于设备健康验收通过。";
  }
  function renderStorage(report, preview, id) {
    const item = report.items.find(item => item.robot_id === id), expired = preview.items.find(item=>item.robot_id === id);
    setText("storage-physical", bytes(report.physical_bytes)); setText("storage-scope", report.physical_scope);
    setText("storage-device", item ? `${bytes(item.raw_json_bytes)} / ${bytes(item.key_json_bytes)}` : "尚无新增计量窗口");
    setText("storage-count", `去重接收窗口数 ${item?.windows ?? 0} · 旧数据需显式回填`);
    setText("storage-estimate", item ? `${bytes(item.estimated_30day_1hz_json_bytes.raw)} / ${bytes(item.estimated_30day_1hz_json_bytes.key)}` : "—");
    setText("storage-retention", `${preview.retention_days}天到期预览：${expired?.windows ?? 0} 个窗口，其中归档清单覆盖 ${expired?.archive_manifest_covered_windows ?? 0}。原始与关键数据均保留，不自动删除。`);
    setText("storage-disk", report.central_disk.detail);
    setText("storage-policy", JSON.stringify({policy_sha256:report.policy_sha256, policy:report.policy, received_days:report.days.filter(row=>row.robot_id === id), meaning:report.meaning}, null, 2));
  }
  async function refresh(force = false) {
    if (window.rp1Pages && !window.rp1Pages.isActive('storage')) return;
    if ((!force && busy) || document.hidden || !selected) return;
    const id = selected, requestVersion = ++version;
    busy = true;
    try {
      const base = `api/v1/robot-monitors/${encodeURIComponent(id)}`;
      const reloadStorage = force || !storageCache || Date.now() - lastStorageRead > 60000;
      const [alerts, events, usage, preview] = await Promise.all([api(`${base}/alerts`), api(`${base}/alert-events?limit=20`), reloadStorage ? api("api/v1/monitor-storage/usage") : storageCache[0], reloadStorage ? api("api/v1/monitor-storage/retention-preview") : storageCache[1]]);
      if (requestVersion !== version || selected !== id) return;
      if (reloadStorage) { storageCache = [usage, preview]; lastStorageRead = Date.now(); }
      renderAlerts(alerts); renderEvents(events); renderStorage(usage, preview, id);
      const source = robots.find(row=>row.robot_id === id)?.source_kind === "simulated" ? "模拟数据 · 非实机" : "ROS2来源 · 待验收";
      setText("central-status", `${id} · ${source} · 更新于 ${new Date().toLocaleTimeString("zh-CN", {hour12:false})}`, "muted");
    } catch (error) {
      if (requestVersion !== version || selected !== id) return;
      setText("central-status", `${error.message}；留存与告警显示为历史快照，当前状态未知。`, "warning");
      setText("central-alert-count", "当前未知"); byId("central-alerts").replaceChildren();
    } finally { if (requestVersion === version) busy = false; }
  }
  window.rp1Central = {refresh:()=>refresh(true), changed:()=>{
    version++; busy=false;
    byId("central-alerts").replaceChildren(); byId("central-events").replaceChildren();
    for (const id of ['storage-device','storage-count','storage-estimate','storage-retention','storage-policy','central-alert-count','central-limit-note']) setText(id, '当前设备数据待读取');
    setText("central-status", "切换设备，等待读取当前上下文"); refresh(true);
  }};
  byId("central-refresh").addEventListener("click", ()=>refresh(true));
  setInterval(()=>refresh(), 5000);
  refresh();
})();
