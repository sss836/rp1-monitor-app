# 整机上位机工厂平台独立桥接

本地开发基线：`4b325a518b94859437cbed9bff9798b1db12f87c`，分支 `codex/factory-platform-bridge-20261105`。本轮不上传 GitHub，不合并、不发版，也未启动现场桥接。

## 实现范围

新增 `rp1_monitor.factory_bridge`，可用 `python -m rp1_monitor.factory_bridge` 或安装源码后使用 `rp1-factory-bridge`。桥接使用 Python 标准库，无新增第三方运行依赖；不会导入或启动 Qt、station、relay、Recorder 或机器人程序，不读写原监控 SQLite/PostgreSQL，不改变原采集、控制、记录和保存方式。

它分成两个独立命令：

- `presence`：只读已有电脑后台的 `GET /api/v1/robot-monitors`，向平台专用工厂 presence 接口报告完整来源快照，用于工厂机器人卡片。
- `submit-results`：只接收用户明确指定、已结束、已人工核实的 **17 列结果汇总 CSV**，通过工厂专用归档接口提交。它不是原始时序曲线，不会生成研发执行或 MTBF 统计。收到文件与测试数据批准是两个不同状态；批准仍由平台独立审核完成。

原网页继续只读，采集启停仍在原生上位机。桥接没有设备连接、断开、策略执行、自动扫描导出目录或审核命令。

## 配置与身份

平台需已部署对应的 `factory/robots/presence`、`factory/robot-result-archives` 接口及迁移。实际部署和工位账号权限应先核对，不能以本地软件测试代替现场生效验证。

`source_id` 是这台上位机稳定编号，presence 和 CSV 的“上位机编号”必须一致。同一 source 首次绑定登录账号，其他账号不能抢占。**本期没有 source 账号转移或解绑入口**；必须使用已绑定账号，停用时仅保留离线状态及历史，不能删绑定或换 source_id 绕过冲突。后续需要转移时，应另行实施并审核带审计的迁移/配置升级，不能把它当作当前已具备的管理员操作。

已有 monitor API **不包含 SN**，未配置的机型可能默认显示 24 自由度。因此本桥接仅使用人工核实的绑定文件，绝不从 robot_id 或界面默认值推断 SN/机型。示例 `bindings.json`：

```json
{
  "robots": [
    {"robot_id": "RP1-ROBOT-01", "serial_number": "实际SN", "model": "24轴"},
    {"robot_id": "RP1-ROBOT-02", "serial_number": "另一实际SN", "model": "28轴"}
  ]
}
```

最多 100 个绑定，SN 不得重复。缺 SN 或缺明确 `24轴`/`28轴` 机型的机器人会提示并跳过，不发布为在线。每次为完整 source 快照，平台会将遗漏的既有机器人置为离线，而非删除历史档案。

平台密码通过终端隐藏输入，cookie/CSRF 仅在进程内存中。脚本调用可使用 `--password-stdin` 从标准输入读取密码，不把密码放在命令参数、绑定文件或 outbox。不要把秘密写入 reason、CSV 或测试日志。viewer 文件只需只读权限，使用原有文件，不复制或重置其内容。

桥接使用现有人员会话，其有效期由平台配置决定，当前默认 8 小时。presence 遇到 401 会退出，必须由人员重新登录；本候选**不承诺无人值守几千小时持续在线**，原 Qt 监控和记录不受桥接会话到期影响。不会为长期常驻保存密码、自动改用旧 ingestion token 或扩大账号权限；正式结果仍需逐次明确核实提交。

## 上报机器人状态

在已有后台正常运行时，独立终端从源码目录执行：

```bash
PYTHONPATH=src python3 -m rp1_monitor.factory_bridge \
  --platform-url https://实际平台地址 \
  --source-id PC-FACTORY-01 --username 实际执行员账号 \
  presence --monitor-url http://127.0.0.1:18980 \
  --viewer-token-file /实际状态目录/viewer-token.txt \
  --bindings /实际配置目录/bindings.json \
  --station-name 整机工位一 --source-kind real --interval 10
```

`--once` 仅发送一次。持续上报默认每 10 秒一次，允许 5–30 秒，串行运行，没有累计的并发请求。停止该桥接不会停止原上位机或机器人；平台会在 heartbeat 超过 45 秒后判离线。不要用多份桥接同时对同一 source 发送不同完整快照。

