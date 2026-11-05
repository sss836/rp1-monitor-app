# Ubuntu 24.04 电脑安装包

此包仅用于 Ubuntu 24.04、amd64、Python 3.12 的电脑，不用于 ARM64 机器人。应用包名为 `rp1-monitor-app`；它安装电脑端 `rp1-monitor-pc`，不安装机器人采集端。电脑源码维护仓库为 [sss836/rp1-monitor-app](https://github.com/sss836/rp1-monitor-app)。

## 构建

在匹配目标系统的构建电脑、项目根目录执行。构建环境需已有 `dpkg-deb`、Python 3.12，以及 Python 的 pip、setuptools、wheel；构建脚本不安装系统组件、不调用 sudo、不安装生成的 deb。

```bash
.venv/bin/python scripts/build_pc_deb.py \
  --output /交付目录/native-monitor-platform-20261106/build \
  --version 0.4.1+platform.20261106.1
```

默认构建会访问 Python 包源下载依赖。脚本先复用 `build_role_packages.py --role pc` 的允许清单导出电脑源码，随后以 `pip wheel --no-index --no-deps --no-build-isolation` 构建当前应用 wheel，再下载 `[desktop,server,station]` 的完整二进制 wheel 依赖。任何依赖没有匹配的二进制 wheel 时失败，不转为源码编译。

在已分离的 `rp1-monitor-pc` 电脑发行仓库运行同一脚本时，脚本自动使用该仓库的清洁源码副本，不要求机器人或开发工程的导出器。也可显式传入 `--source /实际路径/upper-computer`。源码中的 `runtime`、虚拟环境、Git目录及运行文件不会复制；先复制到新输出目录再构建，不修改传入的源码。此模式的输出目录须在传入源码目录之外。

已经准备完整、匹配 Ubuntu 24.04 amd64 / CPython 3.12 的 wheel 集合时，可完全离线构建：

```bash
.venv/bin/python scripts/build_pc_deb.py \
  --output /交付目录/native-monitor-platform-20261106-offline/build \
  --wheelhouse /实际路径/上次构建的wheelhouse
```

`--wheelhouse` 会禁用包索引，只从该目录解析依赖。应用始终使用当前源码新构建的 wheel，不复用目录中的旧应用代码；缺依赖时构建失败。输出目录必须不存在，脚本不会覆盖旧包。失败后新建的部分输出保留供检查，下次使用新目录。

输出包含 deb、`SHA256SUMS`、允许清单导出的源码、应用 wheel、完整 `wheelhouse/` 和 deb 文件树 `package/`。deb 使用 root 所有者与组，不包含现有 `.venv`、`runtime`、设备凭据、数据库、真实采集数据或机器人采集程序。原始测试结果不随安装包发布。

## 安装与启动

先校验下载文件：

```bash
sha256sum -c SHA256SUMS
sudo apt install ./rp1-monitor-app_0.4.1+platform.20261106.1_amd64.deb
```

deb 自带应用及全部 Python 依赖 wheel，安装脚本仅在 `/opt/rp1-monitor-app/venv` 创建独立环境，以 `pip --no-index` 从包内安装。升级会重新安装包内 wheel，确保实际代码来自此次安装包。安装、升级均不启动或停止服务，不连接机器人，不初始化 PostgreSQL，不改写或删除用户结果。

系统层面的 Python venv、Qt/X11/WebEngine 库及中文字体由 deb 的 `Depends` 声明。全新电脑如果缺少这些系统组件，`apt` 仍需包源或提前准备匹配 Ubuntu 24.04 的系统 deb 集合；只拿本应用 deb 不能保证全新离线电脑具备系统依赖。

桌面菜单提供“RP1 整机监控”和“RP1 工厂测试”。也可使用：

```bash
rp1-monitor-app
rp1-monitor-app factory
rp1-monitor-app web
```

默认数据目录为 `${XDG_DATA_HOME:-$HOME/.local/share}/rp1-monitor-app/`，工厂结果和后台状态分别写在其中的 `factory/`、`station/`，独立平台工作区位于同级 `station-platform/`。应用由当前普通用户运行；不在 `/opt` 写测试结果。查看教程及 CSV 模板可进入 `/usr/share/doc/rp1-monitor-app/`。包内保留的导出源码在 `/opt/rp1-monitor-app/source/`，日常使用上面的安装入口。

需要明确复用原有电脑目录时，先关闭该电脑的旧界面，确认现场后台和记录状态，再为新入口指定原 `runtime` 路径。例如：

```bash
RP1_MONITOR_HOME=/home/你的用户名/Desktop/rp1-app/runtime rp1-monitor-app factory
```

这只是选择原有本地目录；安装程序不会迁移、复制、清空原数据，也不会自动接管或重启现有后台。真机长期监控仍按已有 PostgreSQL 配置与设备凭据运行，不能因安装新包而重建生产库。

## 卸载和验证边界

卸载只移除包文件及 `/opt/rp1-monitor-app/venv` 中的包专用依赖，用户目录、原源码目录的 `runtime`、已保存结果及导出的 CSV 均保留。卸载脚本不启停后台或机器人服务。

包结构测试只验证角色边界、启动参数和安装脚本行为，不代表完成图形桌面、实机通信、连续运行或硬件验收。模拟数据保留 `simulated` 来源；安装新包也不代表已有测试通过。
