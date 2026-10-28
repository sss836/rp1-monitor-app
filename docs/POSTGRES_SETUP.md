# 电脑长期库：PostgreSQL 首次配置

真实 ROS 2 机器人必须使用 PostgreSQL。没有私有数据库连接配置时，上位机可以打开，但会拒绝真机连接与真机长期记录。SQLite 只保留旧历史和作为新数据的待传缓存，不是第二份长期库。

这份教程供电脑/数据库维护者操作。机器人不运行 PostgreSQL，也不接收数据库地址或口令。本仓库不携带现场凭据；应用启动不执行生产 DDL。

## 已有数据库与新数据库分开处理

**已有 RP1 现场数据库和原电脑配置：继续复用，不重建。** 不运行本教程的新库创建步骤，不重跑已执行 SQL。先在现有上位机刷新记录状态，核对数据库、记录表和待传状态。

新电脑连接已有数据库时，由维护者：

1. 确认应用登录身份、连接地址、schema 与已安装迁移；数据库升级前先备份，并在隔离环境验证可恢复。
2. 仅执行缺失且已审核的迁移。当前 SQL 不是可反复运行的初始化脚本；对象已存在时停止核对，不删表重建。
3. 私下提供该电脑专用的应用连接文件。如果库里已经登记目标机器人，还须私下导入与该登记匹配的设备凭据。后台应停止后再备份并合并 `runtime/station/devices.json` 的相应条目，保持本人所有、0600；不能公开 token、覆盖其他机器人或凭空生成一个替代值。没有匹配凭据时上位机会拒绝接管，不自动旋转数据库中的绑定。
4. 按文末步骤配置并重启电脑后台。不要复制其他电脑整个 `runtime/`、SQLite 队列或虚拟环境。

下面的新库步骤只适用于维护者已确认的**全新专用数据库与空数据目录**，不是对现有生产库的升级命令。

## 新库需要哪些文件

| 文件 | 用途与顺序 |
| --- | --- |
| `deploy/postgres/compose.review.yml` | PostgreSQL 18 容器模板，仅绑定电脑回环地址；数据和管理员口令使用本机私有路径 |
| `001_schema.review.sql` | 新建专用 `rp1_monitor` schema、核心表、索引和约束 |
| `002_permissions.review.sql` | 建立无登录业务组角色与安全视图，定义应用最小权限 |
| `004_admin.review.sql` | 在 001/002 后建立受控更正、管理员审计和专用函数所有者 |
| `005_robot_configuration.review.sql` | 在 001/002/004 后建立机型、映射和温度策略的版本化配置 |
| `006_monitor_recordings.review.sql` | 建立监控记录与保存点表，授予所需追加/查询权限；长期记录必需 |
| `003_legacy_import.review.sql` | 可选旧数据导入模板，**不在默认新库步骤中执行** |

新库按 **001 → 002 → 004 → 005 → 006** 执行。004 要求迁移超级用户，005 依赖 004；应用运行身份不获得超级用户或这些表的所有权。002 创建的是 `NOLOGIN` 组角色，不能拿组角色名和密码直接登录。机器人记录与正式测试、审批、MTBF 隔离。

本仓库不提供 `setup_central_postgres.py` 一键初始化。下面显式启动容器、审核 SQL、创建登录角色，不把软件安装当作数据库初始化。

## 准备新的 Docker 实例

