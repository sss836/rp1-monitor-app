/* Read-only temperature drill-down. Opening or preparing a question never calls a model. */
'use strict';
(() => {
  const el=byId,dialog=el('thermal-dialog');
  let robot=null,joint=null,serial=0,result=null;
  const states={hot:'超温',below:'未超过配置限值',unknown:'数据缺失 / 已过期',unconfigured:'温度限值未配置'};
  const alertStates={normal:'本次评估正常',pending:'等待持续条件确认',active:'告警中',unknown:'观测未知'};
  const events={opened:'触发（持续条件已确认）',recovered:'恢复（规则确认正常）',observation_lost:'观测中断',observation_restored:'恢复观测'};
  const number=value=>Number.isFinite(value)?`${value.toFixed(1)} °C`:'—';
  const stamp=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'未记录';
  function node(tag,text,css){const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(css)item.className=css;return item;}
  function roster(){return robots.find(item=>item.robot_id===selected)?.health?.motor_map?.rows||[];}
  function resetDetails(status='等待查询温度证据'){
    result=null;el('thermal-readings').replaceChildren();el('thermal-alert-heads').replaceChildren();el('thermal-events').replaceChildren();
    for(const kind of ['motor','mos']){el(`thermal-${kind}-chart`).replaceChildren();el(`thermal-${kind}-empty`).classList.remove('hidden');el(`thermal-${kind}-resolution`).textContent='等待查询';}
    el('thermal-state').textContent='未知';el('thermal-state').className='pill neutral';
    el('thermal-current-detail').textContent='没有观测不能判定为正常。';
    el('thermal-alert-meaning').textContent='';el('thermal-query-status').textContent=status;
    el('thermal-error').textContent='';el('thermal-error').classList.add('hidden');el('thermal-diagnose').disabled=true;
  }
  function renderCurves(){
    if(!result)return;
    for(const [kind,suffix] of [['motor','motor_temperature'],['mos','mos_temperature']]){
      const metric=`joint.${joint}.${suffix}`,curve=(result.curves||[]).find(item=>item.metric===metric);
      el(`thermal-${kind}-resolution`).textContent=curve?.points?.length?'':'尚无该指标的历史摘要';
      drawCurve(curve||{points:[]},`thermal-${kind}-chart`,`thermal-${kind}-empty`,`thermal-${kind}-resolution`);
    }
  }
  function renderAlerts(data){
    const heads=el('thermal-alert-heads');heads.replaceChildren();
    for(const item of data.heads||[]){
      const card=node('article',undefined,'thermal-alert-card');
      const label=item.rule_key?.endsWith('.mos_temperature')?'MOS 温度规则':'电机温度规则';
      const state=!item.evaluation_fresh?'评估已过期 · 当前未知':alertStates[item.state]||'当前未知';
      const heading=node('div',undefined,'thermal-alert-card-heading');heading.append(node('strong',label),node('span',state,!item.evaluation_fresh||item.state!=='normal'?'warning':'good'));card.append(heading);
      const list=node('dl');
      for(const [title,value] of [['本轮条件起点',stamp(item.condition_since)],['本次告警确认触发',stamp(item.opened_at)],['最近评估',stamp(item.evaluated_at)],['未恢复事件',item.is_open?'有 · 以恢复事件为准':'无']]){list.append(node('dt',title),node('dd',value));}
      card.append(list,node('p',item.evidence?.detail||'该规则暂无解释证据。','thermal-note'));heads.append(card);
    }
    if(!data.heads?.length)heads.append(node('p','尚无该关节的持久温度规则评估，不能据此判定正常。','thermal-note'));
    const timeline=el('thermal-events');timeline.replaceChildren();
    for(const item of data.events||[]){
      const entry=node('article',undefined,'thermal-event');
      entry.append(node('time',stamp(item.occurred_at)),node('strong',events[item.event_kind]||item.event_kind),node('span',item.rule_key||''));
      if(item.evidence?.detail)entry.append(node('p',item.evidence.detail));timeline.append(entry);
    }
    if(!data.events?.length)timeline.append(node('p','当前没有匹配的状态变化记录；没有记录不等于设备健康验收通过。','thermal-note'));
    el('thermal-alert-meaning').textContent=data.meaning||'';
  }
  function render(data){
    result=data;
    const current=data.current||{},source=data.source_kind==='simulated'?'模拟数据 · 非实机':'ROS 2 来源 · 实机验收待确认';
    el('thermal-source').textContent=source;el('thermal-title').textContent=`${current.label||`关节 ${joint+1}`} · 温度详情`;
    el('thermal-revision').textContent=data.configuration_revision==null?'中央配置版本：尚未建立':`中央配置版本 #${data.configuration_revision}`;
    el('thermal-state').textContent=current.status||states[current.state]||'当前未知';
    el('thermal-state').className=`pill ${current.state==='hot'?'bad':current.state==='below'?'neutral':'warning'}`;
    el('thermal-current-detail').textContent=current.detail||'当前没有完整温度观测，状态未知。';
    const readings=el('thermal-readings');readings.replaceChildren();
    for(const reading of current.readings||[]){
      const card=node('article',undefined,'thermal-reading');card.append(node('span',`${reading.label}温度`,'label'),node('strong',number(reading.value)));
      card.append(node('span',reading.limit_c==null?'超温限值：未配置':`超温限值：${number(reading.limit_c)}`));
      const status=reading.limit_c==null?'未配置判据':!Number.isFinite(reading.value)?'未知 · 无有效观测':reading.value>reading.limit_c?'超温':'未超过配置限值';
      card.append(node('small',status,reading.limit_c!=null&&Number.isFinite(reading.value)&&reading.value>reading.limit_c?'bad':''));readings.append(card);
    }
    if(!current.readings?.length)readings.append(node('p','当前温度读数未知。','thermal-note'));
    el('thermal-query-status').textContent=`查询快照：${stamp(data.as_of_at)} · 历史 ${stamp(data.start)} 至 ${stamp(data.end)}`;
    el('thermal-meaning').textContent=data.meaning||'每秒摘要并非完整高频曲线；缺失数据保留断点，不填零。模拟结果不代表实机验收。';
    renderCurves();renderAlerts(data.alerts||{});el('thermal-diagnose').disabled=false;
  }
  async function refresh(){
    if(!dialog.open||robot!==selected||joint==null)return;
    const version=++serial,id=robot,index=joint,minutes=el('thermal-minutes').value;
    resetDetails('正在查询所选关节的温度与告警证据…');el('thermal-refresh').disabled=true;
    try{
      const data=await api(`api/v1/robot-monitors/${encodeURIComponent(id)}/joints/${index}/temperature?minutes=${encodeURIComponent(minutes)}`);
      if(version!==serial||id!==selected||id!==robot||index!==joint||!dialog.open)return;
      if(data.robot_id!==id||data.joint!==index)throw new Error('返回证据与当前所选设备或关节不一致，请重新查询');
      render(data);
    }catch(error){
      if(version!==serial||id!==selected||id!==robot||index!==joint||!dialog.open)return;
      resetDetails('查询失败 · 当前温度和告警状态未知');el('thermal-error').textContent=error.message;el('thermal-error').classList.remove('hidden');
    }finally{if(version===serial)el('thermal-refresh').disabled=false;}
  }
  function openJoint(index){
    if(!selected||!Number.isInteger(index)||index<0)return;
    const rows=roster(),current=rows.find(item=>item.joint===index);if(!current)return;
    robot=selected;joint=index;serial++;el('thermal-joint').replaceChildren();
    for(const row of rows){const option=node('option',`${row.label} · joint.${row.joint}${row.region?'':' · 部位待确认'}`);option.value=String(row.joint);el('thermal-joint').append(option);}
    el('thermal-joint').value=String(index);el('thermal-robot').textContent=`${robots.find(item=>item.robot_id===robot)?.name||robot} · ${robot}`;
    el('thermal-source').textContent=robots.find(item=>item.robot_id===robot)?.source_kind==='simulated'?'模拟数据 · 非实机':'ROS 2 来源 · 实机验收待确认';
    el('thermal-title').textContent=`${current.label} · 温度详情`;el('thermal-revision').textContent='配置版本待查询';
    resetDetails();if(!dialog.open)dialog.showModal();refresh();
  }
  function openRegion(region){
    const candidates=roster().filter(row=>row.region===region);
    const heat=row=>{const values=[row.value,...(row.readings||[]).map(reading=>reading.value)].filter(Number.isFinite);return values.length?Math.max(...values):-Infinity;};
    candidates.sort((a,b)=>heat(b)-heat(a)||a.joint-b.joint);if(candidates.length)openJoint(candidates[0].joint);
  }
  function changed(){serial++;robot=null;joint=null;resetDetails('设备已切换，请重新选择关节');el('thermal-refresh').disabled=false;if(dialog.open)dialog.close();}
  el('thermal-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{serial++;result=null;el('thermal-refresh').disabled=false;});
  el('thermal-joint').addEventListener('change',()=>{joint=Number(el('thermal-joint').value);const current=roster().find(row=>row.joint===joint);el('thermal-title').textContent=`${current?.label||`关节 ${joint+1}`} · 温度详情`;refresh();});
  el('thermal-minutes').addEventListener('change',refresh);
  el('thermal-refresh').addEventListener('click',refresh);
  el('thermal-diagnose').addEventListener('click',()=>{if(!result||robot!==selected||joint==null)return;const index=joint,minutes=Number(el('thermal-minutes').value);dialog.close();window.rp1Operations?.temperatureContext(index,minutes);});
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{if(dialog.open)renderCurves();}).observe(el('thermal-charts')||el('thermal-motor-chart'));
  window.rp1Thermal={openJoint,openRegion,changed};
})();
