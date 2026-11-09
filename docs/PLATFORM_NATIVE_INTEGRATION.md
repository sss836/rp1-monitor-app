# 原生整机工作台接入可靠性平台

本文说明本地原生平台接入实现，依据 `desktop_platform.py`、`platform_client.py`、`platform_workspace.py`、`recording_export.py` 与现有 `factory_bridge.py`。它不是生产部署、安装生效或真实设备验收记录；最终构建版本、验证数量和现场验收结果以本批交付报告为准。

## 1. 范围与数据边界

原生入口在原 `AppMonitorWindow` 外增加顶部可选平台登录栏和研发/工厂两个数据工作区。原机器人采集、SSH、控制、监控记录、工厂本地结果及其保存方式继续由原模块负责。平台账号没有被转换成机器人 SSH 账号，也不替代设备维护请求的既有认证。

本次接入分开处理三种事实：

| 对象 | 本地来源 | 平台用途 |
| --- | --- | --- |
| 监控实时状态 | 原本机监控和机器人遥测 | 原监控逻辑；本次档案送审不会自动产生实时在线状态 |
| 研发原始记录 | 本机 PostgreSQL 已保存的监控记录及其逐点样本 | 独立精确导出，经人员核实后按 `RESEARCH` / `HISTORY` 归档、送审 |
| 工厂整机结果 | 原 `FactoryStore` 导出的17列结果 CSV | 专用工厂结果归档、独立审核；不是原始采样曲线或 MTBF 暴露事实 |

这里的“原点”指本机监控记录表中实际保存的采样点。它保留本机记录时选择的指标、频率和数据质量，不是机器人 CAN 总线的原始逐帧数据，也不是把已有聚合曲线恢复成原始采样。本次导出不改变原采集和保存频率。

## 2. 账号、权限与本地工作

平台必须使用受信任的 HTTPS 地址。原生登录使用现有人会话：`/api/v1/auth/login`，后续请求使用内存 Cookie 与 CSRF。密码、Cookie 和 CSRF 值不写入本地任务数据库、导出清单或浏览器链接。

| 平台角色 | 本次原生界面行为 |
| --- | --- |
| `SYSTEM_ADMIN`、`TEST_EXECUTOR` | 可核实并提交工厂结果，可读取本机研发源和送审研发档案；正式上下文权限仍由平台校验 |
| `FACTORY_REVIEWER` | 工厂分区查看、按服务端 `can_review` 审核；不能导出本机研发源或上传结果 |
| `VIEWER` | 按平台权限查看档案；没有上传、审核或本机研发源导出权限 |

以上为本机操作门禁。平台仍重新检查账号角色、数据可见范围、档案所有者、上下文和审批状态，界面按钮不是权限权威。平台允许范围内的执行员/管理员自审沿用平台规则，不把“账号已获准”解释成“测试数据已批准”。

启动直接显示完整本地工作台，平台登录可选。`--factory` 打开原本地工厂页；`--quick-connect` 按原流程启动或复用本机station、进入已有连接恢复与SSH设置。平台登录不是本地连接、测试、控制、保存或查看历史的前提；这些功能继续使用原设备认证、状态检查和操作条件，不会自动启动新测试或电机动作。

本地初始流程与平台会话分开，仅启动一次。以后登录、改密、退出或会话到期都不重复初始化station、不重置已有本地连接。顶部账号栏常驻，主工作区不被独立登录页面阻挡；只有平台上传、平台记录、审批等操作需要有效会话和相应角色。

首次密码必须修改的账号仅对平台功能要求完成改密；本地完整功能仍可继续使用。会话到期由平台返回的 `expires_at` 决定，平台现有默认通常为8小时，可由平台配置调整。到期或401后，需要人员重新登录，不承诺无人值守连续数千小时的平台会话。本次没有保存密码、自动续登、使用旧 ingestion token 或扩大账号权限。

**退出或平台会话失效不会调用原采集断开、停止记录或设备控制停止函数。** 任何实例都可直接进入“本地监控与工具”，包含首次未登录的启动。平台任务可能暂停或取消，平台上传须重新登录；本地工具和设备控制继续遵守原设备认证、状态检查和操作条件。关闭整个应用仍执行原本地关闭链路，不能把平台退出与应用退出混为一谈。

