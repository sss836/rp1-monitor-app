(() => {
  const status = document.getElementById('status'), refresh = document.getElementById('refresh-state');
  async function update() {
    refresh.disabled = true; status.textContent = '正在读取电脑后台状态…';
    try {
      const response = await fetch('/api/v1/monitor-recordings/context', {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error('后台状态读取失败');
      const state = await response.json(), source = state.source_kind === 'simulated' ? '模拟数据 · 非实机' : state.source_kind === 'ros2' ? 'ROS 2 来源 · 仍待实机验收' : '尚无当前机器人来源';
      status.textContent = `${state.control_status_unknown ? '中央历史查询，实时采集状态请在上位机确认' : state.connected ? '上位机已接入 ' + state.robot_id : '机器人当前未接通，历史仍可查询'}。${source}。${state.history_database || '长期历史库状态待确认'}。${state.active ? '正在记录：' + state.active.session.run_number : state.control_status_unknown ? '当前为中央只读服务，运行状态请在上位机确认' : '当前没有运行中的记录'}。` + (state.error ? '记录异常：' + state.error : '');
    } catch (error) { status.textContent = '电脑后台暂不可用，请先启动上位机，然后刷新状态。'; }
    finally { refresh.disabled = false; }
  }
  refresh.addEventListener('click', update); update();
})();
