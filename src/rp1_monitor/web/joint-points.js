/* Separate axes near a configured body anchor; offsets are diagram layout only. */
(() => {
  const el = id => document.getElementById(id);
  function points(view) {
    const result = [];
    for (const anchor of view?.regions || []) {
      if (!Number.isFinite(anchor.x) || !Number.isFinite(anchor.y)) continue;
      const members = (view?.rows || []).filter(row => row.region === anchor.key).sort((a,b) => a.joint-b.joint);
      members.forEach((row,index) => {
        const columns = Math.min(3,members.length), line = Math.floor(index/columns);
        const lineCount = Math.min(columns,members.length-line*columns);
        let dx = (index%columns-(lineCount-1)/2)*.088, dy = line*.055;
        // Wrist markers sit outside the hip cluster in the existing waving image.
        if (anchor.key.endsWith('_wrist')) {
          dx = anchor.x > .5 ? .05 : -.05;
          dy = (index-(members.length-1)/2)*.06;
        }
        result.push({...row,key:`joint-${row.joint}`,x:anchor.x+dx,y:anchor.y+dy});
      });
    }
    return result;
  }
  function renderUnmapped(view, focusedJoint = null) {
    const mapped = new Set(points(view).map(point => point.joint));
    const pending = (view?.rows || []).filter(row => !mapped.has(row.joint));
    el('joint-map-coverage').textContent = view ? `${(view.rows || []).length} 个关节 · 图上 ${mapped.size} 个独立标记 · 位置待确认 ${pending.length} 个` : '关节数量与位置等待设备数据';
    const root = el('unmapped-joint-list'); root.replaceChildren();
    el('unmapped-joints').hidden = !pending.length;
    for (const row of pending) {
      const button = document.createElement('button'); button.type = 'button'; button.dataset.joint = String(row.joint);
      button.dataset.state = row.state; button.title = row.detail;
      button.setAttribute('aria-label', `joint.${row.joint} · ${row.label}，位置待确认，查看温度曲线与告警`);
      const label = document.createElement('span'); label.textContent = `J${String(row.joint).padStart(2,'0')}`;
      const value = document.createElement('strong'); value.textContent = row.value == null ? '— °C' : `${row.value.toFixed(1)} °C`;
      const swatch = document.createElement('span'); swatch.className = 'unmapped-joint-swatch'; swatch.style.setProperty('--joint-color', row.color);
      const state = document.createElement('small'); state.textContent = row.state === 'hot' ? '超温 · 位置待确认' : row.state === 'unknown' || row.state === 'unconfigured' ? '温度判据未知' : '位置待确认';
      button.append(label,value,swatch,state);
      button.addEventListener('click', () => window.rp1Thermal?.openJoint(row.joint));
      root.append(button);
      if (focusedJoint === String(row.joint)) button.focus({preventScroll:true});
    }
  }
  window.rp1JointMap = {points,renderUnmapped};
})();