平台页面和本地工具分别导航；进入原工厂本地页不会自动把数据归为工厂平台数据。只有明确选择的两域提交动作才确定平台数据域。

## 3. HTTPS、CA 与打开大屏

在顶部账号栏填写账号和密码并明确点击登录；平台HTTPS地址和CA位于“连接设置”，不要求每次操作本地功能先登录。地址不得带用户名、密码、查询参数或片段；不提供“跳过证书校验”。内网自签体系应选择实际可信 CA 的 PEM/CRT 文件，或使用受信任的系统证书配置。环境变量 `RP1_PLATFORM_URL`、`RP1_PLATFORM_CA` 可提供界面初值；不得把密码或 Cookie 放入这些配置。

“打开大屏”“查看记录”“查看曲线”调用系统浏览器，链接仅包含业务身份：

- 工厂结果：`?view=factory&kind=whole&domain=FACTORY&factory_archive=<UUID>`。
- 研发档案：`?view=archive&kind=whole&domain=RESEARCH&id=<UUID>`。
- 研发曲线：`?view=curves&kind=whole&domain=RESEARCH&id=<UUID>`。

浏览器与原生界面各自登录。不会把原生 Cookie、CSRF、密码或设备凭据拼入链接。工厂17列汇总没有原始采样曲线，界面不为它提供研发曲线入口。

## 4. 研发源：只读本机 PostgreSQL

### 4.1 选择源连接文件

研发区显式选择已有本机 PostgreSQL 私有连接文件，例如 `postgres-url.txt`。文件由当前操作系统用户所有，权限应为0600；拒绝符号链接、非普通文件和其他用户可读写文件。内容为该本机数据库的 PostgreSQL URL，只在发起读取时读取，不复制到平台任务、导出清单或其他配置中。

推荐使用只具有所需 `monitor_recordings`、`monitor_recording_samples` 读取权限的数据库账号。本模块另外以 `REPEATABLE READ` 和 `READ ONLY` 事务读取。它不实例化原 `Store` 或后端应用，不调用建表、迁移或采集器，也不会修改原记录、停止记录或删除样本。

“读取已结束记录”只选择 `source_kind=ros2` 且 `ended_at` 非空的候选；界面最多读取100条，导出器上限200条。记录结束与数据库读取一致性不等于硬件测试完成、通过或正式成果封存。实际测试是否结束须由人员另行核实。

### 4.2 精确逐点导出

选择候选后执行“导出精确原点”。导出在一个只读一致快照内读取记录元数据、样本数、最大序号和按原序号排列的样本。SQLAlchemy 使用流式游标，`max_row_buffer=256`；不调用原 `/curves` 聚合接口，也不把全部 CSV 放入内存。

导出 CSV 格式为：

```text
time_s,sample,rp1_recording_json,rp1_sample_json,<原recording.metrics顺序的指标列>
```

- `rp1_recording_json` 首个数据行保存 `rp1-monitor-recording-v1` 元数据；其后为空。包括原记录ID、机器人ID、来源、原起止、指标、`source_context`、样本数和最大序号，以及实际存在的记录标签。
- `rp1_sample_json` 每行保存原 `sequence`、`observed_at`、完整 `payload` 与 `payload_hash`。原时间依据、缺失原因、质量字段和显式目录单位全部保留，不按指标名称猜单位。
- `sample` 是原正整数采样序号，允许严格递增跳号；缺号只计数，不插点或补值。
- `time_s` 相对原记录 `started_at`，首个样本可能大于0；不会把首点强行移到0。
- 指标缺失或 `None` 输出空单元格，不填0。数值必须有限，拒绝布尔值和绝对值大于 `1e100` 的值。
- 每个 payload 按原保存方式 `json.dumps(sort_keys=True,separators=(',',':'),allow_nan=False)`、默认 `ensure_ascii=True` 重新计算 SHA256，并与数据库记录逐点核对。哈希冲突、时间不一致或非递增时间会拒绝整个新导出，不修补原事实。
- 正常来源须为 `ros2`。原采集保存的真实断线空点可保留：`source_kind=None`、`connected=False` 且值全为空；模拟样本不能被改标成实机数据。

