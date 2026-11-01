# 上位机整窗退出：本机生命周期审查

日期：2026-11-01。范围：Qt 上位机、预览 Reader、连接预检、诊断/温度/记录 HTTP worker；不连接机器人、不连接已有站点服务、不执行真实 SSH 或电机操作。

## 已复现的问题

旧 `Reader.stop()` 在 GUI 线程执行 WebSocket close 和 `wait(4000)`，不检查 wait 是否成功。调用者随后释放 Reader。以纯本机 fake 握手替换网络连接，等待该 fake 进入 `run()`，调用 stop 后释放外部引用并收集对象，得到：

```text
stop elapsed 4.0 running True
released externally, wrapper alive True
QThread: Destroyed while thread '' is still running
process exit: 134
```

这是能够使整个进程退出的确定缺陷；并不等于已经证明用户此次闪退的实际原因。实际崩溃日志此前缺失。

另有同类退出边界：连接预检/SSH 建链 HTTP worker 最长请求超时可超过退出时原有的 2 秒等待；诊断及温度 worker 虽由模块集合保留、普通页面切换不会销毁，但进程退出时原先没有统一等待。未发现控制会话 complete 被误接到主窗口 close/quit；`app_shell.py` 的页面切换使用已有 `QStackedWidget`。

## 修正

- `Reader.stop()` 仅请求中断并关闭本窗口预览 TCP 的读写方向，不在 GUI 线程等待 WebSocket close handshake 或 QThread.wait。
- 退休 Reader 保留模块强引用，直到 Qt 交付 finished 后才释放、deleteLater；连接替换先解除当前 reader 绑定，原有机器人身份/来源与迟到帧隔离规则保留。
- 主窗口关闭一开始标记 closing；控制/复查/模拟执行已有 shutdown 确认仍保留。随后停止本窗口轮询计时器，禁用继续操作，汇总仍在运行的 Reader、连接预检、诊断、温度及记录 worker，每 100ms 非阻塞检查。线程尚未结束时保持窗口与对象存活。
- `app.exec()` 直接退出的兜底路径也停止预览并等待现有本机读取线程，移除原来的“只等 2 秒后继续销毁”逻辑。
- 没有强制 terminate QThread，没有更改机器人配置、服务或电机指令。

## 验证

- fake 慢握手验证 stop 立即返回，线程完成前和 finished 尚未交付的阶段均保留对象；修复后的独立复现进程退出码为 0。
- fake WebSocket 验证 GUI 只发 TCP shutdown，连接的 context 清理由 worker 执行。
- offscreen 主窗口关闭时仍能分发事件；等待慢 Reader 和四类本机请求结束后再关闭；记录页轮询停止。
- 已有连接预检和 CPU 展示回归验证延迟回包不能切换到旧机器人/旧来源。

具体测试输出由主任务合并报告。软件验证不代表真机控制验收。
