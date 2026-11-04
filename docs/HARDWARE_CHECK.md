# RP1 板卡单文件检查

**升级后的电机参数需单独核对：本脚本与旧版 TUI RP025 的型号基准仍是旧表，不能直接用它们给新版 deploy 刷参数。** 已核查的 deploy 电机驱动 `2ee21b2` 将型号 60 的 SPD/TOR 改为 40 rad/s / 40 N·m，型号 75 改为 35 / 65，型号 97 改为 16 / 165；实际电机内部值仍需读取比对。此处列的是协议编码量程，不是行走增益或允许测试的速度、力矩。没有写过参数也不证明参数一定错误，不应据此直接写入。

原生上位机现在有独立入口：**工厂测试 → 第1项 → 电机参数检查 / 更新**。选择SN和24/28轴后，输入本次sudo密码，点击检查全部电机。界面显示逐台实际值、期望值、读取到的固件版本和缺失原因，结果自动附到当前SN。检查前须按原流程失能电机、停止deploy并退出标零等电机工具；不会自动停止它们。

更新只在本次完整读出、已勾选的差异电机上进行。人员查看差异并确认后，先保存原值，再重新读取核对快照，更新KP/KD/POS/SPD/TOR编码范围并逐项读回。ID、硬件零位、CAN_TIMEOUT和其他Pn不写入，不调用使能、标零或运动；不修改TUI或deploy代码。源码与安装配置、已安装二进制量程表无法核实时拒绝操作。中断保留已尝试项并停止，不自动重试。版本允许清单、全部Pn出厂参数、掉电保持和步态验收尚未由此入口验证，不把“范围一致”当作整机合格。

入口为 [check_rp1_hardware.sh](../scripts/check_rp1_hardware.sh)。把这一个文件复制到板卡即可，不需要复制原来的 `deploy_motorlro_params.py`，不需要本监控工程，也不需要外网。内部使用 Python，以便解析 YAML、日志和二进制 CAN 帧。

板卡需要 Bash、Python 3、PyYAML（通常为 `python3-yaml`）、`iproute2`、systemd 的 `journalctl/systemctl`，以及已有的 WS2812 驱动和 `robopi-addon`。直接使用 Linux SocketCAN，不依赖 ROS 环境、python-can 或 can-utils。脚本不会安装这些依赖。

在板卡上运行，电机需已经上电、CAN 接口需已由现有部署配置好。先把脚本复制到 `~/check_rp1_hardware.sh`；以下示例使用实际部署目录 `~/roboparty_deploy`。说明页本身不会安装或执行脚本：

```bash
# 24 DOF
sudo bash ~/check_rp1_hardware.sh --deploy-dir ~/roboparty_deploy --robot rp1_24dof

# 28 DOF / 7 DOF 手臂
sudo bash ~/check_rp1_hardware.sh --deploy-dir ~/roboparty_deploy --robot rp1

# 明确指定配置文件
sudo bash ~/check_rp1_hardware.sh --robot rp1_24dof \
  --config ~/roboparty_deploy/src/inference/robots/rp1_24dof/robot.yaml
```

默认部署目录是调用 sudo 的原用户的 `~/rp1_deploy`，机型默认为 `rp1_24dof`。显式指定 `--deploy-dir` 可以避免自定义安装路径带来的歧义。BMS/IMU/电机保持只读，无电机参数写入、初始化、标零、推理启停或服务重启入口；也不会执行或 source 仓库内的其他脚本。电机检查会向 CAN 发送参数查询帧。

**按用户明确要求，灯板检查会主动执行 `robopi-ws2812 solid 80 80 80`，将 RGB 设为80、80、80，并在脚本结束或中断后保留已发送的颜色。** 不执行 `off` 或修改灯板开机服务；其他服务或后续命令仍可能覆盖颜色。本脚本不再属于完全只读的硬件检查。