本机读取默认 5 秒、平台请求默认 15 秒预算；每次请求有独立截止监视器，socket 创建后立即登记，多个 A/AAAA 地址的 TCP 连接共用剩余预算，不逐地址重新获得完整超时。监视器关闭实际 HTTP/TLS socket，覆盖连接、响应头、响应体及 TLS 握手，结束/异常路径会取消并 join 监视器，不以 `Future.timeout` 留下失控工作线程。收到慢滴流也不能无限延长已经建立的连接。平台主机名的 DNS 解析仍受操作系统 resolver 限制，Python socket 关闭不能中止 OS 解析；若现场 DNS 长时间阻塞，应修复名称解析/使用经审核的稳定网络配置。monitor 固定 loopback IP 不涉及 DNS。不能将此预算描述为已限制任意操作系统调用的全程硬时限。

在线必须同时满足来源为 `ros2`（模拟源需独立使用 `--source-kind simulated`）、`enabled=true`、`online=true`，且 API 的 `as_of_at` 与接收观测时间均不过期。桥接检查最多 10 秒年龄与有限未来偏差，不将源机器人硬件时钟冒充电脑接收时间。`data_fresh` 与在线分别报告，在线不等于健康/测试合格。

源服务不可达、响应异常、未找到绑定的机器人或时间过期时，发送 `online=false`；不会继续报告旧绿灯。平台也不可达时打印 `delivery_unconfirmed`，不能声称中央已收，中央使用自己的 TTL。401/403、绑定冲突和其他非限流 4xx 停止循环，需核对账号/配置后重新登录，不自动重复使用旧会话。

源地址固定为 HTTP loopback `127.0.0.1`，只读取上述单一路由。单次源响应最多 2 MiB、最多 256 个快照；超限整体按来源不可达处理，而不是截断后继续报绿。大量机器人/完整诊断载荷可能触及该限制，应先核对实际接口体积，再独立审阅更大的预算或专用源接口，不能静默放宽。

平台默认要求 HTTPS（loopback 测试例外）。受控内部 HTTP 部署必须显式加 `--allow-http`；不会禁用 TLS 验证，不跟随重定向，不使用代理环境变量。内部 CA/自签平台应先核对可信证书链，可用 Python 支持的 `SSL_CERT_FILE=/实际可信CA文件.pem` 指向受信 CA，不能关闭证书校验来绕过错误。平台账号 cookie 不会发给本机 monitor，viewer token 也不会发给平台。

## 显式提交已核实的 17 列结果

先使用原生上位机既有“一键保存并导出 CSV”，核对它属于已结束的测试、实际 SN/机型、上位机编号、所有待提交结果和模拟标识。桥接不会代替人完成核实，也不能仅凭文件存在确认真实设备已结束。

```bash
PYTHONPATH=src python3 -m rp1_monitor.factory_bridge \
  --platform-url https://实际平台地址 \
  --source-id PC-FACTORY-01 --username 实际执行员账号 \
  --state-dir /实际私有目录/factory-bridge \
  submit-results /实际导出目录/已核实结果.csv \
  --robot-id RP1-ROBOT-01 --source-kind real \
  --verified --completed --submit --reason 已核对SN机型工位与全部结果且测试已结束
```

三个确认开关缺一即拒绝，且在登录或网络请求前拒绝。`source_kind` 必须显式指定；模拟来源不能改成 real，平台还会核对 CSV 备注中的模拟标识。`robot_id` 必须与人工绑定的 CSV SN 一致；CSV 本身没有 robot_id 列，不应随意填写。平台对同一 `(source_id, robot_id)` 的首次 SN/机型绑定追加身份门禁，后续不同 SN/机型或同 SN 指向另一 robot_id 会拒绝；本期没有静默解绑/换 SN 入口。

本地检查标准 17 列、单文件一个 SN/机型、记录编号非空、所有“上位机编号”与 source 相同，最大 4 MiB、最多 1000 个数据行（不含表头）；其余业务校验由平台再次执行。发送原始文件 bytes，UTF-8 BOM、换行和原值不改写；传输携带大小、SHA256、核实/结束确认及说明。目标固定为 `/api/v1/factory/robot-result-archives`，没有 RESEARCH/domain 切换选项。

账号登录成功后，桥接才将该次显式请求的原始 bytes 和固定元信息封存到独立 SQLite outbox，再尝试传输。首次登录本身失败时不会创建待传任务，源 CSV 原件仍保留，恢复网络后重新明确提交。不会扫描或自动上传其他文件。

