/* Read-only conversation. Text and evidence always render through textContent. */
(() => {
  const el = id => document.getElementById(id);
  const dialog = el('ops-dialog'), transcript = el('ops-transcript'), welcome = el('ops-welcome');
  let revision = 0, busy = false, history = [], modelCatalog = [], temperatureMetrics = null;
  const stateLabels = {observed:'已观测',missing:'缺失/过期',unsupported:'未提供',error:'来源错误',verified:'已核对',unverified:'待核对',stale:'已过期'};
  const clock = () => new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  function reset() {
    temperatureMetrics = null;
    revision++; history = [];
    transcript.querySelectorAll('.chat-message').forEach(item => item.remove());
    welcome.hidden = false;
    el('ops-question').value = '';
    el('ops-message').textContent = '新的对话 · 仅保留本窗口内当前机器人的上下文';
  }
  function contextPlaceholder() {
    el('ops-robot').textContent = selected || '等待选择机器人';
    const robot = robots.find(item => item.robot_id === selected);
    el('ops-header-context').textContent = `${selected || '等待选择机器人'} · ${robot?.source_kind === 'simulated' ? '模拟数据' : 'ROS 2 观测 · 待验收'}`;
    el('ops-source').textContent = robot?.source_kind === 'simulated' ? '模拟数据 · 软件验证' : '来源待核对 · 未完成实机验收';
    el('ops-profile').textContent = '等待读取设备档案';
    el('ops-capabilities').replaceChildren(); el('ops-reports').replaceChildren();
    el('ops-board-status').textContent = '等待读取历史检查报告';
    el('ops-board-results').replaceChildren(); el('ops-board-detail').textContent = '暂无报告';
    el('ops-metric').replaceChildren(new Option('仅查询当前状态',''));
    for (const item of robot?.catalog || []) el('ops-metric').add(new Option(`${item.label} / ${item.unit}`, item.key));
  }
  function openInline() {
    const metric = el('ops-metric').value;
    contextPlaceholder();
    if ([...el('ops-metric').options].some(option => option.value === metric)) el('ops-metric').value = metric;
    else temperatureMetrics = null;
    if (window.matchMedia('(max-width:760px)').matches) document.querySelector('.chat-context').open = false;
    if (!dialog.open) dialog.show();
    perform('refresh');
  }
  window.rp1Operations = {openInline, temperatureContext(joint, minutes) {
    if (!selected || !Number.isInteger(joint) || joint < 0 || joint > 63) return;
    window.rp1Pages?.navigate('diagnostics', selected);
    reset(); contextPlaceholder();
    temperatureMetrics = [`joint.${joint}.motor_temperature`, `joint.${joint}.mos_temperature`];
    if (![...el('ops-metric').options].some(option => option.value === temperatureMetrics[0])) el('ops-metric').add(new Option(`关节 ${joint} · 电机温度`, temperatureMetrics[0]));
    el('ops-metric').value = temperatureMetrics[0];
    el('ops-minutes').value = String([10,60,1440].includes(minutes) ? minutes : 10);
    el('ops-question').value = `请分析关节 ${joint} 的电机和MOS温度、温升摘要以及超温触发/恢复记录。按当前设备配置核对限值，区分已观测事实与未知，不推断没有证据的发热根因。`;
    el('ops-message').textContent = `已带入关节 ${joint} 的电机及MOS两项证据；发送后查询，尚未调用模型。`;
    if (!dialog.open) { if (window.rp1Pages) dialog.show(); else dialog.showModal(); }
    el('ops-question').focus();
  }, changed() {
    reset(); contextPlaceholder();
    if (dialog.open && !busy) perform('refresh');
  }};
  async function request(path, body) {
    const response = await fetch(endpoint(path), {method:body === undefined ? 'GET':'POST',
      headers:{...(token ? {Authorization:`Bearer ${token}`} : {}), ...(body === undefined ? {} : {'Content-Type':'application/json'})},
      body:body === undefined ? undefined:JSON.stringify(body), credentials:'same-origin', cache:'no-store', signal:AbortSignal.timeout(100000)});
    if (!response.ok) {
      const error = await response.json().catch(()=>({}));
      throw new Error(typeof error.detail==='string' && error.detail.length<300 ? error.detail : `查询失败 (${response.status})，请核对只读凭据、服务和模型配置。`);
    }
    return response;
  }
  const base = id => `./api/v1/robot-monitors/${encodeURIComponent(id)}`;
  function query() {
    const end = new Date(), start = new Date(end.getTime()-Number(el('ops-minutes').value)*60000);
    return {start:start.toISOString(),end:end.toISOString(),metrics:temperatureMetrics || (el('ops-metric').value ? [el('ops-metric').value] : [])};
  }
  function message(role, text, meta, evidence) {
    welcome.hidden = true;
    const item = document.createElement('article'); item.className = `chat-message ${role}`;
    const label = document.createElement('p'); label.className = 'message-meta'; label.textContent = meta;
    const body = document.createElement('p'); body.className = 'message-body'; body.textContent = text;
    item.append(label, body);
    if (evidence !== undefined) {
      const details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
      summary.textContent = '查看查询证据'; pre.textContent = JSON.stringify(evidence, null, 2);
      details.append(summary, pre); item.append(details);
    }
    transcript.append(item);
    const messages = transcript.querySelectorAll('.chat-message');
    if (messages.length > 40) messages[0].remove();
    transcript.scrollTop = transcript.scrollHeight;
    return item;
  }
  async function refresh(id, captured) {
    const [capabilities, doctor, reports] = await Promise.all([
      request(base(id)+'/capabilities').then(r=>r.json()), request(base(id)+'/doctor').then(r=>r.json()), request(base(id)+'/diagnostics').then(r=>r.json())]);
    if (captured !== revision || selected !== id) return null;
    const profile = capabilities.profile, revisionLabel = value => !value || value==='unknown' ? '未登记' : value;
    const configuration = capabilities.monitoring_configuration;
    const registered = configuration?.source === 'database' ? `中央配置 #${configuration.revision} · ${configuration.configuration.dof} 自由度 · 硬件 ${revisionLabel(configuration.configuration.hardware_revision)}\n` : '';
    el('ops-profile').textContent = `${registered}采集档案硬件 ${revisionLabel(profile.hardware_revision)}\n运控 ${revisionLabel(profile.inference_revision)} · 驱动 ${revisionLabel(profile.driver_revision)}`;
    el('ops-capabilities').replaceChildren();
    for (const item of capabilities.items) {
      const row = document.createElement('div'), title = document.createElement('span'), state = document.createElement('span');
      title.textContent = item.label; state.textContent = stateLabels[item.state] || item.state;
      state.className = item.ready ? 'ready' : 'attention'; row.title = item.reason; row.append(title, state); el('ops-capabilities').append(row);
    }
    el('ops-reports').replaceChildren();
    for (const item of reports.items) {
      const button = document.createElement('button'); button.type = 'button';
      button.textContent = `下载 ${item.created_at} 的诊断包`;
      button.addEventListener('click',()=>download(id,item.report_id)); el('ops-reports').append(button);
    }
    if (!reports.items.length) el('ops-reports').textContent = '暂无归档记录';
    await refreshBoard(id,captured);
    return doctor;
  }
  async function refreshBoard(id,captured) {
    const labels={pass:'符合本项判据',fail:'未符合判据',needs_review:'待人工复核',skip:'已跳过',error:'检查错误',incomplete:'检查未完整完成'};
    try {
      const result=await request(base(id)+'/board-diagnostics').then(r=>r.json());
      if(captured!==revision || selected!==id) return;
      el('ops-board-results').replaceChildren();
      const report=result.latest;
      if(!report) {
        el('ops-board-status').textContent='暂无板端报告。请在本机上位机发起检查，等待后台上传。';
        el('ops-board-detail').textContent='尚未收到报告，不能视为检查通过。'; return;
      }
      const raw=report.board_diagnostics;
      el('ops-board-status').textContent=`${report.source_kind==='simulated'?'模拟数据 · 软件验证':'历史板端检查 · 非实机验收'}\n${report.created_at}\nTUI ${raw.tui_version} · ${labels[raw.outcome] || raw.outcome}`;
      for(const item of raw.results) {
        const row=document.createElement('div'),name=document.createElement('span'),state=document.createElement('span');
        name.textContent=`${item.id} · ${item.plugin.documentation?.title || item.name || ''}`;
        state.textContent=labels[item.result] || item.result; state.className=item.result==='pass'?'ready':'attention';
        row.title=item.message; row.append(name,state); el('ops-board-results').append(row);
      }
      el('ops-board-detail').textContent=JSON.stringify(report,null,2);
    } catch(error) {
      if(captured===revision && selected===id) el('ops-board-status').textContent=`板端报告读取失败，可刷新重试：${error.message}`;
    }
  }
  async function perform(action) {
    if (busy) return;
    const id = selected, captured = revision, question = el('ops-question').value.trim();
    if (!id) { el('ops-message').textContent = '请先选择机器人'; return; }
    if (action === 'ask' && !question) { el('ops-question').focus(); return; }
    const useModel = el('ops-use-model').checked;
    let payload;
    try { payload = action === 'ask' ? {...query(), question, history:history.slice(-8), use_model:useModel, connection:useModel ? modelConnection() : null} : query(); }
    catch(error) { el('ops-message').textContent=error.message; return; }
    let pending;
    busy = true;
    document.querySelectorAll('[data-ops]').forEach(button => button.disabled = true);
    if (action !== 'refresh' && action !== 'board') {
      message('user', action==='ask' ? question : action==='doctor' ? '帮我做一次接入自检。' : '记录并导出当前诊断证据。', `你 · ${clock()}`);
      pending = message('pending', '正在读取观测与查询证据…', 'RP1 诊断助手 · 查询中');
      if (action === 'ask') el('ops-question').value = '';
    }
    el('ops-message').textContent = '正在查询 · 后台采集与上传继续运行';
    try {
      if(action==='board') {
        await refreshBoard(id,captured);
      } else if (action === 'refresh' || action === 'doctor') {
        const doctor = await refresh(id, captured);
        if (!doctor) return;
        if (action === 'doctor') {
          const attention = doctor.checks.filter(c=>c.result !== 'pass');
          let answer = `已完成 ${doctor.checks.length} 项接入检查，其中 ${attention.length} 项需要核对。\n\n`;
          answer += attention.length ? attention.slice(0,4).map((c,i)=>`${i+1}. ${c.detail}\n建议：${c.next_step}`).join('\n\n') : '当前检查项均有有效观测。';
          if (attention.length > 4) answer += `\n\n其余 ${attention.length-4} 项可在下方查询证据中查看。`;
          answer += '\n\n自检反映本次接入情况，不能代替整机健康验收。';
          pending.remove(); message('assistant',answer,`RP1 · 接入自检 · 只读 · ${clock()}`,doctor);
        }
      } else if (action === 'record') {
        const saved = await request(base(id)+'/diagnostics',payload).then(r=>r.json());
        if (captured !== revision || selected !== id) return;
        await download(id,saved.report_id);
        if (captured !== revision || selected !== id) return;
        pending.remove(); message('assistant','诊断记录已保存，证据包已开始下载。\n包含来源、时间和 SHA256 校验信息。',`RP1 · 证据归档 · ${clock()}`,saved);
        await refresh(id,captured);
      } else {
        const result = await request(base(id)+'/assistant',payload).then(r=>r.json());
        if (captured !== revision || selected !== id) return;
        const mode = result.mode==='ai' ? `${result.provider || '模型'} / ${result.model || ''} · 需复核` : useModel ? '模型未启用 · 已返回本地规则' : '本地规则 · 未调用模型';
        pending.remove(); message('assistant',result.answer,`RP1 · ${mode} · ${clock()}`,result);
        history.push({role:'user',content:question},{role:'assistant',content:result.answer.slice(0,4000)}); history = history.slice(-8);
      }
      if (captured === revision && selected === id) el('ops-message').textContent = '本次查询快照 · 来源与时间见每条回答的查询证据';
    } catch (error) {
      if (captured === revision && selected === id) {
        if (action !== 'refresh') message('assistant',error.message,`RP1 · 请求未完成 · ${clock()}`);
        el('ops-message').textContent = error.message;
        if (action==='ask' && !el('ops-question').value) el('ops-question').value = question;
      }
    } finally {
      pending?.remove(); busy = false;
      document.querySelectorAll('[data-ops]').forEach(button => button.disabled = false);
      if (captured !== revision && dialog.open) perform('refresh');
    }
  }
  async function download(id, reportId) {
    const response = await request(base(id)+`/diagnostics/${encodeURIComponent(reportId)}/bundle`);
    const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = `rp1-diagnostics-${reportId}.zip`; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function modelConnection() {
    const key = el('ops-api-key').value.trim();
    if (key && location.protocol !== 'https:' && !['localhost','127.0.0.1','[::1]'].includes(location.hostname)) throw new Error('传输 API Key 须使用 HTTPS；本机回环预览可用 HTTP。');
    return {provider:el('ops-provider').value || 'server',model:el('ops-model').value.trim(),api_key:key};
  }
  function changeProvider(keep=false) {
    const item=modelCatalog.find(item=>item.id===el('ops-provider').value);
    if (!keep) { el('ops-api-key').value=''; el('ops-model').value=item?.models[0]?.id || ''; }
    el('ops-model-options').replaceChildren();
    for (const model of item?.models || []) el('ops-model-options').append(new Option(model.name || model.id,model.id));
    el('ops-model-endpoint').textContent=item?.endpoint || '地址待确认';
    el('ops-model-status').textContent='请使用所选厂商及地域对应的 API Key。';
  }
  async function modelAction(action) {
    el('ops-model-test').disabled=true; el('ops-model-refresh').disabled=true;
    el('ops-model-status').textContent=action==='refresh' ? '正在读取服务端配置…' : '正在测试接口与工具调用…';
    try {
      if (action==='refresh') {
        const catalog=await request('./api/v1/assistant/models').then(r=>r.json());
        const chosen=el('ops-provider').value;
        modelCatalog=catalog.items; el('ops-provider').replaceChildren();
        for(const item of modelCatalog) el('ops-provider').add(new Option(item.name,item.id));
        if (modelCatalog.some(item=>item.id===chosen)) el('ops-provider').value=chosen;
        changeProvider(true);
        if (!el('ops-model').value) el('ops-model').value=modelCatalog.find(item=>item.id===el('ops-provider').value)?.models[0]?.id || '';
        el('ops-model-status').textContent='服务配置已刷新。连接测试只验证工具调用，不发送机器人数据。';
      } else {
        const result=await request('./api/v1/assistant/test',modelConnection()).then(r=>r.json());
        el('ops-model-status').textContent=`连接成功 · ${result.provider} / ${result.model} · 工具调用已通过；未发送机器人数据`;
      }
    } catch(error) { el('ops-model-status').textContent=error.message; }
    finally { el('ops-model-test').disabled=false; el('ops-model-refresh').disabled=false; }
  }
  el('ops-model-open').addEventListener('click',()=>{el('ops-model-dialog').showModal();modelAction('refresh');});
  el('ops-model-close').addEventListener('click',()=>el('ops-model-dialog').close());
  el('ops-model-done').addEventListener('click',()=>el('ops-model-dialog').close());
  el('ops-key-clear').addEventListener('click',()=>{el('ops-api-key').value='';});
  el('ops-provider').addEventListener('change',()=>changeProvider());
  el('ops-model-refresh').addEventListener('click',()=>modelAction('refresh'));
  el('ops-model-test').addEventListener('click',()=>modelAction('test'));
  el('ops-open').addEventListener('click',()=>{
    if (window.rp1Pages) { window.rp1Pages.navigate('diagnostics', selected); return; }
    temperatureMetrics = null;
    contextPlaceholder();
    if (window.matchMedia('(max-width:760px)').matches) document.querySelector('.chat-context').open = false;
    dialog.showModal(); perform('refresh'); el('ops-question').focus();
  });
  el('ops-close').addEventListener('click',()=>{if (window.rp1Pages) window.rp1Pages.navigate('overview', selected); else dialog.close();});
  el('ops-metric').addEventListener('change',()=>{temperatureMetrics=null;});
  el('ops-new').addEventListener('click',()=>{reset(); el('ops-question').focus();});
  el('ops-compose').addEventListener('submit',event=>{event.preventDefault(); perform('ask');});
  el('ops-question').addEventListener('keydown',event=>{
    if (event.key==='Enter' && !event.shiftKey && !event.isComposing) {event.preventDefault(); perform('ask');}
  });
  el('ops-use-model').addEventListener('change',()=>{el('ops-mode').textContent = el('ops-use-model').checked ? '模型分析 · 发送所选证据' : '本地规则 · 未调用模型';});
  document.querySelectorAll('[data-question]').forEach(button=>button.addEventListener('click',()=>{el('ops-question').value=button.dataset.question; el('ops-question').focus();}));
  document.querySelectorAll('[data-ops]:not([type="submit"])').forEach(button=>button.addEventListener('click',()=>perform(button.dataset.ops)));
})();