| 项目 | 检查条件 | 判定边界 |
|---|---|---|
| BMS | `bms.service` active；当前启动、最近30秒日志中最新电池电压为有效正数；显示 journal 原始时间、年龄、原文 | 兼容 `[BMS Data]`、`[CAN BMS Data]`、`[NRF PMIC]`；连接成功文字、电源轨标签不算电压。无可识别近期样本为 UNKNOWN。日志可能混合缓存字段，PASS 不证明每个485字段刚采样、温度/保护位正常或电压符合某型号电池额定范围 |
| WS2812 灯板 | 查询 `robopi-addon` 安装状态、命令与字符设备；齐全后执行 `robopi-ws2812 solid 80 80 80` | 环境缺失、命令错误或超时为 FAIL；命令返回成功后显示已发送RGB并保留UNKNOWN，因为无灯珠反馈，实际颜色与亮灯待现场确认 |
| IMU | YAML 中 HIPNUC CAN 接口、bitrate 和 IMU ID；指定源地址的 J1939 四元数 PGN `FF46`、角速度 PGN `FF37` 均持续到达；四元数范数0.95–1.05 | 默认采样5秒，每路至少50 Hz、至少3个有效帧、最大间隔不超过 `max(0.2秒, 3/最低Hz)`，含窗口起止缺口；这些为脚本工程检查门槛。显示原始帧和解码值，不校准安装方向或绝对姿态精度，不展示线加速度 |
| LRO 参数 | 按 YAML 的总线、ID和型号完整检查24或28台电机；每台查询 KP/KD/POS/SPD/TOR/CAN_TIMEOUT 六项 | 查询 `E0 17/18/19/1A/1B/1F`；回复类型 `A0`；CAN FD 使用 BRS。按用户 Python 文件的编码尺度逐整数精确比对，非其原 `read` 模式“读到就算成功”的规则。无响应、错误值、未知型号、非 LRO、地址重复、数量不齐都不能通过 |

电机基准中的 KP/KD 是协议范围，不是 `robot.kp/kd` 的运行控制增益。四种型号的 POS 均为 ±12.5 rad；速度和力矩按原 Python 文件的型号表选取。`CAN_TIMEOUT` 的比对基准沿用原文件的 **0 ms**，脚本只读出并比较，不修改设备值。接口必须 UP、非 BUS-OFF/STOPPED/SLEEPING；CAN FD 总线需启用 FD。虚拟 CAN 接口不能作实机判定。

默认保留全部六项检查，终端只显示每台电机一行，例如 `[PASS] can1/2 model=3: 参数 6/6`。失败时在该行下面展开不一致、设备错误码或无响应的项，包含实读值、期望值及原始回复。加 `-v` 或 `--verbose` 才展开全部正常参数；`--versions` 的版本状态也合并到每台汇总行。

可以调整观测门槛和超时，例如：

```bash
sudo bash ~/rp1_deploy/check_rp1_hardware.sh --deploy-dir ~/rp1_deploy \
  --seconds 10 --imu-min-hz 100 --timeout 0.5 --bms-max-age 20
```

每项电机参数默认等待0.3秒。24台全部无响应约43秒，28台约50秒，另加IMU采样与状态查询；单项失败后继续汇总。采样最多60秒，每项电机超时最多5秒。脚本只输出终端文本；若需要文件可自行重定向，脚本不会上传或删除任何数据。

退出码：`0` 所有项目 PASS；`1` 至少一项 FAIL；`2` 没有 FAIL 但存在 UNKNOWN（命令行错误也为2）；`130` 用户中断。当前灯板设置始终保留实际亮灯的现场确认，因此命令返回成功时也不会产生“整机全部通过”的结论。

## 版本信息

加上 `--versions` 开启附加信息采集：

```bash
sudo bash ~/rp1_deploy/check_rp1_hardware.sh --deploy-dir ~/rp1_deploy \
  --robot rp1_24dof --versions
```

- **板卡软件包版本**：分别查询 `roboparty-bms`、`roboparty-motors`、`roboparty-imu`、`robopi-addon`、`robopi-config`。仅说明已安装的软件包，不能代替外接设备固件版本；源码安装时可能没有包记录。
- **BMS 设备 FW/HW**：先获取 `bms.service` 当前 `InvocationID`，仅在当前启动、该次服务运行的日志里读取 `FW Version / HW Version`，显示日志时间和原始十六进制版本码。不会为获得启动日志而重启服务。无日志或格式不支持为 UNKNOWN；零值视为未确认，不能解释为有效“版本0”。SCUD485 在所核查驱动中未提供 HW/SW，填充零值。
- **每台 LRO 版本查询**：显式加 `--versions` 时，在原六项参数检查之外附加 `E0 1E`。在 `--timeout` 窗口内采集同一电机ID的全部匹配查询回复，按用户提供的《LeadRobot_CAN自定义协议3_V1.00》第4.5节解码软件、硬件和协议版本。正常结果合并到每台一行，例如 `；版本=READ FW=1.2.3 HW=1.0.2 PROTO=3.0.1.0`。`READ` 仅表示版本数据读取、解析成功，不代表符合允许版本清单。该可选查询每台再增加一个 `--timeout` 秒采样窗口。