收到匹配 UUID、source/robot/SN/source_kind、SHA256/大小、状态与版本的最小回执后，任务才标为 `sent` 并释放 outbox 内的 CSV bytes。`PENDING` 仅表示已收待审核，**不表示测试已批准**。本机原 CSV 从不删除。相同回执丢失后重试使用相同 Idempotency-Key；同记录内容修改需重新核实并显式提交，由服务端追加版本，不能覆写原事实。

## 重试与容量

```bash
PYTHONPATH=src python3 -m rp1_monitor.factory_bridge \
  --platform-url https://实际平台地址 --source-id PC-FACTORY-01 \
  --username 实际执行员账号 --state-dir /实际私有目录/factory-bridge retry

PYTHONPATH=src python3 -m rp1_monitor.factory_bridge \
  --platform-url https://实际平台地址 --source-id PC-FACTORY-01 \
  --state-dir /实际私有目录/factory-bridge status
```

`retry` 只重传已经显式提交的冻结任务，不重新读取已被修改的源文件。默认最多传 10 个任务，一项失败即停止并保留待传；需要时再次执行。pending 的重复显式提交或网络重试复用该待传任务的 Idempotency-Key。任务已经 sent 后，再次执行完整的人工核实/结束/显式提交命令，会创建新的 attempt 与键并真正发往平台，允许驳回后同文件重提；平台仍负责对 PENDING/APPROVED 的相同文件去重。已有任务与审核历史不会被改写。网络失败、会话失效、409 冲突或不匹配回执都不会删除 payload；核对错误后以原账号和原目标重试。不能把不同目标或不同账号的任务挪去发送。

默认状态目录为 `~/.local/share/rp1-monitor/factory-bridge/<source_id>/`。目录要求当前用户所有且 0700，SQLite 文件为 0600；不保存密码、viewer token、cookie、CSRF 或任意服务端错误正文。默认待传载荷 32 MiB、64 项，查重与容量检查在 `BEGIN IMMEDIATE` 中执行；满后拒绝新任务，保留已有项。最多保留 512 个已完成最小回执。此处是桥接载荷预算，不是 SQLite 物理文件/RSS 的完整上限；不得删除数据库来伪造传输成功。

## 本轮验证与交付状态

执行命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s tests -p test_factory_bridge.py -v
```

2026-11-05 最终隔离 Docker 运行：`tests/test_factory_bridge.py` 26 项加原有 `tests/test_result_summary.py` 2 项，**28 passed，无 skip**。覆盖在线/缺 SN/模拟隔离、过期/失联离线、loopback GET、登录 cookie+CSRF、会话失效、重定向、响应上限、显式确认、原始 bytes/hash、1000 行门禁、失败重试、sent 后新的显式重提、并发容量、错误回执保留与敏感值不落盘；本机 TCP 响应头/响应体 slow-drip 均实际注入，并检查总截止预算和线程释放。另验证多地址 TCP 连接共用剩余预算、截止关闭竞争的错误归类。日志位于平台工作树 `outputs/whole-robot-factory-20261105/monitor-bridge-final.log`。子 agent 原沙箱的 bind 限制曾导致两项 skip，最终已由主 agent 的隔离 Docker 实跑补齐，不把 skip 当作通过。

同日另由平台主 agent 执行平台仓库 `scripts/test-robot-factory-bridge.py` 的隔离 TCP/临时数据库联验并通过：实际 `FactoryStore` 导出 31 行 → `PlatformClient` 登录 cookie/CSRF → 原 bytes 提交 PENDING → 独立审核 → 工厂卡片 SYS-012；相同记录 ID 修改内容追加版本，sent 后同文件同说明在被驳回后显式重提会生成新归档，原导出文件和本地证据文件不变，也不写研发执行。TLS 故障验证通过：不可信 CA 拒绝、`SSL_CERT_FILE` 可信 CA 的 HTTPS 成功、慢 TLS 响应体按总预算中断且无截止监视线程遗留。日志为平台工作树 `outputs/whole-robot-factory-20261105/bridge-final-tcp-tls.log`；临时数据库/卷已清理。上述属于隔离软件联验，仍不表示现场上位机安装生效或真实机器人验收。OS DNS 解析的预算边界仍如前述，未被这些 TCP/TLS 测试取消。

未修改原生功能、未安装现场版本、未对现场服务运行桥接、未推送或发布。变更文件为 `src/rp1_monitor/factory_bridge.py`、`tests/test_factory_bridge.py`、`pyproject.toml`（独立 CLI entry point）、`README.md`（文档入口）及本文；后续合并前应一并复核平台 API/迁移及验证记录。