导出先写私有临时目录，完成后 fsync 并原子改名为新的证据目录。`raw.csv` 与 `manifest.json` 为0600，目录为0700。manifest 保存文件 SHA256、字节数、原点数量、最大序号、缺号数量、来源证据摘要和原记录元数据。不完整临时副本在失败或取消时清除；原 PG 与已完成导出不自动删除。

### 4.3 大文件、磁盘与等待预算

`NativePlatformService` 构造参数可以配置预算，当前默认：

| 预算 | 默认 |
| --- | --- |
| 单个研究 CSV `max_export_bytes` | 50 GiB，与平台协议单文件上限匹配 |
| 未完成研究任务逻辑文件量 `max_pending_bytes` | 64 GiB、最多128条任务 |
| 导出证据目录 `max_export_directory_bytes` | 64 GiB，包含保留的已完成证据 |
| 写入前磁盘空闲预留 `min_free_bytes` | 512 MiB |
| recording/sample JSON | 各不超过256 KiB |
| 单个 CSV 行 | 不超过2 MiB |

目录配额检查和导出用私有 SQLite 写锁串行，超额明确失败，不通过删除旧证据腾空间。上述是本接入目录和待传文件的预算，不是整台电脑或 PostgreSQL 的全局磁盘限制。已有原 PG、本地工厂目录、任务历史与其他应用的磁盘占用需另行管理；外部进程也可能同时消耗空间。

源 PG 连接使用 `connect_timeout=5`，每个 SQL 使用 `statement_timeout=5000ms`。大历史的统计或查询超过预算时会失败，原数据保留，不能因此换成聚合数据冒充原点。取消在样本边界检查；已执行中的 PG SQL 依该超时返回，不宣称本机取消按钮已主动终止数据库 SQL。连接中的操作系统 DNS 解析不由 Python 保证严格总时限。

## 5. 研发人员核实与送审

新版研发页分为“选择原始记录 → 关联用例与计划 → 核实并送审”；数据库连接与执行UUID收进折叠详情，时间使用北京时间选择控件，有效时长使用小时／分钟／秒（秒可含小数），全部人工事实初始保持未知，不自动填写0或通过。平台研发档案详情分开显示原采集事实、人工声明和解析质量，技术原值在次级页签查看。

先读取平台目录和已有整机研发计划上下文。界面只使用平台实际返回的已发布 SYS 用例与真实上下文，不生成替代 UUID，不把“能编辑计划”当作“能使用已有上下文送审”。已有上下文的原发布版本保留，不偷偷换成目录的最新版本。

必须核对并冻结全部7项身份：

```text
campaign_id, cycle_id, segment_id, asset_id,
configuration_id, test_case_version_id, station_id
```

人工核实工位须与所选上下文工位一致。实际工位变化，应先在平台配置并选择对应真实上下文；本机不自动替换或猜测身份。

人员另行填写实际对象、测试名称、实际测试人、结论 `passed/failed/blocked`、核实备注、带时区的实际测试起止、有效运行秒数、完成循环与异常次数。界面不默认填写时间差、0或通过；明确核实后的0可以作为声明。监控总时长不能直接成为 MTBF 有效暴露时长。测试人员文字是人工声明，实际提交账号来自平台会话，二者不互相冒充。

测试人最多100字、核实备注最多1000字；需要中央保留的实际测试依据应写入核实备注。本地任务说明最多300字，保存在冻结任务中；当前平台 HISTORY 送审 SQL 使用固定中央 reason，不能承诺该自定义任务说明原样成为中央审计 reason。

只有同时勾选“已核对原点和身份”“实际测试已结束”并显式提交后，才发送原 CSV。通路为：

```text
本机私有精确CSV
  → POST /api/v1/archive-imports（RESEARCH / HISTORY，原采集事实未知）
  → PUT /api/v1/archives/{id}/content?offset=...（每块≤4MiB）
  → POST /api/v1/archives/{id}/complete
  → GET /api/v1/archives/{id} 等待真实 READY
  → POST /api/v1/archives/{id}/verify-and-submit
  → PENDING → 平台独立审批 → APPROVED 或 REJECTED
```

HISTORY 接入不把观测区间伪装成设备有效运行、完成循环或机器完成状态；未知原采集事实保留未知，人工申报以独立核实版本记录。READY仅表示文件校验/解析就绪，`sent` 仅表示传输/送审回执确认，PENDING/REJECTED不能进入正式统计。批准后是否进入正式可靠性查询，仍由平台正式上下文、证据、指标和统计规则确定，不等于本机自动完成了 MTBF 计算或硬件验收。