版本回复的数据域总共 **12字节，包含2字节报文头和10字节版本分量**。Motor ID 位于CAN标识符，不占数据域。布局依据为PDF物理第13页查询码30（十进制，即 `0x1E`）、第14页报文头、第16–17页类型5查询回复表：

| 字节偏移，从0开始 | 字段 |
|---|---|
| Byte0高3位 | 查询回复类型5；低5位为电机错误码，必须为0才作为无错误版本解析 |
| Byte1 | 查询码 `0x1E` |
| Byte2–4 | 软件/固件版本，三个 `uint8` 分量，输出为 `FW` |
| Byte5–7 | 硬件版本，三个 `uint8` 分量，输出为 `HW` |
| Byte8–11 | 协议版本，四个 `uint8` 分量，输出为 `PROTO` |

版本分量按0–255的无符号整数显示，点号只是展示分隔符；文档未规定主版本、次版本或补丁语义。不按ASCII、BCD或合并的24/32位整数猜测编码；也不把电机版本全零自动视为BMS的占位规则。

以下是 **simulated合成示例，非真实电机版本**：

```text
payload: A0 1E 01 02 03 01 00 02 03 00 01 00
输出: [PASS] can1/2 model=3: 参数 6/6；版本=READ FW=1.2.3 HW=1.0.2 PROTO=3.0.1.0
```

正常时使用 `--verbose` 才展开版本原始帧、字节数及相对查询的主机接收时间；无响应、长度非12字节、非零电机错误码或冲突回复时显示 `版本=UNKNOWN`。异常时自动保留全部匹配帧的原始证据。相同有效回复可去重；不同有效版本不任选一条；有效帧混入畸形/错误回复也不默认为成功。版本没有短帧拼接规则，不套用代码29的UUID三帧规则。

**版本查询在运控停用的检修窗口执行。** 所核查 LRO 驱动的接收回调没有按 `fb_type` 分流，可能将长度至少8字节的查询回复误按状态帧解码；本脚本自身隔离处理原始版本回复，但不能改变同一总线上其他进程的驱动行为，也不会自动停止运控服务。

若要手动读取某台电机的版本回复，先从板卡 YAML 确认总线和ID。以下以 **can1 上 ID=2 的 CAN FD 电机** 为例：

```bash
# 终端1：先监听回复
sudo candump can1,002:7FF

# 终端2：只发版本查询，不控制电机
sudo cansend can1 002##1E01E
```

其中 `002` 为电机ID的十六进制，`##1` 表示 CAN FD + BRS，真正的查询数据只有 `E0 1E`。监听输出中的本机发送帧不是回复；必须看到同一ID、类型5、查询码 `1E` 的目标响应。无错误的版本头为 `A0 1E`。手动示例需要 `can-utils`；单文件脚本自身不需要。脚本已按上述协议解码版本分量，不把驱动软件包版本当电机固件。

版本信息是独立的信息采集，不改变原六项参数统计和四项检查退出码；版本未知或READ均不能解释为版本合规。要自动判断“版本正确”，还需要按电机型号、电池型号和硬件批次确定的允许版本清单。

还适合继续纳入的只读项目：

| 优先级 | 可扩展项 | 已核查的来源与边界 |
|---|---|---|
| 高 | CAN 总线 RX/TX 错误、丢帧、bus-off/restart 计数 | `ip -details -statistics -json link show`；累计值需与本次增量区分，不能只凭历史计数判本次故障 |
| 高 | 电池序列号、温度、保护位、循环次数、SOH | 已有 daemon 的只读 socket 快照；需明确 BMS 类型、实际安装版本和126字节协议。缺字段不能填零冒充有效，SCUD485 不提供 SOH；不打开第二个进程抢读485串口 |
| 高 | 电机温度、MOS 温度、故障码、反馈覆盖率 | 被动监听既有反馈或订阅现有诊断；缺反馈标未知，不调用 `refresh_motor_status()`，该函数会发送 MIT 指令 |
| 中 | 电机厂家、MCU UUID、批次一致性 | 上游定义只读 `E0 00` 和 `E0 1D`；UUID 为多帧，需完整组帧和布局，不能取到一帧就认定完整 |
| 中 | 部署 Git 提交、子模块提交、配置校验和与允许版本对照 | 本地只读采集即可；版本一致性应按设备型号/批次分组，允许版本来自维护确认的清单 |