先完成[电脑软件环境准备](STARTUP.md#首次在电脑准备软件)，确认 Docker 与 Compose 可用，并核对电脑磁盘、空闲回环端口和官方 PostgreSQL 18 镜像。以下示例项目名 `rp1-monitor-pc-db`、端口 `55433`、目录 `runtime/postgres-new/` 都只用于新实例；已有同名项目、目录或占用端口时先停止核对，不覆盖。

在电脑仓库根目录执行：

```bash
python3 - <<'PY'
import os
import secrets
from pathlib import Path

root = Path('runtime')
root.mkdir(mode=0o700, exist_ok=True)
directory = root / 'postgres-new'
directory.mkdir(mode=0o700)  # 已存在则报错，不覆盖
(directory / 'data').mkdir(mode=0o711)
(directory / 'credentials').mkdir(mode=0o700)
path = directory / 'credentials' / 'pg-admin-password'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(secrets.token_urlsafe(32) + '\n')
PY
cp deploy/postgres/compose.review.yml runtime/postgres-new/compose.yml
export RP1_PG_DATA_DIR="$PWD/runtime/postgres-new/data"
export RP1_PG_ADMIN_PASSWORD_FILE="$PWD/runtime/postgres-new/credentials/pg-admin-password"
export RP1_PG_PORT=55433
```

这些操作只创建电脑上的新私有目录和随机管理员口令文件；不输出口令，不操作机器人。使用新的专用数据目录，不能把已有数据库卷指向这里重初始化。Compose 挂载的是 `/var/lib/postgresql` 父目录，与此 PostgreSQL 18 模板保持一致。该挂载父目录设为 0711，使容器内 postgres 用户可穿越；外层 `runtime/postgres-new/` 和凭据目录仍为 0700，实际 PGDATA 子目录由数据库入口设置为 0700。

维护者获取并核对官方 `postgres:18` 镜像及其 digest，把复制后的 `compose.yml` 中 `image` 改为已审核的 `postgres@sha256:实际值`，然后：

```bash
docker compose --project-name rp1-monitor-pc-db \
  -f runtime/postgres-new/compose.yml up -d
docker compose --project-name rp1-monitor-pc-db \
  -f runtime/postgres-new/compose.yml ps
```

等待数据库 healthy 后继续。上述环境变量在当前终端生效；以后管理容器时需重新设置同一组三个非秘密路径/端口变量。不要把口令内容写进环境变量、命令行或公开 `.env`。

模板不会将 SQL 挂载到自动初始化目录，容器 healthy 不代表 schema、角色或记录表已准备完成。不要执行 `down -v` 或删除数据目录来排错。

## 审核并执行新库 SQL

逐份阅读上述 SQL。以下块仅在这份新库执行一次，遇错停止；以容器内 `postgres` 系统用户通过本地 peer 认证运行，不把管理员口令放进命令：

```bash
(
  set -e
  for migration in \
    001_schema.review.sql \
    002_permissions.review.sql \
    004_admin.review.sql \
    005_robot_configuration.review.sql \
    006_monitor_recordings.review.sql
  do
    docker compose --project-name rp1-monitor-pc-db \
      -f runtime/postgres-new/compose.yml exec --user postgres -T postgres \
      psql -X -v ON_ERROR_STOP=1 -U postgres -d rp1_monitor \
      < "deploy/postgres/$migration"
  done
)
```

每份迁移各有自己的事务。若中间失败，之前已提交的迁移仍保留；先核对实际对象和错误，再决定仅继续尚未执行的文件，不能从 001 全部重跑。默认不执行 003，也不导入仓库外的真实遥测。

## 创建电脑应用登录身份

应用只需加入 `rp1_monitor_app`。它不是数据库所有者，不加入 `rp1_monitor_admin`、内部 `rp1_monitor_admin_executor` 或其他高权限身份。已有同名登录时先核对，不覆盖或重置既有账号。

打开新库的交互式 SQL 终端：

```bash
docker compose --project-name rp1-monitor-pc-db \
  -f runtime/postgres-new/compose.yml exec --user postgres postgres \
  psql -X -v ON_ERROR_STOP=1 -U postgres -d rp1_monitor
```

在 `psql` 内逐条执行：

```sql
CREATE ROLE rp1_pc_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS CONNECTION LIMIT 10;
GRANT rp1_monitor_app TO rp1_pc_api;
ALTER ROLE rp1_pc_api SET search_path=rp1_monitor,pg_catalog;
ALTER ROLE rp1_pc_api SET statement_timeout='15s';
ALTER ROLE rp1_pc_api SET idle_in_transaction_session_timeout='30s';
\password rp1_pc_api
\q
```

`\password` 会交互式询问新口令，避免将明文放进 SQL 历史或命令参数。登录角色必须自己设置 `search_path` 和超时，组角色设置不会自动传给登录成员。请用秘密管理器保管该应用口令，不使用上面的数据库管理员口令。

独立管理员页面如需启用，应另配个人管理员身份和审计登录；不能因此给后台应用增加管理员权限。本教程不创建管理员网页登录。

## 准备私有应用连接文件

以下示例对应刚建的本机 `55433/rp1_monitor`、登录 `rp1_pc_api`。使用新终端时先回到电脑仓库根目录。脚本从终端隐藏输入应用口令，自动完成 URL 编码，不把口令放进参数或环境变量，也不输出完整 URL：

```bash
.venv/bin/python - <<'PY'
import getpass
import os
from pathlib import Path
from sqlalchemy.engine import URL

directory = Path('runtime/private')
directory.mkdir(mode=0o700, exist_ok=True)
if directory.is_symlink() or directory.stat().st_mode & 0o077:
    raise SystemExit('私有目录权限须为0700，且不能是符号链接')
url = URL.create(
    'postgresql+psycopg', username='rp1_pc_api',
    password=getpass.getpass('应用数据库口令：'),
    host='127.0.0.1', port=55433, database='rp1_monitor',
    query={'options': '-csearch_path=rp1_monitor,pg_catalog'},
)
path = directory / 'postgres-url.txt'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(url.render_as_string(hide_password=False) + '\n')
print('已写入私有连接文件；未输出内容。')
PY
```

已有数据库由维护者提供对应文件，不照搬示例主机、端口或账号。文件必须是本人所有的一行连接 URL，权限 0600，父目录私有；不允许符号链接。URL 格式仅供识别：

```text
postgresql+psycopg://<应用登录>:<已编码口令>@<数据库主机>:<数据库端口>/<数据库>?options=-csearch_path%3Drp1_monitor%2Cpg_catalog
```

不要把这个占位示例当实际配置。`18980` 是上位机网页 HTTP 端口，不填在数据库端口位置。连接文件不上传 GitHub、不复制到机器人、不发到聊天。

## 让电脑后台使用该数据库

下面方法适用于**第一次启动且没有现存 18980 后台**的电脑，方便在配置后明确停止和重启，不需要查找或批量终止进程。

1. 在电脑仓库的第一个终端前台启动后台：

   ```bash
   .venv/bin/python -m rp1_monitor station \
     --state-dir "$PWD/runtime/station" --port 18980
   ```

   这个终端保持运行。此时尚未读取 PostgreSQL 配置，只用于打开配置界面；不要连接真机或开始记录。

2. 第二个终端进入同一仓库，运行 `./start-monitor.sh`。在上位机“监控记录”点击“配置长期历史库”，选择刚准备的 `runtime/private/postgres-url.txt`。程序将它复制为 `runtime/station/postgres-url.txt`，已有文件时拒绝覆盖；不建库、不执行 SQL。
3. 关闭 Qt 窗口，回到第一个终端按 `Ctrl+C` 正常停止**电脑后台**。这是本机进程，不是机器人运控。首次配置过程没有真机连接或活动记录。
4. 再运行 `./start-monitor.sh`，新后台会加载 PostgreSQL 配置。只关闭再打开 Qt 不会重启一直运行的后台，因此不能省略上一步。
5. 在“监控记录”刷新状态，确认 PostgreSQL 可达且记录表就绪，再按[启动教程](STARTUP.md#连接机器人)连接机器人。试做短记录，结束后确认待传队列正常、曲线与缺失项符合真实来源；不能把界面打开当作已保存。

如果已经有后台、活动记录或待传队列，不再执行首次前台启动。由维护者先核对并正常结束记录、确认已有队列安全，再定位属于本项目和该运行目录的后台正常重启；不要杀全部 Python、删除 SQLite 或覆盖凭据文件。已有 PostgreSQL 配置文件的更换也应备份并单独审阅，界面不会直接覆盖。

## 保存和维护的边界

PostgreSQL 保存长期摘要与记录点，电脑 SQLite 保存确认入库前的待传项。长期库不可达时只能在有限缓存预算内继续，达到容量/磁盘边界会暂停新采样并保留已有项。当前没有自动月度清理或 PostgreSQL 分区，不能承诺无限保存。

安排数据库容量检查、受保护的备份和隔离恢复演练。备份、数据卷、连接文件、`runtime/station/`、真实遥测及日志都不能进入源码仓库。本次配置不要求修改机器人、驱动、BMS、CAN、策略或推理进程。
