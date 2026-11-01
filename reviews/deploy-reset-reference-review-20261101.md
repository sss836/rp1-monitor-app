# deploy 固定 4 秒复位参考核验 · 2026-11-01

审查方式：只读业务源码、读取已登记本地 deploy 与知识缓存、通过已有 GitHub 连接 GET 核实准确 ref/源码，并运行本机假 SDK/时钟/Qt 测试。未连接机器人，未调用实际 SDK/CAN/ROS，未执行仓库脚本、编译、安装；审查 Agent 只写本报告。

## 结论

新增 `reset_profile="deploy_reset_4s"` 路径未发现必须修复的功能问题。它复用 deploy 的 4 秒 cubic 位置插值和逐物理电机增益规则，取消桌面工人输入速度和总时限；旧准备路径仍保留原有时长规划和速率门。

本次固定 4 秒是目标生成时程，不是所有物理初始姿态均能在 4 秒内实际到位的保证。目标仍为视频检查起点，未替换成 deploy 默认站姿；状态明确保留“未验证实际到位”，不会自动开始限位检查。

## 参考版本与准确来源

- 本地 deploy 的 inference gitlink 与检出 HEAD 均为 `17aa57661bef6a6b55bae733ab461f951b4ca6dd`。
- GitHub `refs/heads/master` 在本次读取时仍为 `d794fa9b25fd7518430152610f30b6c953f78ffe`；通过连接器读取该固定提交的 `src/robot_interface.cpp:299–381`，blob 为 `8c58724edff6f5d03aadcb9ec82d0269ffd741ad`，与知识缓存一致。
- 本地与 d794 的 24/28 `robot.yaml`、默认策略 `configs/default.yaml` 及 `src/utils/decouple_rp1.cpp` 均逐字节一致。
- 固定提交引用：https://github.com/Roboparty/roboparty_inference/blob/d794fa9b25fd7518430152610f30b6c953f78ffe/src/robot_interface.cpp#L299

| 参考行为 | 本地 robot_interface.cpp | d794 robot_interface.cpp |
| --- | --- | --- |
| 4000 ms / 20 ms / 200 步，含首末点 | 313–324 | 311–326 |
| `u²(3−2u)`，从当前逻辑关节角插值 | 328–333 | 330–335 |
| 踝部 pitch/roll 先 IK，再重排至电机 | 336–352 | 338–354 |
| 过程 Kp=配置 Kp÷2.5，Kd=配置 Kd，速度与 FF=0 | 353–356 | 355–358 |
| 终点恢复完整配置 Kp | 367–373 | 369–375 |

2.5 是 Kp 的除数，不是一个固定 Kp。参考源码在最终终点低 Kp 帧之后额外发送完整 Kp 帧；本项目在达到 4 秒的命令 tick 使用完整 Kp，后续继续保持终点。这里是控制规律仿照，不应描述成逐包、逐调度行为完全相同。

## 本项目实现核验

- `calibration_check.py:359–413`：只有明确的新 profile 进入固定 4 秒 cubic；不额外增加旧 2 秒 gain ramp 或停稳窗口。旧 profile 继续使用原 `SmoothMove`、加速度/jerk与时限预算。
- `preparation_control.py:100–143`：新路径从配置 Kp÷2.5 和原 Kd 开始，FF 恒为 0，不学习积分补偿；达到 4 秒后恢复配置完整 Kp。
- `motor_control_runtime.py:604–643`：128 点逻辑姿态到物理电机的闭链路线预检和 SDK 编码检查仍在；新路径不使用旧用户速率来改时长或触发原速率中止，其他路径不变。
- `motor_control_runtime.py:778–825`：仍执行原始物理与映射后力矩中止、逐电机新鲜度、控制节拍、停止/失能请求、发包前校验和终点保持。
- `desktop_motor_control.py:918–926,1059–1103`：命令不携带工人填写的速度/总时限；确认显示固定 4 秒、逐电机复位/最终增益、FF=0、目标是视频起点以及实际到位未验证。确认后的参数变更仍拒绝提交。
- 输入/输出坐标正确区分：视频目标为具名逻辑弧度，闭链成对解算后重排；Kp/Kd 使用绑定版本的物理电机顺序。运行时核对机型、robot.yaml hash 与 profile ID。

原 deploy 的 4 秒 cubic 本身没有旧 a≤1°/s²、jerk≤2°/s³或 20°/s 的保证。它只有端点速度为零；78.06°逻辑行程理论峰速为 29.27°/s。用户已明确选择模仿该复位方式，因此本审查不把授权的速率规划变化误报为意外回归，也不把它表述为原约束不变。

## 验证

1. 新增专测：`test_calibration_preparation.py`、`test_calibration_check_integration.py`、`test_desktop_motor_control.py` 中 `deploy_reset or factory_reset`，**9 passed / 366 deselected**，2.33 秒。
2. 纯准备状态机与控制器回归：`test_calibration_preparation.py`、`test_preparation_control.py`，**192 passed**，1.59 秒。

两组存在部分用例重叠，不相加宣称为 201 个独立用例。均为离线软件证据；已有 Starlette/httpx 弃用警告与本次复位无关。

## 文档收尾

审读时 `docs/CALIBRATION_CHECK.md:13,27,35` 及 `docs/MOTOR_ENABLE_TRAJECTORY.md:7` 仍描述旧人工速度/90 秒与 2 秒渐增、a1/j2；已交主任务同步为当前固定 4 秒路径。历史严格模式的说明可以保留，但必须明确其不适用于当前桌面入口。