以上扩展项是建议，当前入口未实现它们；IMU 频率、最大间隔、ID/波特率和电机六项参数覆盖率已在现有检查内。

源码依据：

- [robopi-config 的 BMS 检测](https://github.com/Roboparty/robopi-config/blob/trixie/usr/lib/robopi-config/robopi-bms-hwtest)：原程序会重启服务，本脚本仅读状态和日志。
- [RoboPi Addon 检测](https://github.com/Roboparty/robopi-config/blob/trixie/usr/lib/robopi-config/robopi-addon-hwtest) 的状态检查对象，以及 [WS2812 命令实现](https://github.com/Roboparty/robopi_addon/blob/cb8daa08237fe91e04e6dfce971d6ceab81e5910/src/ws2812_pwm6.c)：按用户要求仅执行 `solid 80 80 80`，不调用 demo、off 或 SIG 输出。
- [deploy 固定版本的子模块映射](https://github.com/Roboparty/roboparty_deploy/blob/c8c213508dc57f83f72ab33cdb760d178cfdb9e7/.gitmodules)。
- [24 DOF](https://github.com/Roboparty/roboparty_inference/blob/649b2f01ddae4217da2f53534732f06d268b5eb9/robots/rp1_24dof/robot.yaml) / [28 DOF](https://github.com/Roboparty/roboparty_inference/blob/649b2f01ddae4217da2f53534732f06d268b5eb9/robots/rp1/robot.yaml) 配置。
- [HIPNUC J1939 解码](https://github.com/Roboparty/roboparty_imu/blob/7a6e6d2eeef8ff3513cee54a8fa833773efd42f3/src/drivers/hipnuc/hipnuc_j1939_parser.c) 与 [IMU 初始化](https://github.com/Roboparty/roboparty_imu/blob/7a6e6d2eeef8ff3513cee54a8fa833773efd42f3/init_imu.sh)：初始化会改接口波特率、写设备配置并复位，本检查不执行。
- 用户提供的 `/home/shn/Desktop/deploy_motorlro_params.py`：型号参数、查询码、字节序和缩放基准；原文件未修改。
- 用户提供的 [LeadRobot协议PDF](/home/shn/Downloads/LeadRobot_CAN自定义协议3_V1.00.pdf)：第13页查询、第14页报文头、第16–17页4.5节版本布局；原PDF未修改。
- [LRO 查询枚举](https://github.com/Roboparty/roboparty_motors/blob/45eac3ff3dc860e3bd688c1e62a3c5368166849a/src/drivers/lro/lro_motor_driver.hpp)：厂家、UUID、版本查询码；[驱动实现](https://github.com/Roboparty/roboparty_motors/blob/45eac3ff3dc860e3bd688c1e62a3c5368166849a/src/drivers/lro/lro_motor_driver.cpp) 确认 `refresh_motor_status()` 的控制行为，因此未调用。
- [BMS 启动版本日志](https://github.com/Roboparty/roboparty_bms/blob/bc6be959e0ea9b27644e38c6dece80e9a6377685/src/bms_daemon.cpp)；[SCUD485 的占位版本值](https://github.com/Roboparty/roboparty_bms/blob/bc6be959e0ea9b27644e38c6dece80e9a6377685/src/drivers/scud485/scud_bms_driver.cpp)。

软件验证使用 [100项合成帧与命令替身测试](../tests/test_hardware_check.py)，覆盖误报、超时、值不一致、帧过滤、版本查询/字段解码/长度及错误码/冲突回复、BMS版本占位值、电机精简/详细输出、灯板固定RGB指令及失败处理；同时核对两套上游 YAML 的全部总线/ID/型号及原 Python 的四种型号、六项基准。**未连接 RP1，未实际执行亮灯命令，未完成实机验收。**
