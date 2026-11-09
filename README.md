# RP1 电脑上位机与数据网页

这个仓库只部署在电脑，包含 Qt 上位机、SSH 连接、接收与缓存后台、PostgreSQL 接口、中文网页、监控记录与观测报告，以及可选的 AI 诊断助手。

- 电脑端仓库：[sss836/rp1-monitor-app](https://github.com/sss836/rp1-monitor-app)
- 机器人端仓库：[sss836/rp1-test-monitor](https://github.com/sss836/rp1-test-monitor)

机器人端使用独立的 `rp1_robot` 采集程序。本仓库不包含 ROS/BMS/主机传感器采集器，也没有机器人 `agent` 启动命令；不要把电脑源码、`.venv` 或 `runtime` 上传到机器人。

## Ubuntu 安装包与整机工厂测试

`0.4.4` 的顶部可选登录栏默认连接 `https://10.43.17.214:8443`，使用随包提供的站点 CA。研发和工厂归档继续使用平台账号，送审须由人员核实并显式提交。本地采集和记录无需登录。配置、两域流程及恢复见[平台接入说明](docs/PLATFORM_NATIVE_INTEGRATION.md)。

GitHub Releases 提供 Ubuntu 24.04 amd64 的 `.deb`，内置电脑端 Python 依赖。安装后从应用菜单打开“RP1 工厂测试”，或执行 `rp1-monitor-app factory`。安装、数据目录和从源码构建安装包见[deb 安装说明](docs/PC_DEB.md)。

整机“测试结果与报告”支持导出前编辑，并保留原始结论和修改历史。点击“一键保存并导出 CSV”生成原 17 列 CSV 及同名追溯清单。**带追溯清单的结果需要平台同时支持 `trace_sha256` 和专用 `/trace` 接口。** 不支持时保留本地证据和任务，待平台配套后选中原任务重试。使用说明见[结果汇总](docs/FACTORY_RESULT_SUMMARY.md)和[CSV规范](docs/TEST_RESULTS_CSV_FORMAT.md)。

## 在电脑启动

适用 Ubuntu 图形桌面、Python 3.10+ 和可用的 Python 包源。先准备本项目虚拟环境：

```bash
git clone https://github.com/sss836/rp1-monitor-app.git
cd rp1-monitor-app
./start-monitor.sh setup
```

`setup` 只安装电脑依赖，不连接机器人、不建库、不执行数据库迁移。**首次真机连接前必须先配置 PostgreSQL。未配置时可以打开界面，但不能连接真实 ROS 2 采集端或开始真机长期记录。**

新电脑请按[启动与连接教程](docs/STARTUP.md)及[PostgreSQL 首次配置](docs/POSTGRES_SETUP.md)操作。已有现场数据库和电脑配置继续复用，不要重新建库、重跑初始化或覆盖运行目录。

配置完成后，日常打开上位机：

```bash
./start-monitor.sh
```

在设备设置输入 `ssh robo@192.168.137.250` 和机器人密码，核对首次显示的 SSH 主机指纹后连接。机器人须已经手动启动其独立监听；连接监控只建立网线 SSH 端口转发，不远程安装程序、不启动运控。

打开同一套数据的只读网页：

```bash
./start-monitor.sh web
```

默认入口为 [http://127.0.0.1:18980/robots](http://127.0.0.1:18980/robots)。`/connect` 兼容入口也会转到机器人列表。网页用于状态、历史曲线及报告；SSH 连接、保存指标、频率和记录启停由原生上位机操作。

## 角度检查与标定

按用户新增需求，原生上位机还提供 **角度与标定 → 打开电机控制…**：通过独立SSH内存会话，明确确认后使能24/28台电机并回放整机HMI NPZ/JSON轨迹。支持运行预检、实时反馈与停止并失能，不会随监控连接自动执行。机器人侧读取范围、锁文件、SDK调用、CPU限制及操作步骤见[电机使能与轨迹说明](docs/MOTOR_ENABLE_TRAJECTORY.md)。中央网页和AI不提供该控制接口。

上位机第9页“角度与标定”复用已有 `/joint_states`，支持24／28关节、°／rad、参考姿态保存／载入及当前减参考的偏差。参考JSON只写电脑；不把位置缓存、姿态偏差或退出码当作硬件标定验收。

按用户要求，独立的硬件标零入口可以打开系统SSH交互终端，使用机器人已经部署的LRO脚本，分别指定24／28关节配置并校验版本。它会使能电机、发送阻尼并写硬件零位，需要现场支撑、停用控制程序及逐电机确认；不会在连接时自动启动。源码、配置、构建记录或进程预检不通过时拒绝运行；软件支持不代表24／28关节均已完成真机验收。网页和AI不提供执行接口。详细操作、改动范围和恢复边界见[关节检查与标定说明](docs/JOINT_INSPECTION.md)。

## 记录与 AI

工厂平台的独立桥接命令与边界见[工厂平台桥接说明](docs/FACTORY_PLATFORM_BRIDGE.md)：只读取既有监控 API，上报机器人来源状态；结果 CSV 必须在测试结束、人工核实后显式提交，平台审核独立进行。桥接不改变原 Qt、采集、控制或本地保存，也不会随原程序自动运行。

上位机“监控记录”可多选温度、力矩、SOC、关节电压、电池电流等指标，填写用例标签、机器人标签、场地与保存频率，生成本次编号。保存频率为电脑取样频率，不改变机器人 CAN 或 ROS 发布频率；来源缺失时保存为空值。PostgreSQL 是唯一长期历史库，电脑 SQLite 仅作待传缓存；历史不自动清理，仍须安排磁盘容量与备份。

诊断助手的“连接与模型设置”中选择厂商、填写实际模型 ID 和 API Key，测试成功后勾选“用于诊断对话”。模型请求由电脑后台执行，机器人不运行 AI。详见[启动教程中的 AI 步骤](docs/STARTUP.md#连接-ai-诊断助手)。

关闭 Qt 或浏览器不停止电脑后台。修改数据库连接后，只重开 Qt 不会生效，需要正常重启电脑后台；首次配置的明确操作见[数据库教程](docs/POSTGRES_SETUP.md#让电脑后台使用该数据库)。

## 代码与验证

源码位于 `src/rp1_monitor/`，网页资源位于其 `web/`。电脑安装包名为 `rp1-monitor-pc`，保留共用数据契约和显示逻辑，不包含机器人采集器。可以在隔离环境运行本仓库的软件验证：

```bash
.venv/bin/python -m pip install -e '.[desktop,server,station,dev]'
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest -q tests/test_workstation.py
```

软件和模拟链路通过不等于实机温度、力矩、关节电压或连续运动验收。仓库不携带数据库、真实遥测、设备令牌、SSH 密码或模型 Key；`runtime/`、`.venv/` 和现场配置不提交。发布前端资源时保留随包的第三方许可证。
