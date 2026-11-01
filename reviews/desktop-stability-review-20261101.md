# 上位机卡顿与整窗退出修复：独立只读复核

复核日期：2026-11-01（北京时间）。仅阅读本机源码，运行无网络的 offscreen Qt + FakeWorker 复现；没有连接机器人、服务或真实 SSH，没有使能、发送轨迹或改变控制频率。业务源码未由复核代理修改。

## 发现

### 已闭环 P2：关闭过程中已排队的记录回调仍可能接续发起查询

`desktop_recordings.py:421` 的 `current()` 原实现只检查面板是否仍有效及请求代次。主窗口进入 `_closing=True` 后，窗口会保留以等待在途线程结束，因此 `isValid(self)` 仍为真。关闭前已进入 Qt 队列的 context 成功回调可继续进入 `refresh_context()` 中的 `load_records()`，新增一次请求。虽然普通线程注册表仍会保活该请求，这不会直接证明 QThread 析构崩溃，但会延长关闭时间，并与“停止后不再发起查询”不符。

本地复现：FakeWorker 不执行任何 HTTP；第一次 `call()` 的完成回调接续第二次 `call()`，设置主窗口 `_closing=True` 后再发出第一次的已排队 `done`。观察到请求数量由 1 变为 2。建议在 `call()` 入口和完成结果的 `current()` 中检查根窗口关闭状态，且以同一 fake 场景验证关闭后不接续请求。

复核更新：主任务已在 `call()` 入口与 `current()` 同时检查 `self.window()._closing`。独立阅读修复并执行 `test_closing_window_ignores_queued_context_result_without_starting_history_request`：1 passed。该用例验证关闭后已排队 context 回包既不应用状态，也不触发历史查询，随后显式调用 `call()` 也不新增 worker。本项已闭环。

## 已核对的边界

- `start_pose_report()` 先检查 250ms 新鲜度再使用数学缓存。缓存键包括机型、完整物理电机配置、角度、方案关节顺序及起点；偏移或映射改变会重算并重新验证。缓存不改变机器人端校验，也不产生运动授权。
- 物理反馈和展开映射表复用已有单元格，交互式列宽避免每次更新全表测量；更新真实变动的角度与状态，没有以零替代缺失值。
- 热图只在区域内容/描述变化时更新；静态机器人图按尺寸缓存。隐藏温度图仍记录有限长度 buffer，切回首页时刷新，断连和 stream 切换清空历史。
- `Reader.stop()` 请求中断并只 shutdown 底层 socket，握手与 context 清理由接收线程执行。`_RETIRED_READERS` 持有运行中线程，直到 Qt 派发 `finished`，避免超时后析构运行中的 QThread。旧 reader 的消息还需匹配当前 reader 对象身份，不能改写新连接。
- 普通关闭流程先停止预览、停止子定时器，再异步等待普通在途请求。既有 HTTP timeout 并非强制总时限：诊断请求可等待 100 秒，导出含两个阶段。因此应说明“等待本机在途请求完成”，不能承诺任何关闭都瞬间完成。
- 新诊断文件使用私有权限，不写 exception value、源代码行、locals、SSH 密码或遥测；Qt fatal 只记固定描述。保留原终端报错渠道。它有助于下次崩溃取证，不能反推用户这次消失的唯一原因。
- 本轮界面改动没有改变机器人控制 50Hz、端点判据、反馈新鲜度或现场确认要求。

## 非本轮新增的退出边界

`_run_desktop()` 中直接 `QApplication.quit()` 的 fallback 只等待普通预览/HTTP worker，未覆盖独立 MotorControlWorker。常规主窗口关闭走 `closeEvent()` 中已有控制会话 shutdown；本轮不应新添绕过 closeEvent 的 `quit()` 路径。若以后增加全局退出按钮或自动退出，应一并排空控制 worker，并保持失能/物理停止确认语义。

## 结论

记录页迟到回调已修复并通过独立纯本机回归，未发现本轮缓存、接收线程保活或诊断实现中的新控制授权绕过；本次复核无遗留阻断项。整窗退出仍需用户下一次私有崩溃栈验证，不能将本地可复现 QThread 风险等同于此次现场根因。