## 6. 工厂17列结果提交与审核

新版日常操作分为三个短步骤，平台记录和待传任务在独立页签：

1. **选择机器人与结果**：本地工作台导出后自动读取预览，也可手选原CSV。SN、机型、上位机编号和测试人员从文件只读带入，不重复填写。采集机器人ID使用真实连接或经人员核实的映射，来源不从SN或当前连接猜测。
2. **核对测试项目**：按SYS 12个目录项目定位，查看对应原父项／子项、实测结果、通过标准、备注和记录身份。无需先填采集映射即可预览；“未测试”“本文件未包含”“仅子项／父项缺失”“不适用”分别显示，不合成通过结论。
3. **确认提交**：进入此步及提交时才强制核对采集映射／来源，人员明确勾选核实和测试结束并说明依据；提交前重核预览SHA，文件变化即停止，不入队、不发送。

已上传的工厂详情沿用按项目查看，完整原17列在次级页签按需构建。此CSV没有独立原始证据列；页面明确原证据仍需在本地工作台核对，不展示编造的已上传证据链接。

继续在原本地工厂流程填写和保存结果，再由原导出生成17列 CSV。导出信号只把该文件设为“工厂候选”，不自动上传。也可显式选择其他已核实的本地原17列文件；不会扫描目录并自动提交。

`factory_preview` 在登录且具有工厂提交权限后仅读取指定本地文件，不发平台HTTP、不创建任务、不修改结果。它自动读出CSV已有的上位机编号、SN、机型和测试人员；不让人员重复猜填这些已有身份。机器人 `robot_id` 不在原17列中，仍须按当前真实连接或人员明确确认的映射选择，不能从SN猜测。

预览依据原 `data/factory-report-rules.json` 展示12个SYS项目的目录标题，以父项/子项行索引分组。CSV实际测试名称、实际结果、标准、人员、备注和结论逐字保留；目录标题不能覆盖人工编辑后的实际名称。31行主/子项不等于31个项目；缺失、未测试、待复核、不通过和不适用分别保留，不默认通过。没有模拟标识时来源仅为unknown，不能据此自动声明real。

原17列没有独立evidence列。预览明确返回 `evidence=None/evidence_available=False`，不把备注推断成已归档原始证据，也不读取或上传备注中提及的附件路径。原证据仍在原本地保存目录。

预览同时冻结文件SHA。原生提交使用 `expected_sha256`，再次读取实际bytes并校验，文件变化就在创建Outbox或发送HTTP前拒绝，要求重新预览核实；不会仅凭旧预览放行新的内容。独立旧CLI的显式提交方式保持兼容。

核对文件、上位机 `source_id`、实际 `robot_id`、真实/模拟来源和说明，确认本次测试结束后显式提交。当前专用接口为：

```text
POST /api/v1/factory/robot-result-archives?...&verified=true&completed=true
Content-Type: text/csv
Idempotency-Key: <冻结任务键>
```

它发送原 CSV bytes；单文件最多4 MiB、最多1000条数据行、每字段最多65536字符。单个文件必须是一个SN/机型，所有上位机编号须与本次 `source_id` 一致。SN/机型取原文件，不从实时机器人ID或昵称猜测。模拟来源必须明确标记，不能批准为实机工厂成果。

原文件不改写、不删除。服务器以来源、记录ID和内容保存行版本；同一记录ID的更正是追加内容版本，不覆盖旧证据。上传后的PENDING与之后的独立APPROVE/REJECT审核相分离；审核需有权限且服务端返回 `can_review`。审核使用冻结的状态版本，冲突时须刷新核对，不能强行覆盖他人的审核。

工厂汇总批准仍是工厂结果批准，不是原始采样、有效小时或可靠性/MTBF统计资格。需要原曲线时回本地原监控记录或按独立研发证据流程处理，不能用17列汇总代替原始测量。

`source_id` 首次使用会绑定平台账号；同一 `(source_id,robot_id)` 的SN与机型也受服务端身份约束，不能静默换机或换账号抢占。当前没有本机或平台 source 转移/解绑入口。后续上传使用原绑定账号；停用设备保留离线状态与历史，不通过删除绑定或随意换源ID规避身份约束。正式迁移需要后续独立设计并记录审计。

