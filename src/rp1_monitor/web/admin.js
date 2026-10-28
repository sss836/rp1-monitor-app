/* Personal administrator workspace. Credentials/session tokens never enter browser storage. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const api = new URL('./api/v1/admin/', window.location.href);
  let csrf = '', robots = [], selected = null, tab = 'profile', generation = 0;
  let history = [], historySpec = null, historyNext = null, auditNext = null, editing = null;
  let historyRequest = 0, auditRequest = 0;
  let configurationRevision = null, configurationDraft = null, configurationRobot = null;
  let configurationOriginalDof = 24, configurationDirty = false, configurationReady = false, configurationConflict = false;
  let configurationRequest = 0, configurationVersionsRequest = 0, configurationNext = null;
  const regions = [
    ['left_shoulder','左肩'],['right_shoulder','右肩'],['left_elbow','左肘'],['right_elbow','右肘'],
    ['left_wrist','左腕'],['right_wrist','右腕'],['left_hip','左髋'],['right_hip','右髋'],
    ['left_knee','左膝'],['right_knee','右膝'],['left_ankle','左踝'],['right_ankle','右踝']
  ];
  const fmt = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:6});
  const date = value => new Date(value).toLocaleString('zh-CN', {hour12:false});
  const source = kind => kind === 'simulated' ? '模拟数据 · 非实机' : 'ROS 2 · 实机验收待确认';
  const action = { 'robot.created':'新建设备', 'robot.updated':'修改档案', 'metric.replace':'更正观测', 'metric.exclude':'排除观测', 'metric.restore':'撤销更正', 'robot.configuration.updated':'修改机型与温度策略', 'robot.configuration.created':'建立机型与温度策略', 'robot.configuration.saved':'保存机型与温度策略', 'configuration.updated':'修改机型与温度策略' };
  function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
  function message(text, success=false) { $('admin-message').textContent=text; $('admin-message').className=`admin-message${success?' success':''}${text?'':' hidden'}`; if(text&&!success) $('admin-message').focus(); }
  function showLogin() { resetConfiguration(); csrf=''; selected=null; robots=[]; generation++; $('admin-workspace').classList.add('hidden'); $('admin-login').classList.remove('hidden'); $('admin-logout').classList.add('hidden'); $('admin-person').textContent='管理员登录'; $('admin-password').value=''; }
  async function request(path, options={}) {
    const activeSession=csrf;
    const headers = {'Content-Type':'application/json'};
    if(options.method && options.method!=='GET') headers['X-CSRF-Token']=csrf;
    const response = await fetch(new URL(path,api), {...options,headers,credentials:'same-origin',cache:'no-store'});
    const body = await response.json();
    if(!['login','session'].includes(path) && activeSession!==csrf) throw new Error('登录会话已改变，请重新加载当前页面');
    if(!response.ok) { if(response.status===401 && path!=='login') showLogin(); const detail=typeof body.detail==='string'?body.detail:'请核对输入内容与必填字段'; const error=new Error(detail); error.status=response.status; throw error; }
    return body;
  }
  async function busy(form, operation) { const button=form.querySelector('button[type=submit]'); if(button.disabled)return; button.disabled=true; try{ await operation(); }catch(error){ const dialog=form.closest('dialog'); if(dialog)dialog.querySelector('.admin-dialog-error').textContent=error.message; else message(error.message); }finally{button.disabled=false;} }
  function reason(id) { const value=$(id).value.trim(); $(id).setCustomValidity(value?'':'请填写修改原因'); if(!value){$(id).reportValidity();throw new Error('请填写修改原因');} return value; }
  function current() { return robots.find(item=>item.robot_id===selected); }
  async function enter(session) { csrf=session.csrf; $('admin-person').textContent=`${session.username} · 管理员`; $('admin-login').classList.add('hidden'); $('admin-logout').classList.remove('hidden'); $('admin-workspace').classList.remove('hidden'); $('admin-password').value=''; message(''); const health=await fetch(new URL('./health',window.location.href),{cache:'no-store'}).then(r=>r.json()); $('new-source').querySelector('[value=ros2]').disabled=!!health.demo; await loadRobots(); }
  async function loadRobots(prefer=selected) { const result=await request('robots'); robots=result.items; $('admin-count').textContent=String(robots.length); $('admin-robots').replaceChildren(); for(const robot of robots){ const button=element('button',undefined,'robot-button'); button.type='button'; button.dataset.robotId=robot.robot_id; button.append(element('div',robot.name,'robot-name'),element('span',source(robot.source_kind),'pill warning'),element('div',`${robot.robot_id} · ${robot.enabled?'已启用':'已停用'}`,'robot-meta')); button.addEventListener('click',()=>selectRobot(robot.robot_id).catch(e=>message(e.message))); $('admin-robots').append(button); } if(!robots.length){selected=null;$('admin-robots').append(element('p','还没有设备，点击“新建”开始登记。','admin-empty')); $('profile-form').classList.add('hidden'); return;} $('profile-form').classList.remove('hidden'); await selectRobot(robots.some(r=>r.robot_id===prefer)?prefer:robots[0].robot_id); }
  async function selectRobot(id) { if(id!==selected&&configurationDirty&&!window.confirm('当前机型与温度策略尚未保存。切换设备会放弃这些编辑，是否继续？'))return; if(id!==selected)resetConfiguration(); selected=id; generation++; const robot=current(); for(const button of $('admin-robots').children)button.classList.toggle('selected',button.dataset.robotId===id); $('admin-robot-id').textContent=robot.robot_id; $('admin-robot-name').textContent=robot.name; $('admin-source').textContent=source(robot.source_kind); $('robot-name').value=robot.name; $('robot-enabled').checked=robot.enabled; $('profile-reason').value=''; loadMetricOptions(id,generation).catch(e=>message(e.message)); if(tab==='corrections')await loadHistory(); if(tab==='audit')await loadAudit(); if(tab==='configuration'&&(!configurationReady||configurationRobot!==id))await loadConfiguration(); }
  async function loadMetricOptions(id,version) { const result=await request(`robots/${encodeURIComponent(id)}/metrics`); if(version!==generation||id!==selected)return; $('history-metrics').replaceChildren(); for(const spec of result.items){const option=element('option'); option.value=spec.key; option.label=`${spec.label} ${spec.unit}`; $('history-metrics').append(option);} }
  async function switchTab(name) { tab=name; for(const button of document.querySelectorAll('[data-tab]')){const chosen=button.dataset.tab===name;button.setAttribute('aria-selected',String(chosen));button.tabIndex=chosen?0:-1;$(`panel-${button.dataset.tab}`).classList.toggle('hidden',!chosen);} if(selected&&tab==='corrections')await loadHistory(); if(selected&&tab==='audit')await loadAudit(); if(selected&&tab==='configuration'&&(!configurationReady||configurationRobot!==selected))await loadConfiguration(); }
  async function loadHistory(more=false) { if(!selected)return; const serial=++historyRequest,version=generation,robot=selected,metric=$('history-metric').value.trim(); const parameters=new URLSearchParams({metric,minutes:$('history-minutes').value});if(more&&historyNext)parameters.set('before_id',historyNext); const result=await request(`robots/${encodeURIComponent(robot)}/windows?${parameters}`); if(serial!==historyRequest||version!==generation||robot!==selected||metric!==$('history-metric').value.trim())return;history=more?[...history,...result.items]:result.items;historySpec=result.spec;historyNext=result.next_before_id;$('history-spec').textContent=`${result.spec.label} · ${result.spec.unit||'无单位'} · ${history.length} 个窗口 · 人工更正独立留存`;$('history-rows').replaceChildren(); for(const row of history){const tr=element('tr');tr.append(element('td',date(row.window_end)),element('td',fmt(row.original?.last)),element('td',row.operation==='exclude'?'已标记无效':row.revision?fmt(row.effective?.last):'沿用原始值'),element('td',row.revision?`#${row.revision}`:'原始'));const td=element('td');const button=element('button','核对 / 更正');button.type='button';button.addEventListener('click',()=>openCorrection(row,metric));td.append(button);tr.append(td);$('history-rows').append(tr);} $('history-empty').classList.toggle('hidden',history.length>0);$('history-empty').textContent='所选范围没有该指标的观测。可调整指标或查询范围。';$('history-more').classList.toggle('hidden',!historyNext); }
  function operationFields() { const replace=$('correction-operation').value==='replace';$('replacement-fields').classList.toggle('hidden',!replace);$('correction-last').required=replace; const range=replace&&historySpec.aggregation==='range';$('replacement-range').classList.toggle('hidden',!range);for(const id of ['correction-min','correction-max'])$(id).required=range; }
  function openCorrection(row,metric) { editing={row,metric,robot:selected};$('correction-context').textContent=`${selected} · ${metric} · ${date(row.window_end)}`;$('correction-original').textContent=`${fmt(row.original?.last)} ${historySpec.unit}`;$('correction-quality').textContent=`样本数 ${row.original.count} · 年龄 ${fmt(row.original.age_ms)} ms · ${row.revision?`当前更正 #${row.revision}`:'尚无人工更正'}`;const replaceOption=$('correction-operation').querySelector('[value=replace]');replaceOption.disabled=row.original.count===0;$('correction-operation').value=row.original.count===0?'exclude':'replace';const value=row.effective||row.original;$('correction-last').value=value.last??'';$('correction-min').value=value.minimum??'';$('correction-max').value=value.maximum??'';$('correction-reason').value='';$('correction-dialog').querySelector('.admin-dialog-error').textContent='';operationFields();$('correction-dialog').showModal(); }
  async function loadAudit(more=false) { if(!selected)return;const serial=++auditRequest,version=generation,robot=selected;const parameters=new URLSearchParams({robot_id:robot});if(more&&auditNext)parameters.set('before_id',auditNext);const result=await request(`audit?${parameters}`);if(serial!==auditRequest||version!==generation||robot!==selected)return;if(!more)$('audit-rows').replaceChildren();auditNext=result.next_before_id;for(const row of result.items){const details=element('details',undefined,'admin-audit');const summary=element('summary');summary.append(element('strong',action[row.action]||row.action),element('span',row.actor_db_login,'muted'),element('time',date(row.created_at)));details.append(summary,element('p',row.reason),element('pre',JSON.stringify({修改前:row.before_value,修改后:row.after_value},null,2)));$('audit-rows').append(details);} $('audit-empty').classList.toggle('hidden',$('audit-rows').children.length>0);$('audit-more').classList.toggle('hidden',!auditNext); }
  function emptyConfiguration(dof) {
    return {schema_version:1,hardware_revision:'unknown',dof,physical_mapping_verified:false,
      scale_min_c:20,scale_max_c:100,
      joints:Array.from({length:dof},(_,index)=>({index,joint_name:null,label:`关节 ${index+1}`,region:null,bus:null,motor_id:null})),
      temperature_limits_c:{}};
  }
  function configurationError(text) {
    $('configuration-error').textContent=text;
    $('configuration-error').classList.toggle('hidden',!text);
    if(text)$('configuration-error').focus();
  }
  function dirtyConfiguration() {
    if(!configurationReady)return;
    configurationDirty=true;
    $('configuration-dirty').textContent='有未保存的修改';
  }
  function resetConfiguration() {
    configurationRequest++; configurationVersionsRequest++;
    configurationRevision=null; configurationDraft=null; configurationRobot=null;
    configurationOriginalDof=24; configurationDirty=false; configurationReady=false; configurationConflict=false;
    configurationNext=null;
    $('configuration-fields').disabled=true;
    $('configuration-save').disabled=false;
    $('configuration-version').textContent='尚未载入';
    $('configuration-source').textContent='';
    $('configuration-dirty').textContent='';
    $('configuration-reason').value='';
    $('configuration-joints').replaceChildren();
    $('configuration-versions').replaceChildren();
    $('configuration-versions-empty').classList.remove('hidden');
    $('configuration-versions-more').classList.add('hidden');
    configurationError('');
  }
  function configurationInput(joint,field,value,type='text') {
    const input=element('input'); input.type=type; input.value=value??''; input.dataset.field=field;
    input.setAttribute('aria-label',`关节 ${joint.index+1} ${field==='label'?'显示名称':field==='joint_name'?'实际关节名称':field==='bus'?'电机总线':field==='motor_id'?'电机编号':field==='motor_limit'?'电机超温限值 °C':'MOS 超温限值 °C'}`);
    if(type==='number'){input.step='any';input.placeholder='未配置';}
    else {input.maxLength=['bus','motor_id'].includes(field)?64:128;input.placeholder=field==='label'?`关节 ${joint.index+1}`:'待确认';}
    if(field==='label')input.required=true;
    return input;
  }
  function renderConfigurationJoints() {
    const dof=Number($('configuration-dof').value);
    const defaults=emptyConfiguration(dof);
    while(configurationDraft.joints.length<dof)configurationDraft.joints.push(defaults.joints[configurationDraft.joints.length]);
    $('configuration-joints').replaceChildren();
    for(const joint of configurationDraft.joints.slice(0,dof)) {
      const row=element('tr'); row.dataset.index=String(joint.index);
      const identity=element('th',undefined); identity.scope='row';identity.append(element('strong',`joint.${joint.index}`),element('small',`关节 ${joint.index+1}`));row.append(identity);
      for(const field of ['label','joint_name']){const cell=element('td');cell.append(configurationInput(joint,field,joint[field]));row.append(cell);}
      const regionCell=element('td'),select=element('select');select.dataset.field='region';select.setAttribute('aria-label',`关节 ${joint.index+1} 图上部位`);
      for(const [value,label] of [['','未确认'],...regions]){const option=element('option',label);option.value=value;select.append(option);}select.value=joint.region||'';regionCell.append(select);row.append(regionCell);
      for(const field of ['bus','motor_id']){const cell=element('td');cell.append(configurationInput(joint,field,joint[field]));row.append(cell);}
      for(const [field,suffix] of [['motor_limit','motor_temperature'],['mos_limit','mos_temperature']]){const cell=element('td');cell.append(configurationInput(joint,field,configurationDraft.temperature_limits_c[`joint.${joint.index}.${suffix}`],'number'));row.append(cell);}
      $('configuration-joints').append(row);
    }
    $('configuration-joint-count').textContent=`${dof} 个关节`;
    const reducing=configurationOriginalDof===28&&dof===24;
    $('configuration-reduction').classList.toggle('hidden',!reducing);
    $('configuration-reduction-confirm').required=reducing;
    if(!reducing)$('configuration-reduction-confirm').checked=false;
  }
  function captureConfigurationRows() {
    for(const row of $('configuration-joints').children){
      const index=Number(row.dataset.index),joint=configurationDraft.joints[index];
      for(const field of ['label','joint_name','region','bus','motor_id']){
        const value=row.querySelector(`[data-field="${field}"]`).value.trim();joint[field]=value||null;
      }
      for(const [field,suffix] of [['motor_limit','motor_temperature'],['mos_limit','mos_temperature']]){
        const input=row.querySelector(`[data-field="${field}"]`),key=`joint.${index}.${suffix}`;
        if(input.value.trim()==='')delete configurationDraft.temperature_limits_c[key];else configurationDraft.temperature_limits_c[key]=input.valueAsNumber;
      }
    }
  }
  function renderConfiguration(envelope) {
    configurationDraft=structuredClone(envelope.configuration||emptyConfiguration(24));
    configurationRevision=envelope.revision; configurationRobot=envelope.robot_id;
    configurationOriginalDof=configurationDraft.dof;configurationDirty=false;configurationReady=true;configurationConflict=false;
    $('configuration-version').textContent=envelope.revision==null?'尚无中央版本':`当前版本 #${envelope.revision}`;
    $('configuration-source').textContent=({database:'来源：中央数据库',local_file:'来源：本机配置文件 · 保存后建立中央版本',unconfigured:'机型尚未确认 · 下方为 24 自由度待填写模板'})[envelope.source]||'来源待确认';
    $('configuration-dof').value=String(configurationDraft.dof);
    $('configuration-hardware').value=configurationDraft.hardware_revision;
    $('configuration-scale-min').value=configurationDraft.scale_min_c;
    $('configuration-scale-max').value=configurationDraft.scale_max_c;
    $('configuration-mapping-verified').checked=configurationDraft.physical_mapping_verified;
    $('configuration-reason').value='';$('configuration-dirty').textContent='';
    $('configuration-reduction-confirm').checked=false;
    $('configuration-fields').disabled=false;$('configuration-save').disabled=false;
    configurationError('');renderConfigurationJoints();
  }
  async function loadConfiguration() {
    if(!selected)return;
    const serial=++configurationRequest,version=generation,robot=selected;
    configurationReady=false;$('configuration-fields').disabled=true;
    $('configuration-version').textContent='正在载入…';configurationError('');
    try {
      const result=await request(`robots/${encodeURIComponent(robot)}/configuration`);
      if(serial!==configurationRequest||version!==generation||robot!==selected)return;
      renderConfiguration(result);await loadConfigurationVersions().catch(error=>configurationError(error.message));
    }catch(error){if(serial===configurationRequest&&version===generation&&robot===selected){$('configuration-version').textContent='载入失败';configurationError(error.message);} }
  }
  async function loadConfigurationVersions(more=false) {
    if(!selected)return;
    const serial=++configurationVersionsRequest,version=generation,robot=selected;
    const parameters=new URLSearchParams({limit:'20'});if(more&&configurationNext)parameters.set('before_id',configurationNext);
    const result=await request(`robots/${encodeURIComponent(robot)}/configuration/versions?${parameters}`);
    if(serial!==configurationVersionsRequest||version!==generation||robot!==selected)return;
    if(!more)$('configuration-versions').replaceChildren();
    configurationNext=result.next_before_id;
    for(const row of result.items){
      const details=element('details',undefined,'admin-audit'),summary=element('summary');
      summary.append(element('strong',`版本 #${row.id} · ${row.configuration.dof} 自由度`),element('span',row.actor_db_login,'muted'),element('time',date(row.created_at)));
      details.append(summary,element('p',row.reason),element('pre',JSON.stringify(row.configuration,null,2)));$('configuration-versions').append(details);
    }
    $('configuration-versions-empty').classList.toggle('hidden',$('configuration-versions').children.length>0);
    $('configuration-versions-more').classList.toggle('hidden',!configurationNext);
  }
  function validateConfiguration() {
    captureConfigurationRows();
    const dof=Number($('configuration-dof').value),minimum=$('configuration-scale-min').valueAsNumber,maximum=$('configuration-scale-max').valueAsNumber;
    if(!Number.isFinite(minimum)||!Number.isFinite(maximum)||minimum>=maximum)throw new Error('色阶起点必须小于终点，且均为有效数值');
    const configuration={...configurationDraft,dof,hardware_revision:$('configuration-hardware').value.trim()||'unknown',physical_mapping_verified:$('configuration-mapping-verified').checked,scale_min_c:minimum,scale_max_c:maximum,joints:configurationDraft.joints.slice(0,dof),temperature_limits_c:{}};
    const names=new Set(),motors=new Set();
    for(const joint of configuration.joints){
      if(!joint.label)throw new Error(`请填写关节 ${joint.index+1} 的显示名称`);
      if(joint.joint_name){if(names.has(joint.joint_name))throw new Error(`实际关节名称重复：${joint.joint_name}`);names.add(joint.joint_name);}
      if(Boolean(joint.bus)!==Boolean(joint.motor_id))throw new Error(`关节 ${joint.index+1} 的电机总线与编号需要一起填写`);
      if(joint.bus){const identity=JSON.stringify([joint.bus,joint.motor_id]);if(motors.has(identity))throw new Error(`关节 ${joint.index+1} 的电机总线与编号重复`);motors.add(identity);}
      if(configuration.physical_mapping_verified&&current()?.source_kind==='ros2'&&!joint.joint_name)throw new Error(`确认 ROS 2 物理映射前，请填写关节 ${joint.index+1} 的实际关节名称`);
      for(const suffix of ['motor_temperature','mos_temperature']){const key=`joint.${joint.index}.${suffix}`;if(Object.hasOwn(configurationDraft.temperature_limits_c,key)){const value=configurationDraft.temperature_limits_c[key];if(!Number.isFinite(value))throw new Error(`关节 ${joint.index+1} 的温度限值必须为有效数值`);configuration.temperature_limits_c[key]=value;}}
    }
    return configuration;
  }
  $('configuration-form').addEventListener('input',event=>{event.target.setCustomValidity?.('');dirtyConfiguration();});
  $('configuration-form').addEventListener('change',dirtyConfiguration);
  $('configuration-dof').addEventListener('change',()=>{if(!configurationReady)return;captureConfigurationRows();$('configuration-mapping-verified').checked=false;renderConfigurationJoints();dirtyConfiguration();});
  $('configuration-form').addEventListener('submit',event=>{
    event.preventDefault();busy(event.currentTarget,async()=>{
      if(!configurationReady||configurationRobot!==selected)throw new Error('请先载入当前机器人的配置');
      if(configurationConflict)throw new Error('中央版本已改变。请先核对并重新载入，再保存新的修改');
      const robot=selected,version=generation,configuration=validateConfiguration(),saveReason=reason('configuration-reason');
      configurationError('');
      try {
        const result=await request(`robots/${encodeURIComponent(robot)}/configuration`,{method:'POST',body:JSON.stringify({expected_revision:configurationRevision,configuration,reason:saveReason})});
        if(robot!==selected||version!==generation)return;
        renderConfiguration(result);message('机型与温度策略已保存为新版本，修改原因与操作者已留存。',true);await loadConfigurationVersions();
      }catch(error){
        if(robot===selected&&version===generation){
          if(error.status===409){configurationConflict=true;$('configuration-dirty').textContent='版本冲突 · 编辑内容仍保留';}
          configurationError(error.status===409?'中央配置已由其他管理员更新。当前编辑仍保留，请核对后点击“重新载入”；本次未覆盖任何版本。':error.message);
        }
      }
    });
  });
  $('configuration-reload').addEventListener('click',()=>{if(configurationDirty&&!window.confirm('重新载入会放弃当前未保存的机型与温度策略编辑。是否继续？'))return;loadConfiguration().catch(e=>configurationError(e.message));});
  $('configuration-versions-refresh').addEventListener('click',()=>loadConfigurationVersions().catch(e=>configurationError(e.message)));
  $('configuration-versions-more').addEventListener('click',()=>loadConfigurationVersions(true).catch(e=>configurationError(e.message)));
  $('login-form').addEventListener('submit',event=>{event.preventDefault();busy(event.currentTarget,async()=>{const session=await request('login',{method:'POST',body:JSON.stringify({username:$('admin-username').value.trim(),password:$('admin-password').value})});await enter(session);});});
  $('admin-logout').addEventListener('click',async()=>{try{await request('logout',{method:'POST',body:'{}'});}catch(error){message(error.message);}finally{showLogin();}});
  for(const button of document.querySelectorAll('[data-tab]')) {button.addEventListener('click',()=>switchTab(button.dataset.tab).catch(e=>message(e.message)));button.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const tabs=[...document.querySelectorAll('[data-tab]')];let index=tabs.indexOf(button);index=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[index].click();tabs[index].focus();});}
  for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>$(button.dataset.close).close());
  $('new-robot').addEventListener('click',()=>{$('new-form').reset();$('new-dialog').querySelector('.admin-dialog-error').textContent='';$('new-dialog').showModal();});
  $('new-form').addEventListener('submit',event=>{event.preventDefault();busy(event.currentTarget,async()=>{const result=await request('robots',{method:'POST',body:JSON.stringify({robot_id:$('new-id').value.trim(),name:$('new-name').value.trim(),source_kind:$('new-source').value,configuration:emptyConfiguration(Number($('new-dof').value)),reason:reason('new-reason')})});$('new-dialog').close();message(`已登记 ${result.robot_id}。上传凭据已保存到私有部署目录，引用：${result.credential_reference}`,true);await loadRobots(result.robot_id);});});
  $('profile-form').addEventListener('submit',event=>{event.preventDefault();busy(event.currentTarget,async()=>{const robot={...current()};await request(`robots/${encodeURIComponent(robot.robot_id)}`,{method:'PATCH',body:JSON.stringify({expected_name:robot.name,expected_enabled:robot.enabled,name:$('robot-name').value.trim(),enabled:$('robot-enabled').checked,reason:reason('profile-reason')})});message('设备档案已保存，修改已写入操作记录。',true);await loadRobots(robot.robot_id);});});
  $('history-filter').addEventListener('submit',event=>{event.preventDefault();busy(event.currentTarget,()=>loadHistory());});
  $('history-more').addEventListener('click',()=>loadHistory(true).catch(e=>message(e.message)));
  $('audit-more').addEventListener('click',()=>loadAudit(true).catch(e=>message(e.message)));
  $('audit-refresh').addEventListener('click',()=>loadAudit().catch(e=>message(e.message)));
  $('correction-operation').addEventListener('change',operationFields);
  $('correction-form').addEventListener('submit',event=>{event.preventDefault();busy(event.currentTarget,async()=>{const {row,metric,robot}=editing;const operation=$('correction-operation').value;let replacement=null;if(operation==='replace'){replacement={...row.original,last:$('correction-last').valueAsNumber};if(historySpec.aggregation==='range'){replacement.minimum=$('correction-min').valueAsNumber;replacement.maximum=$('correction-max').valueAsNumber;}if(!Number.isFinite(replacement.last)||replacement.minimum!=null&&!Number.isFinite(replacement.minimum)||replacement.maximum!=null&&!Number.isFinite(replacement.maximum))throw new Error('请输入有效的有限数值');if(replacement.minimum!=null&&!(replacement.minimum<=replacement.last&&replacement.last<=replacement.maximum))throw new Error('最后值必须位于最小值与最大值之间');}await request(`windows/${row.window_id}/corrections`,{method:'POST',body:JSON.stringify({metric,expected_revision:row.revision,operation,replacement,reason:reason('correction-reason')})});$('correction-dialog').close();message('更正记录已保存，原始观测保持不变。',true);if(robot===selected)await loadHistory();});});
  for(const input of document.querySelectorAll('textarea'))input.addEventListener('input',()=>input.setCustomValidity(''));
  request('session').then(enter).catch(error=>{showLogin();if(error.status!==401)message(error.message);});
})();
