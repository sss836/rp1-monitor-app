# 标零前的驱动构建与检查

标零前，上位机核对当前 deploy 源码、已安装驱动和构建记录。机器人采集端安装包随附 `scripts/calibration_runtime.py` 和 `scripts/update_deploy.sh`。现有采集端如缺少这两个文件，使用上位机「设备设置 → 机器人采集端管理」更新一次安装包。

在上位机的「角度与标定」打开标零流程并核对机器人身份、SSH 绑定和机型。确认现场条件后，上位机使用机器人已安装的构建工具，优先复用与当前源码和安装产物匹配的记录。需要构建时，以普通机器人账号编译并写入记录。若运控、标零或其他电机工具仍在运行，退出这些工具后再开始。

## 在机器人终端拉取并编译

退出运控、标零和其他电机工具后，用机器人普通账号执行。以下命令用于 `/home/robo/roboparty_deploy` 尚未建立的情况。

```bash
cd /home/robo
git clone --branch main --recurse-submodules https://github.com/Roboparty/roboparty_deploy.git
cd roboparty_deploy
source /opt/ros/jazzy/setup.bash
colcon build --packages-up-to roboparty_inference
```

也可以执行完整的 `colcon build`。已有 Git 工作区时，先在该目录执行 `git pull --ff-only` 和 `git submodule update --init --recursive`，再编译。

构建成功后回上位机打开整机标定。上位机优先复用有效的构建记录。首次使用终端构建时，它会核对最近一次 colcon 的成功回执、当前源码及实际安装产物，然后写入可复用记录。记录缺失且现有构建无法核验时，按需补编译。GitHub 拉取和终端构建直接使用上述命令。

工具依赖本机 ROS Jazzy 和随 deploy 提供的第三方归档。上位机补编译采用单任务、非 CPU0 运行。`code_built` 表示当前源码、Python ABI 和安装产物已核对，实机标定仍在现场独立进行。

## 会修改机器人哪些地方

| 位置 | 作用 |
| --- | --- |
| `~/.local/share/rp1-collector-managed/releases/<release>/scripts/` | 随采集端安装包交付的构建和部署更新工具 |
| `/home/robo/roboparty_deploy/build/` | 编译中间文件 |
| `/home/robo/roboparty_deploy/install/` | 更新电机驱动、IMU 和推理的安装产物，以及构建系统安装的脚本/资源 |
| `/home/robo/roboparty_deploy/log/` | colcon 构建日志 |
| `/home/robo/.local/state/rp1-monitor/calibration/runtime-build.json` | 当前构建记录 |
| 同目录 `runtime-build-*.json` | 每次成功构建的历史记录，使用唯一文件名保留 |

构建和 GitHub 更新步骤只准备部署源码、编译产物和构建记录。电机、推理、CAN 总线和现场标零操作保持独立。

## 上位机流程变化

重启上位机后，在“角度与标定”打开标零流程：

1. 核对在线采集连接、机器人身份、SSH 绑定和弹窗中的“标零机型”。有明确绑定配置时自动选24/28；没有时必须独立人工选择。此流程不要求先有角度、关节名称或参考姿态；角度表的显示数量不参与选型，旧角度也不用于标零或保存参考。
2. 完成现场确认，再打开独立终端。终端检查脚本、配置和已审查的 LRO 驱动源码，并使用已安装的工具检查或生成构建记录。
3. 终端内仍需 SSH/sudo 认证及明确的 `CALIBRATE 24` 或 `CALIBRATE 28` 确认，随后才运行原有标零脚本。上位机不会因终端打开或脚本退出就判定标零成功。

109 固件的被动监听解析已在本机回放中按低三位故障码、8/12 字节 Type1 帧修正。该代码没有自动同步到机器人；旧版健康快照补丁不能直接套到新 deploy。

自动限位运动仍未开放。当前已完成模拟执行和离线比较；真机还需要逐电机位置/原始负载的真实接收证据、停止确认及具体运动参数。构建记录只解决“实际安装的是否为这次编译代码”，不能代替这些条件。

具体机器人的构建产物哈希和现场操作记录应单独保留，不随电脑端源码发布，也不作为其他机器人已完成构建或标定的证明。