## 7. 冻结任务、重试与失败恢复

启动入口将平台状态放在 station 状态目录的同级目录，例如 `runtime/station-platform`，避免污染原 station 的标记/初始化目录。底层服务也允许显式给定其他私有目录。

- 研发：`research-tasks.sqlite` 只保存冻结元数据和外部证据文件引用，不把大 CSV 存成SQLite BLOB。记录平台地址、账号所有者、固定RESEARCH域、原 recording ID、文件SHA/大小、7UUID、人工核实、幂等键与已绑定归档身份。
- 工厂：沿用 `factory/<owner UUID>/<source_id>/outbox.sqlite`。每个来源队列最多64个待传/32 MiB，单CSV最多4 MiB；确认有效回执后清除待传BLOB，保留任务事实和回执。原导出文件仍保留。
- 登录密码、Cookie、CSRF 与 PG 连接凭据不进入上述持久任务。换平台地址或换账号后不能接管旧账号/旧目的地的任务；原账号重新登录后才恢复对应待传任务。

重试先核对外部证据文件大小和完整SHA，变化就停止。研究上传先读取服务器实际 `received_bytes`，从该 offset 继续，单块最大4 MiB。服务器已收块但回执丢失时，不依据本机旧进度重复造归档。首次有效回执冻结 `archive_id` 与 `execution_uuid`；后续GET、块、complete及送审回执均同时核对身份、SHA、大小、数据域、来源和所有者。

研究首次import、每块、complete、intake各用稳定任务键。送审前先持久冻结真实归档修订号与 submission 修订号；回执丢失时查询既有送审状态，不自动制造新版本。服务端按SHA复用已PENDING/APPROVED档案时，本机必须核对原有7UUID及人工核实语义；更换用例/资产或人工声明会明确报 `archive_context_conflict` / `archive_verification_conflict`，保留任务，不重标旧批准成果。

工厂同一待传请求的网络重试复用原键；已经送达后人员再次显式提交是新的尝试，用于驳回后重提，服务器仍负责相同内容的去重。审核幂等键由账号、归档ID、预期版本、决定及说明共同决定，回执丢失重试同一决定不会随机更换键。

平台HTTP单次总预算默认15秒，覆盖可受控连接、TLS、响应头/体；超时或取消会中断跟踪socket，并回收deadline计时器，不用遗留后台Future伪装超时。操作系统DNS解析是预算例外。等待READY的轮询窗口为45秒，窗口结束时正在进行的单次HTTP仍受其自身预算；尚未READY会保留 `validating` 任务，人员稍后重试。取消不代表服务器撤回，已经送达的提交须刷新台账核对。

通用/旧bridge HTTP响应payload默认仍限2 MiB；原生平台client使用有界64 MiB，超限多读1字节即拒绝。原因是合法4 MiB工厂CSV在detail中含原单元格及重复的结构字段，JSON控制字符转义可能放大，32 MiB不能覆盖该合法范围。64 MiB是响应原始payload上限，**不是全进程RAM或RSS上限**；解码后的字符串、JSON对象、Qt对象和原本地模块还会使用额外内存。大结果应按项目选择详情并截断列表摘要，避免默认展开全部长文本。

原生UI同时只运行一个平台/本地历史后台任务，冻结账号代次、来源、分区和参数；完成前不能换账号或重认领任务。到期、来源变化或取消后的旧回调不再展示为当前身份数据。关闭窗口先等待该任务结束，再进入原本地关闭链路，不强杀线程。原设备本地读写仍由原模块的生命周期负责。

任务 `pending/uploading/validating/sent` 与服务端 `READY/PENDING/APPROVED/REJECTED` 分别表示传输、解析和审批，不能互换含义。持久任务和证据不自动清理；达到预算后应先核对已有任务、存储位置与备份，不删除原PG或修改冻结文件来强行重试。

## 8. 实时机器人卡片与 presence

本次原生登录和档案提交没有自动启动旧 `rp1-factory-bridge presence` 常驻线程，也不自动生成或续绿机器人在线状态。大屏在线卡片仍需要真实、持续的presence来源；平台TTL会让失联来源转为离线。

