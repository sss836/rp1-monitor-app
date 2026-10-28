/* Separate read-only component pages. Statistics belong to the queried time range. */
(() => {
  const el = id => document.getElementById(id), kinds = ['battery','joints','host'];
  const charts = {battery:['battery-soc','battery-temperature','battery-voltage','battery-current'], joints:['joints-temperature','joints-mos','joints-torque','joints-rms'], host:['host-cpu','host-memory','host-temperature']};
  let robot = null, available = false, generation = 0, activeKey = '', inflight = null, result = null, lastRead = 0, jointSignature = '';
  let records = {robotId:null,items:[],cursor:null,loaded:false,busy:false,version:0}, preferredRecord = '';
  const mode = kind => el(kind+'-view-mode')?.value || 'live';
  const currentRecord = kind => records.items.find(item=>item.id === el(kind+'-recording')?.value);
  const customDate = value => {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value)) throw new Error('请填写北京时间的开始和结束时间');
    const date = new Date(value+(value.length === 16 ? ':00' : '')+'+08:00');
    if (!Number.isFinite(date.getTime())) throw new Error('时间格式无效');
    return date;
  };
  const beijingInput = date => new Date(date.getTime()+8*3600000).toISOString().slice(0,16);
  function context(kind) {
    const record = mode(kind) === 'recording' ? currentRecord(kind) : null;
    document.dispatchEvent?.(new CustomEvent('rp1:component-context',{detail:{robotId:selected,mode:mode(kind),recordingId:record?.id || null,runNumber:record?.run_number || null}}));
  }
  function field(id,label,node) {
    const wrapper = document.createElement('div'); wrapper.className = 'component-time-field';
    const name = document.createElement('label'); name.htmlFor = id; name.textContent = label;
    node.id = id; wrapper.append(name,node); return wrapper;
  }
  function setupControls(kind) {
    const controls = el(kind+'-range').closest?.('.component-controls');
    if (!controls) return;
    controls.classList.add('component-time-controls');
    const selector = document.createElement('select');
    selector.replaceChildren(new Option('实时查看','live'),new Option('按记录查看','recording'),new Option('自定义时间','custom'));
    controls.prepend(field(kind+'-view-mode','查看方式',selector));
    const recording = document.createElement('select'); recording.replaceChildren(new Option('请选择本机监控记录',''));
    const recordField = field(kind+'-recording','监控记录',recording); recordField.classList.add('component-recording-field'); recordField.hidden = true;
    const more = document.createElement('button'); more.id = kind+'-recordings-more'; more.type = 'button'; more.textContent = '加载更早记录'; more.hidden = true; more.className = 'component-recordings-more';
    ['start','end'].forEach((suffix,index)=>{
      const input = document.createElement('input'); input.type = 'datetime-local'; input.step = '60';
      input.value = beijingInput(new Date(Date.now()-(index ? 0 : 3600000)));
      const wrapper = field(kind+'-'+suffix,(index ? '结束' : '开始')+'时间（北京时间）',input);
      wrapper.classList.add('component-custom-field'); wrapper.hidden = true; controls.insertBefore(wrapper,el(kind+'-refresh'));
      input.addEventListener('change',()=>{selectionChanged();refresh(true);});
    });
    controls.insertBefore(recordField,el(kind+'-refresh')); controls.insertBefore(more,el(kind+'-refresh'));
    const report = document.createElement('a'); report.id = kind+'-recording-report'; report.className = 'component-recording-report'; report.textContent = '查看本次记录与报告 ↗'; report.href = '#'; report.hidden = true; controls.append(report);
    report.addEventListener('click',event=>{event.preventDefault(); const record = currentRecord(kind); if (mode(kind) !== 'recording' || record?.robot_id !== selected) return; window.rp1Pages?.navigate('recordings',selected); document.dispatchEvent(new CustomEvent('rp1:recording-select',{detail:{recordingId:record.id}}));});
    selector.addEventListener('change',()=>{selectionChanged();syncMode(kind);update(robot,available);refresh(true);});
    recording.addEventListener('change',()=>{preferredRecord = recording.value; selectionChanged();syncMode(kind);update(robot,available);refresh(true);});
    more.addEventListener('click',()=>loadRecords(kind,true));
  }
  function syncMode(kind) {
    const value = mode(kind), record = currentRecord(kind), controls = el(kind+'-range').closest?.('.component-controls');
    if (controls) {
      el(kind+'-range').parentElement.hidden = value !== 'live';
      el(kind+'-recording').parentElement.hidden = value !== 'recording';
      el(kind+'-recordings-more').hidden = value !== 'recording' || !records.cursor;
      el(kind+'-recording-report').hidden = value !== 'recording' || !record;
      for (const suffix of ['start','end']) el(kind+'-'+suffix).parentElement.hidden = value !== 'custom';
      const panel = controls.closest('[data-page-panel]');
      for (const label of panel.querySelectorAll('.component-kpis .label')) { label.dataset.liveText ||= label.textContent; label.textContent = value === 'live' ? label.dataset.liveText : label.dataset.liveText.replace(/^当前最高热区温度$/,'时段末次所选热区温度').replace(/^本次开机运行时长$/,'时段末次开机运行时长').replace(/^当前/,'时段末次'); }
      for (const label of panel.querySelectorAll('.chart-legend .legend-line')) { label.dataset.liveText ||= label.textContent; label.textContent = value === 'recording' ? '保存样本分桶均值' : label.dataset.liveText; }
      for (const label of panel.querySelectorAll('.chart-legend .legend-range')) { label.dataset.liveText ||= label.textContent; label.textContent = value === 'recording' ? '保存样本分桶极值' : label.dataset.liveText; }
      for (const subtitle of panel.querySelectorAll('.component-chart-subtitle')) { subtitle.dataset.liveText ||= subtitle.textContent; subtitle.textContent = value === 'recording' ? subtitle.dataset.liveText.replace('每秒摘要，非完整高频曲线','仅当时勾选保存的样本；保留缺失/中断') : subtitle.dataset.liveText; }
      const cores = panel.querySelector('.host-core-panel'); if (cores) cores.hidden = value !== 'live';
    }
    if (active() && page() === kind) context(kind);
  }
  function fillRecordChoices() {
    for (const kind of kinds) {
      const selector = el(kind+'-recording'); if (!selector) continue;
      const previous = preferredRecord || selector.value;
      selector.replaceChildren(new Option(records.items.length ? '请选择本机监控记录' : '尚无本机监控记录',''),...records.items.map(record=>new Option(`${record.run_number} · ${record.frequency_hz} Hz${record.source_kind === 'simulated' ? ' · 模拟' : ''}`,record.id)));
      selector.value = records.items.some(record=>record.id === previous) ? previous : '';
      const more = el(kind+'-recordings-more'); if (more) { more.hidden = mode(kind) !== 'recording' || !records.cursor; more.disabled = records.busy; }
    }
  }
  async function loadRecords(kind,more=false) {
    if (!active() || document.hidden || mode(kind) !== 'recording' || records.busy || (more && !records.cursor)) return;
    const id = selected, version = ++records.version, before = more ? records.cursor : null; records.busy = true;
    const params = new URLSearchParams({robot_id:id,limit:'200'}); if (before) params.set('before',before);
    el(kind+'-recordings-more').disabled = true; text(kind+'-status','正在读取本机监控记录…');
    try {
      const data = await api(`api/v1/monitor-recordings?${params}`,30000);
      if (version !== records.version || selected !== id) return;
      if (!Array.isArray(data.items) || data.items.some(item=>item.robot_id !== id)) throw new Error('监控记录与当前机器人不一致');
      const combined = more ? [...records.items,...data.items] : data.items;
      records.items = [...new Map(combined.map(item=>[item.id,item])).values()]; records.robotId = id; records.cursor = data.next_cursor || null; records.loaded = true;
      fillRecordChoices();
      if (!records.items.length) { text(kind+'-status','本机器人尚无监控记录；请在上位机开始长期记录。'); return; }
      if (page() === kind && active() && mode(kind) === 'recording' && !document.hidden) { update(robot,available);syncMode(kind);refresh(true); }
    } catch(error) { if (version === records.version && selected === id && page() === kind) text(kind+'-status',`读取记录失败：${error.message}。点击“刷新数据”重试。`); }
    finally { if (version === records.version) { records.busy = false; if (el(kind+'-recordings-more')) el(kind+'-recordings-more').disabled = false; } }
  }
  const number = (value, unit, digits=1) => Number.isFinite(value) ? `${value.toFixed(digits)} ${unit}` : '—';
  const text = (id, value) => { if (el(id)) el(id).textContent = value; };
  const page = () => window.rp1Pages?.currentPage();
  const active = () => kinds.includes(page()) && window.rp1Pages.isActive(page());
  function duration(value) {
    if (!Number.isFinite(value) || value < 0) return '—';
    return `${Math.floor(value/86400)}天 ${String(Math.floor(value/3600)%24).padStart(2,'0')}:${String(Math.floor(value/60)%60).padStart(2,'0')}:${String(Math.floor(value)%60).padStart(2,'0')}`;
  }
  function clearStats() {
    for (const id of ['battery-page-current-peak','battery-page-voltage-range','joints-page-rms','joints-page-temperature-max','joints-page-torque-peak']) text(id,'—');
    text('battery-current-extrema','所选时段电流极值待读取');
    text('joints-rms-note','等待有效配对样本');
  }
  function clearCurves() {
    result = null; clearStats();
    for (const kind of kinds) if (mode(kind) !== 'live') for (const id of historicalIds[kind]) text(id,'—');
    for (const prefixes of Object.values(charts)) for (const prefix of prefixes) {
      el(prefix+'-chart').replaceChildren(); el(prefix+'-empty').classList.remove('hidden');
      text(prefix+'-resolution','等待查询当前设备与所选时段');
    }
  }
  function changed() {
    generation++; activeKey = ''; inflight = null; lastRead = 0; jointSignature = '';
    records = {robotId:null,items:[],cursor:null,loaded:false,busy:false,version:records.version+1}; preferredRecord = ''; fillRecordChoices();
    clearCurves();
    for (const kind of kinds) syncMode(kind);
    el('host-thermal-select').replaceChildren(new Option('等待热区数据',''));
    el('host-thermal-select').dataset.signature = '';
    for (const kind of kinds) { el(kind+'-refresh').disabled = false; text(kind+'-status','设备已切换，等待查询当前设备。'); }
  }
  function update(snapshot, online) {
    robot = snapshot || null; available = !!online;
    const thermalSelector = el('host-thermal-select');
    const thermalKeys = new Set([...(robot?.catalog || []).map(item=>item.key),...(robot?.health?.host?.sensors || []).map(sensor=>sensor.key)]);
    if (mode('host') === 'live' && available && thermalSelector.value && !thermalKeys.has(thermalSelector.value)) {
      if (page() === 'host') selectionChanged();
      thermalSelector.replaceChildren(new Option('等待热区数据',''));
      thermalSelector.dataset.signature = '';
    }
    const timeVerified = robot?.transport?.freshness_verified === true;
    const realtimeAllowed = window.rp1RobotView?.realtimeAllowed(robot) ?? (robot?.transport ? timeVerified : !robot?.clock_unverified);
    const health = available && realtimeAllowed ? robot?.health : null, battery = health?.battery || {}, host = health?.host || {};
    if (mode('battery') === 'live') {
      text('battery-page-soc',number(battery.soc,'%')); text('battery-page-temp',number(battery.temperature,'°C'));
      text('battery-page-voltage',number(battery.voltage,'V',2)); text('battery-page-current',number(battery.current,'A',2));
    }
    if (mode('host') === 'live') {
      text('host-page-cpu',number(host.cpu_usage,'%')); text('host-page-memory',number(host.memory_usage,'%'));
      text('host-page-temperature',number(host.max_temperature,'°C')); text('host-page-uptime',duration(host.uptime_seconds));
      text('host-uptime-note',!Number.isFinite(host.uptime_seconds) ? '未提供有效开机时长' : robot?.source_kind === 'simulated' ? '模拟源本次启动时长 · 非实机' : '机器人主机 /proc/uptime · 采集快照');
    }
    const rows = robot?.health?.motor_map?.rows || [];
    const configuration = robot?.monitoring_configuration;
    const declared = configuration?.source === 'unconfigured' ? [] : configuration?.configuration?.joints || [];
    let names = window.rp1RobotView?.joints(robot) || (rows.length ? rows.map(row=>[row.joint,row.label]) : declared.length ? declared.map(item=>[item.index,item.joint_name || item.label]) : [...new Set((robot?.catalog || []).filter(item=>item.key.startsWith('joint.')).map(item=>Number(item.key.split('.')[1])))].sort((a,b)=>a-b).map(index=>[index,`关节 ${index}`]));
    if (mode('joints') === 'recording' && currentRecord('joints')) {
      const record = currentRecord('joints'), keys = record.metrics || [];
      names = [...new Set(keys.filter(key=>/^joint\.\d+\./.test(key)).map(key=>Number(key.split('.')[1])))].sort((a,b)=>a-b).map(index=>[index,record.source_context?.joint_names?.[index] || `关节 ${index}`]);
    }
    const signature = JSON.stringify([robot?.robot_id,names]);
    if (signature !== jointSignature) {
      jointSignature = signature; const previous = el('joints-page-select').value;
      el('joints-page-select').replaceChildren(...names.map(([joint,label])=>new Option(`J${String(joint).padStart(2,'0')} · ${label}`,String(joint))));
      if (names.some(([joint])=>String(joint) === previous)) el('joints-page-select').value = previous;
      if (!names.length) el('joints-page-select').add(new Option('尚未提供关节配置',''));
    }
    const index = Number(el('joints-page-select').value), motor = health?.motors?.items?.find(item=>item.joint === index);
    if (mode('joints') === 'live') { text('joints-page-temperature',number(motor?.temperature,'°C')); text('joints-page-mos',number(motor?.mos_temperature,'°C')); }
    const torque = robot?.metrics?.[`joint.${index}.torque`];
    const elapsed = window.rp1RobotView?.observationAgeMs(robot) ?? (robot?.transport ? Number.isFinite(robot.transport.elapsed_seconds) ? robot.transport.elapsed_seconds*1000 : Infinity : ((robot?.age_seconds||0)+Math.max(0,robot?.clock_offset_seconds||0))*1000);
    const fresh = available && robot?.online && realtimeAllowed && torque && Number.isFinite(torque.age_ms) && torque.age_ms+elapsed <= 1000;
    if (mode('joints') === 'live') text('joints-page-torque',number(fresh ? torque.last : null,'N·m',3));
    if (!available) for (const kind of kinds) if (mode(kind) === 'live') text(kind+'-status','平台连接中断，实时读数未知；已有曲线为查询快照。');
    if (active() && !document.hidden) refresh();
  }
  function render(data) {
    const metrics = new Map(data.curves.map(curve=>[curve.metric,curve]));
    const draw = (prefix,key) => {
      const curve = metrics.get(key) || {points:[]};
      text(prefix+'-resolution','当前范围尚无有效数据');
      drawCurve(curve,prefix+'-chart',prefix+'-empty',prefix+'-resolution');
    };
    if (data.component === 'battery') {
      for (const quantity of ['soc','temperature','voltage','current']) draw('battery-'+quantity,'battery.'+quantity);
      if (robot?.source_kind === 'ros2' && robot?.quality?.device_profile?.bms_type === 'journal' && !metrics.get('battery.temperature')?.points?.some(point=>Number.isFinite(point.last))) text('battery-temperature-empty','当前 journal BMS 日志未提供电池温度；不以主机温度代替，缺失不补零。');
      text('battery-page-current-peak',number(data.stats.current_peak_abs,'A',3));
      text('battery-page-voltage-range',`${number(data.stats.voltage_min,'V',3)} / ${number(data.stats.voltage_max,'V',3)}`);
      text('battery-current-extrema',`电流最低 ${number(data.stats.current_min,'A',3)} · 最高 ${number(data.stats.current_max,'A',3)}`);
    } else if (data.component === 'joints') {
      const base = `joint.${data.joint}.`;
      for (const [suffix,metric] of [['temperature','motor_temperature'],['mos','mos_temperature'],['torque','torque'],['rms','tracking_rms']]) draw('joints-'+suffix,base+metric);
      text('joints-page-rms',number(data.stats.tracking_rms,'rad',5));
      text('joints-page-temperature-max',`${number(data.stats.motor_temperature_max,'°C')} / ${number(data.stats.mos_temperature_max,'°C')}`);
      text('joints-page-torque-peak',number(data.stats.torque_peak_abs,'N·m',3));
      text('joints-rms-note',`${data.stats.tracking_samples || 0} 个有效配对样本 · 策略目标对照`);
      if (!data.stats.tracking_samples) text('joints-rms-empty','尚无同步反馈/策略目标配对样本；不从摘要末值推算 RMS。');
    } else {
      draw('host-cpu','host.cpu.usage'); draw('host-memory','host.memory.usage');
      const thermal = data.thermal_metrics || data.curves.filter(curve=>curve.metric.startsWith('host.thermal.')).map(curve=>curve.metric);
      const selector = el('host-thermal-select'), previous = selector.value;
      const signature = data.recording ? `recording:${data.recording.id}:${thermal.join('|')}` : thermal.join('|');
      if (selector.dataset.signature !== signature) {
        selector.dataset.signature = signature;
        selector.replaceChildren(...thermal.map(metric=>new Option(robot?.health?.host?.sensors?.find(sensor=>sensor.key === metric)?.label || metric,metric)));
        if (thermal.includes(previous)) selector.value = previous;
        if (!thermal.length) selector.add(new Option('未提供热区数据',''));
      }
      draw('host-temperature',data.selected_thermal || selector.value);
    }
    if (data.recording) {
      for (const prefix of charts[data.component]) {
        text(prefix+'-resolution',el(prefix+'-resolution').textContent.replace('个摘要点','个保存样本分桶')+' · 保存频率 '+data.recording.frequency_hz+' Hz');
        if (!el(prefix+'-empty').classList.contains('hidden')) text(prefix+'-empty','所选记录尚无此指标的有效保存样本；缺失不补零。');
      }
      if (data.component === 'joints') text('joints-rms-note','整段 RMS 未知：不能从分桶 RMS 均值重新计算；下方仅展示保存的配对 RMS 样本。');
    }
  }
  function metricsFor(kind,joint,thermal) {
    if (kind === 'battery') return ['soc','temperature','voltage','current'].map(key=>'battery.'+key);
    if (kind === 'joints') return ['motor_temperature','mos_temperature','torque','tracking_rms'].map(key=>`joint.${joint}.${key}`);
    return ['host.cpu.usage','host.memory.usage','host.uptime.seconds',...(thermal ? [thermal] : [])];
  }
  function recordedData(data,kind,joint,record,metrics,thermal) {
    if (!Array.isArray(data.points) || !Array.isArray(data.metrics) || data.metrics.length !== metrics.length || data.metrics.some(item=>!metrics.includes(item.key)) || new Set(data.metrics.map(item=>item.key)).size !== metrics.length || !Array.isArray(data.sessions) || data.sessions.some(item=>item.id !== record.id || item.robot_id !== record.robot_id)) throw new Error('记录曲线与当前设备、记录或指标不一致');
    const curves = metrics.map(key=>({metric:key,unit:data.metrics.find(item=>item.key === key)?.unit || '',points:data.points.map(point=>{
      const value = point.values?.[key];
      const saved = record.metrics.includes(key), incomplete = !saved || !value || value.valid < value.total;
      return {time:point.time,last:!incomplete && Number.isFinite(value?.value) ? value.value : null,minimum:saved ? value?.minimum ?? null : null,maximum:saved ? value?.maximum ?? null : null,samples:saved ? value?.valid || 0 : 0,gap_count:point.gap_before || incomplete ? 1 : 0};
    })}));
    const limits = key => {
      const points = curves.find(curve=>curve.metric === key)?.points || [];
      const low = points.map(point=>point.minimum).filter(Number.isFinite), high = points.map(point=>point.maximum).filter(Number.isFinite);
      return {min:low.length ? Math.min(...low) : null,max:high.length ? Math.max(...high) : null};
    };
    const current = limits('battery.current'), voltage = limits('battery.voltage'), torque = limits(`joint.${joint}.torque`);
    const peak = extrema => extrema.min === null && extrema.max === null ? null : Math.max(Math.abs(extrema.min ?? 0),Math.abs(extrema.max ?? 0));
    return {robot_id:record.robot_id,component:kind,joint,source_kind:record.source_kind,recording:record,curves,start:data.start || record.started_at,end:data.end || record.ended_at || record.started_at,thermal_metrics:record.metrics.filter(key=>/^host\.thermal\.\d+\.temperature$/.test(key)),selected_thermal:thermal,
      stats:{current_min:current.min,current_max:current.max,current_peak_abs:peak(current),voltage_min:voltage.min,voltage_max:voltage.max,tracking_rms:null,tracking_samples:0,motor_temperature_max:limits(`joint.${joint}.motor_temperature`).max,mos_temperature_max:limits(`joint.${joint}.mos_temperature`).max,torque_peak_abs:peak(torque)}};
  }
  function historicalCards(data) {
    const last = key => data.curves.find(curve=>curve.metric === key)?.points?.at(-1)?.last;
    if (data.component === 'battery') {
      for (const [id,key,unit,digits] of [['soc','soc','%',1],['temp','temperature','°C',1],['voltage','voltage','V',2],['current','current','A',2]]) text('battery-page-'+id,number(last('battery.'+key),unit,digits));
    } else if (data.component === 'joints') {
      for (const [id,key,unit,digits] of [['temperature','motor_temperature','°C',1],['mos','mos_temperature','°C',1],['torque','torque','N·m',3]]) text('joints-page-'+id,number(last(`joint.${data.joint}.${key}`),unit,digits));
    } else {
      text('host-page-cpu',number(last('host.cpu.usage'),'%')); text('host-page-memory',number(last('host.memory.usage'),'%'));
      text('host-page-temperature',number(last(data.selected_thermal || el('host-thermal-select').value),'°C'));
      text('host-page-uptime',duration(last('host.uptime.seconds'))); text('host-uptime-note','所选时段末次保存的开机时长；未保存则未知');
    }
  }
  async function refresh(force=false) {
    if (!active() || document.hidden || !robot || robot.robot_id !== selected) return;
    const kind = page(), view = mode(kind), record = currentRecord(kind);
    if (!available && view === 'live') return;
    if (view === 'recording' && !record) {
      if (!records.loaded || force && !records.items.length) loadRecords(kind);
      else text(kind+'-status','请选择本机器人的监控记录；历史仅包含当时勾选并已保存的指标。');
      return;
    }
    if (view === 'recording' && record.robot_id !== selected) return;
    const joint = kind === 'joints' ? el('joints-page-select').value : '', range = el(kind+'-range').value;
    if (kind === 'joints' && !joint) { text(kind+'-status',view === 'recording' ? '本次记录未保存关节指标，不能用实时摘要补充。' : '等待关节配置。'); return; }
    if (view === 'recording' && kind === 'host') {
      const keys = record.metrics.filter(key=>/^host\.thermal\.\d+\.temperature$/.test(key)), selector = el('host-thermal-select');
      if (selector.dataset.signature !== `recording:${record.id}:${keys.join('|')}`) {
        const previous = selector.value; selector.replaceChildren(...keys.map(key=>new Option(key,key)));
        if (keys.includes(previous)) selector.value = previous;
        if (!keys.length) selector.add(new Option('本次未保存热区',''));
        selector.dataset.signature = `recording:${record.id}:${keys.join('|')}`;
      }
    }
    const thermal = kind === 'host' ? el('host-thermal-select').value : '';
    if (thermal && !/^host\.thermal\.\d+\.temperature$/.test(thermal)) { text(kind+'-status','请选择有效的主机热区'); return; }
    const startValue = el(kind+'-start')?.value || '', endValue = el(kind+'-end')?.value || '';
    const key = [selected,kind,view,record?.id,joint,range,thermal,startValue,endValue].join('|');
    if (inflight === key) return;
    const interval = view !== 'live' ? 60000 : Number(range) > 3600 ? 60000 : Number(range) > 600 ? 30000 : 10000;
    if (!force && activeKey === key && Date.now()-lastRead < interval) return;
    if (activeKey !== key) clearCurves();
    activeKey = key; inflight = key; lastRead = Date.now();
    el(kind+'-refresh').disabled = true;
    const ticket = ++generation, id = selected;
    if (!result || force) text(kind+'-status','正在读取所选时段的曲线和统计…');
    let timing;
    try {
      let data;
      if (view === 'recording') {
        const metrics = metricsFor(kind,joint,thermal), params = new URLSearchParams({robot_id:id,recording_id:record.id,max_points:'300'});
        metrics.forEach(metric=>params.append('metrics',metric));
        data = recordedData(await api(`api/v1/monitor-recordings/curves?${params}`,30000),kind,joint,record,metrics,thermal);
      } else {
        timing = view === 'custom' ? {start:customDate(startValue),end:customDate(endValue),basis:'北京时间指定区间'} : window.rp1RobotView?.historyRange(robot,Number(range));
        const end = timing?.end || new Date(), start = timing?.start || new Date(end.getTime()-Number(range)*1000);
        if (start >= end) throw new Error('开始时间必须早于结束时间');
        const params = new URLSearchParams({component:kind,start:start.toISOString(),end:end.toISOString(),max_points:'300'});
        if (kind === 'joints') params.set('joint',joint);
        if (thermal) params.set('thermal',thermal);
        data = await api(`api/v1/robot-monitors/${encodeURIComponent(id)}/component-history?${params}`,30000);
      }
      if (ticket !== generation || selected !== id || page() !== kind || mode(kind) !== view || !active()) return;
      if (data.robot_id !== id || data.component !== kind || (kind === 'joints' && String(data.joint) !== joint)) throw new Error('返回证据与当前设备或关节不一致，请刷新');
      result = data;
      if (!document.hidden) { render(data); if (view !== 'live') historicalCards(data); }
      const source = data.source_kind === 'simulated' ? '模拟数据 · 非实机' : 'ROS2来源 · 待实机验收';
      let note = kind === 'joints' ? '仅有效同采样配对参与跟踪 RMS；策略目标不是最终电机目标。' : '时段统计来自全部原始摘要；曲线保留缺口。';
      if (view === 'recording') {
        const missing = metricsFor(kind,joint,thermal).filter(metric=>!record.metrics.includes(metric));
        note = `记录 ${record.run_number} · 保存频率 ${record.frequency_hz} Hz · 曲线为保存样本的分桶均值/极值，卡片为末次分桶均值；缺失与中断不连线。${missing.length ? ' 未勾选保存：'+missing.join('、')+'；不以实时摘要补充。' : ''}${kind === 'joints' ? ' 跟踪 RMS 曲线为已保存的同采样配对 RMS；缺少配对样本权重，整段 RMS 保持未知。' : ''}`;

      }
      text(kind+'-status',`${id} · ${source} · ${new Date(data.start).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})} 至 ${new Date(data.end).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})} · ${view === 'recording' ? '电脑保存时间（北京时间）' : timing?.basis || '中央查询时间'} · ${note}`);
      context(kind);
    } catch(error) {
      if (ticket !== generation || selected !== id || page() !== kind) return;
      clearCurves(); text(kind+'-status',`查询失败：${error.message}。可点击“刷新数据”重试。`);
    } finally { if (ticket === generation) { inflight = null; lastRead = Date.now(); el(kind+'-refresh').disabled = false; } }
  }
  function activate(view) {
    generation++; inflight = null;
    for (const kind of kinds) el(kind+'-refresh').disabled = false;
    if (kinds.includes(view)) {syncMode(view);refresh(true);}
  }
  function selectionChanged() {
    generation++; inflight = null; activeKey = ''; lastRead = 0; clearCurves();
    for (const kind of kinds) { el(kind+'-refresh').disabled = false; if (mode(kind) !== 'live') for (const id of historicalIds[kind]) text(id,'—'); }
  }
  const historicalIds = {battery:['battery-page-soc','battery-page-temp','battery-page-voltage','battery-page-current'],joints:['joints-page-temperature','joints-page-mos','joints-page-torque'],host:['host-page-cpu','host-page-memory','host-page-temperature','host-page-uptime']};
  for (const kind of kinds) {
    setupControls(kind);
    el(kind+'-refresh').addEventListener('click',()=>refresh(true));
    el(kind+'-range').addEventListener('change',()=>{selectionChanged(); refresh(true);});
  }
  el('joints-page-select').addEventListener('change',()=>{selectionChanged(); update(robot,available); refresh(true);});
  el('host-thermal-select').addEventListener('change',()=>{selectionChanged(); refresh(true);});
  document.addEventListener('rp1:recording-select',event=>{ preferredRecord = event.detail?.recordingId || ''; if (records.items.some(record=>record.id === preferredRecord)) {fillRecordChoices();selectionChanged();if (active()) {syncMode(page());update(robot,available);refresh(true);}} });
  document.addEventListener('visibilitychange',()=>{if (!document.hidden) refresh(true);});
  new ResizeObserver(()=>{if (!document.hidden && result && active() && result.component === page()) render(result);}).observe(el('monitor-content'));
  window.rp1Components = {update,changed,activate,refresh:()=>refresh(true)};
  setInterval(()=>{refresh();window.rp1HostCores?.tick();},1000);
})();