原独立bridge入口继续可用，按其独立说明配置只读本机monitor API、显式机器人SN/机型绑定、平台账号和固定工厂域。不要把“文件已送达”解释为“机器人当前在线”，也不要为了保持绿灯伪造时间、SN或来源。平台会话到期后需重新登录，原Qt监控与本地保存不受影响。

## 9. 文件级接线与维护

| 文件 | 类/函数与连接 |
| --- | --- |
| `src/rp1_monitor/desktop.py` | `_run_desktop()` 构造 `PlatformMonitorWindow`，直接按原args执行一次本地初始连接，平台登录不参与；退出保留原worker收尾链路 |
| `src/rp1_monitor/desktop_platform_details.py`、`desktop_research_archive_view.py` | 只读工厂项目／子项详情与研发人类可读核实信息；原始列／技术身份延迟展示，审批仍由外层处理 |
| `src/rp1_monitor/desktop_diagnostics.py`、`desktop.py` | 诊断日志使用同级 `<station-dir>-desktop-diagnostics`，不占用首次station所有权检查目录；不接管或删除旧目录 |
| `src/rp1_monitor/desktop_platform.py` | `PlatformMonitorWindow` 复用原10个本地页面；`PlatformJob` 串行后台任务，`request()` 冻结参数/代次；`platform_state_path()`、`platform_link()` 分别确定私有状态和无凭据深链 |
| `src/rp1_monitor/platform_client.py` | `PlatformSessionClient` 内存登录/改密/profile/logout/CSRF；401失效与真实403错误区分；`NativePlatformError` 提供安全错误码和session快照 |
| `src/rp1_monitor/platform_workspace.py` | `NativePlatformService.operation()` 统一同步动作；`_enqueue_research()` 冻结任务，`_process_research()` 分块/READY/送审，`_check_archive()`、`_check_submission_evidence()` 严核身份和去重复用；`_retry()` 返回统一任务DTO |
| `src/rp1_monitor/recording_export.py` | `ResearchRecordingExporter.recordings()/export()` 独立只读PG连接；`export_rows()` 精确流式编码、逐点hash、配额与原子目录；不调用原保存模块写方法 |
| `src/rp1_monitor/factory_bridge.py` | 复用 `result_upload()`、`Outbox` 和严格工厂回执验证；运输层补可信CA、取消及有界错误码，不改变原机器人/监控保存 |
| `src/rp1_monitor/factory_preview.py` | `preview_result_csv()` 复用有界原CSV读取，按既有12项目定义组织只读预览，保持原结论与字段，不上传、不回写、不猜来源或证据 |
| `tests/test_platform_native.py` | 离线人会话、哈希/空点/跳号、预算、owner、回执双身份、SHA上下文/核实冲突、幂等审核与续传故障注入 |
| `tests/test_desktop_platform.py` | mock平台/设备，验证首次登录、改密、到期不强停本地、权限、参数冻结、取消、晚回调与关闭收尾 |
| `tests/test_factory_preview.py` | 原31行导出映射、父子项/结论分别保留、无证据列/来源未知、非法输入与预览后内容变化拒绝 |

公共引擎接口是 `NativePlatformService(state_dir).operation(action,payload)`，每个成功返回和安全错误均携带session快照。`cancel_current()`、`close()` 供UI跨线程请求取消和收尾；不并发共享未串行的会话client。

`tasks` 返回 `items` 为扁平任务DTO；`retry` 同样返回最新 `items`，额外 `results` 是本次执行回执。研发submit返回 `{task,archive,session}`，工厂submit返回 `{task,receipt,session}`，传输回执不替代审批状态。

## 10. 验证与现场交付边界

本地自动验证使用临时目录、mock设备与合成证据；跨仓验证使用可销毁的隔离HTTP/PG及平台worker，不触及真实采集。测试结果、构建校验和版本号在本批最终交付报告中补充。

需要分别确认代码回归、隔离协议联验、安装后实际运行版本、长期稳定性与真实设备验收，不能将其中任何一项代替其他阶段。本说明不宣称已安装到用户当前实例、已部署生产、已启动机器人或已完成实机MTBF测试。

初次平台适配以本地分支交付，后续纳入 [0.4.3 发布](RELEASE_0.4.3.md)。现场更换实例仍须遵循原设备停止／失能、正常退出及版本核对要求。
