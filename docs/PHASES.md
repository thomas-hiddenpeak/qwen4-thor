# PHASES.md — 分阶段计划

## 2026-11-08：自动恢复 A/B 已有界交付

[结果与交付](SERVE_SELF_RECOVERY_RESULT_2026-11-08.md)：177 项不同 CPU，
累计 5 模型/21 Chat，最后补验严格租约通过。独立 Guardian、持久预算、
模型/控制器恢复和网关重绑已接通；新显式入口安装为停止状态，原生
默认保持。C/D 的局部阻塞/进度/留存、可选资源与部署范围继续独立，
长稳由用户执行。本阶段分支 `codex/serve-self-recovery`；以下旧队列
入口和当时状态为历史过程，最新结果以 STATUS 与本项交付为准。

## 当前任务：Serve 接入与运行保障（2026-11-02）

用户在成熟参考系统对照后要求配置任务。以下是持续维护的执行队列；
任务描述不替代具体接受结果，当前发布身份与运行状态仍只以
[STATUS](STATUS.md)为准。任务来源为
[全链路审计](SERVE_COMPLETENESS_AUDIT_2026-11-02.md)的剩余 G 项与
[能力对照](VLLM_SERVICE_GAPS_2026-11-02.md)，已交付的输入合同、
工具控制及编译隔离不重复立项。

执行入口继续使用 `codex/main-reaudit-fixes`，以提交划分子任务。
用户随后要求推进目标，阶段 A 已于2026-11-03按限定范围交付；
[结果、失败与未证明项](SERVE_STAGE_A_RESULT_2026-11-03.md)。B1（SRV-04）也已于2026-11-03限定交付，
[结果与证据](SERVE_STAGE_B1_RESULT_2026-11-03.md)；
C项按明确业务条件单独冻结。
任务编号表示排期，不是故障严重程度。

### 队列与依赖

2026-11-05后续用户授权[有界交付](SERVE_BOUNDED_DELIVERY_2026-11-05.md)：
PR #10已合入main；[部署profile已交付有限结论](SERVE_BOUNDED_DEPLOYMENT_RESULT_2026-11-05.md)，
4响应/2工具通过；额外启动因压力安全余量停止，完整退场组合NO_GO，
两轮原件保留、预算关闭，c1653470未接受。下一项独立补单进程RPM/TPM
及客户端公平准入，尚未开始。长稳仍由用户执行，实际
LAN/通知按需启用；[物理RAM硬上限](SERVE_PHYSICAL_RAM_DECISION_2026-11-05.md)
本轮明确NO_GO_CURRENT_CONTRACT，保留覆盖限制，不无限追加研究。
以下“准备PR/不扩展”是此前有限收尾范围，不替代本轮授权。

2026-11-05用户授权推进能收尾的工作；[三项有限收尾](SERVE_CLOSEOUT_HANDOFF_2026-11-05.md)
已完成：SRV-08b客户端、当前默认托管组合及持久任务协议/来源交接。
本工作分支准备独立PR，不再以长稳或可选部署阻塞后续开发。
长期工业化不自动扩展；长稳由用户执行，正式部署、公平策略与整机
物理RAM保护分别保留条件待办。此前“暂停/未验”记录保留为历史。

| 编号 | 阶段 / 任务 | 状态 | 前置与完成标志 |
|---|---|---|---|
| 有界部署 | 离线profile及当前资源组合 | [工具可用；退场组合NO_GO](SERVE_BOUNDED_DEPLOYMENT_RESULT_2026-11-05.md) | 4完整响应/2工具；补验安全停止、2截断流不接受；169原件独立核对，GPU释放，不追加模型 |
| SRV-08b | 客户端总截止/可选Key、CA、IP | [限定接受](SERVE_CLIENT_TRANSPORT_RESULT_2026-11-05.md) | 83 Python/40 Node、34网络场景、6响应/2工具；无硬实时或全SDK承诺 |
| 当前托管 | 默认a56bd448的v6包 | [限定接受](SERVE_CURRENT_MANAGED_RESULT_2026-11-05.md) | 包41dc8fdb、正常CLI/两代TLS/4响应2工具/满额运维；旧包保留、无常驻安装 |
| 当前任务 | 15d7ccb1→cc321239及17→18来源 | [限定接受](SERVE_CURRENT_TASK_RESULT_2026-11-05.md) | 两应用保存工具后中断、显式迁移只续最终答案；未知不重放，方向与来源精确绑定 |
| SRV-20 | 当前合同与证据入口复核 | 静态核对完成 | [结果](SERVE_CONTRACT_REAUDIT_2026-11-04.md)；不新增运行时/业务接受，不宣称全层无遗漏 |
| SRV-03c | 准备阶段等待可取消 | 已限定交付8e9b4ce7 | [结果](SERVE_PREPARE_LOCK_RESULT_2026-11-04.md)；294公共host/260必需/9结构化、16原生复用+7补验；1模型6尝试2工具，默认已换/完整回退保留；协作等待不扩大为GPU硬抢占 |
| SRV-01 | A：统一请求身份与错误定位 | 已交付（限定范围） | 无；成功、拒绝和 SSE 错误可按同一关联身份定位 |
| SRV-01c | 工具与结构化输入错误位置 | [已限定交付49cc78cd](SERVE_ERROR_PARAMS_RESULT_2026-11-04.md) | 287公共/253必需/9结构化、91差分/4驱动；1模型1TLS双Key36拒绝/6成功/2真实工具，355记录重建；GPU54.677秒释放，旧pair保留。文档摘要及离线收尾原失败保留，无追加推理 |
| SRV-01b | 接收拒绝与首次输出失败观测 | 已限定交付；ed672a65 | 实际8生成尝试/16拒绝/2启动，接收15/写3首次原因与指标日志对账，恢复及真实工具通过 |
| SRV-02 | A：就绪 / 排空与控制入口保障 | 已交付（限定范围） | 01；负载达到上限时，健康与取消仍有有界服务能力 |
| SRV-03 | A：HTTP / SSE 合同、心跳与慢读边界 | 已交付（限定范围） | 01、02；代理和慢客户端下终态、取消、资源回收符合合同 |
| SRV-03b | A回补：严格长payload实际背压 | 原轮失败；独立复制候选限定通过 | 6生成/4负例/2启动，固定12443字节的真实EAGAIN取消/写截止/恢复通过；原失败不追认 |
| SRV-02b | 请求准入与容量适配排队 | 已限定交付；c16caf05 | 实际10生成尝试/4拒绝/2启动、总准入/容量FIFO/取消/真实工具通过，不能代替03b |
| SRV-02c | 全局内存观测与提前准入 | 已限定交付；84d16983 | 实际5生成/4拒绝/2启动及1预检，高floor拒绝与默认业务通过；动态恢复后由02d补证，物理硬限额待办 |
| SRV-02d | 同模型压力恢复与持续混合业务 | 已限定接受；[结果](SERVE_PRESSURE_RESULT_2026-11-03.md) | 实际326生成/4负例/1启动及65预检；同PID压力恢复、64轮混合/真实工具与资源包络通过；不代替整机硬限额或长稳 |
| SRV-04 | B：消息角色与模板兼容 | B1已交付（单一指令槽profile） | developer替代开头system；混用/name拒绝；原生字节/ID与真实工具闭环已验 |
| SRV-05 | B：推理字段、输出隐藏与推理预算 | 05a/05b均已限定交付；31ffc41e | 04；字段适配、输出隐藏、计算预算分别证明 |
| SRV-06 | B：文本停止条件 | 已限定交付；a3fd64a3 | 04、05；跨 token / 跨块停止不泄漏尾部、不产生伪完整工具调用 |
| SRV-07 | B：token / 模板预检与有效配置发现 | 限定交付，见SRV-07结果 | 04、06；预检与真实请求共用模板、分词和预算逻辑 |
| SRV-08 | 贯穿 A/B：机器协议与客户端矩阵 | 完整输入/JS SDK限定交付，见SRV-08结果 | 38字段/10路由加原生语义；Node24.18.0/openai7.47.0真实工具/失败/显式答案恢复；非全框架兼容 |
| SRV-09 | C：结构化输出范围与组合扩展 | 条件待办 | B 通过且出现具体 schema / 组合需求；一次只扩一个受控范围 |
| SRV-10 | C：网关与多人资源策略 | [10a本机TLS/双客户端短验限定通过](SERVE_GATEWAY_RESULT_2026-11-04.md) | 原取消错接失败保留，OpenAPI交叉核对/定向取消恢复已验；实际LAN证书/防火墙/访问与长期配额待部署验收 |
| SRV-10b | 网关托管、依赖身份与有界日志 | [短验限定通过](SERVE_GATEWAY_MANAGED_RESULT_2026-11-04.md) | 18有效CPU/4驱动/3隔离入口；1模型/4网关/8生成，6成功含真实工具/2预期断流，排空与3类异常退场通过；失联终态不伪造，GPU/原锁释放；实际LAN/通知/跨代次留存待部署 |
| SRV-10c | 客户端状态隔离与请求预算 | [已限定交付7619966e](SERVE_CLIENT_RESULT_2026-11-04.md) | 280公共/246必需/9后端/28 SSD/70包与3新handoff合同；27 HTTP合同执行29次、4启动/18成功/2取消/6拒绝、真实工具与私有/共享cache隔离；原链接/429/漏seed/相对guard拒绝保持，默认pair及回退包已交付；实际LAN、物理RAM/公平调度和用户长验另验；托管组合见10d |
| SRV-10d | 客户端策略托管闭环与长验交接 | [本机短验限定接受](SERVE_CLIENT_MANAGED_RESULT_2026-11-04.md) | 38有效CPU；1模型/2网关/12成功/2真实工具，SSE排空/重新接入与错策略拒绝，原租约78.713秒；328原件/4 ELF身份封存，新day同包只准备；实际LAN与用户长稳待验 |
| SRV-10e | TLS材料与证书有效期生命周期 | [限定接受](SERVE_TLS_LIFECYCLE_RESULT_2026-11-04.md) | 61 CPU/4有效驱动；1模型2网关同CA不同leaf、4成功2工具，后端身份保持；281原件封存、GPU62.235秒释放；保留驱动断言/离线身份比较错误，TLS告警接线见10f、实际LAN仍待 |
| SRV-10f | TLS状态采集、到期告警与通知接线 | [限定接受](SERVE_TLS_OBSERVATION_RESULT_2026-11-04.md) | 57 CPU/4驱动/29官方时序；新host v2/notify v3独立依赖与早期回收、只读固定代次/材料/新鲜度；1模型1网关4响应2工具，临期/Stale/恢复/终态本机HTTPS确认；404原件封存、GPU145.758秒释放，实际LAN/正式渠道/长验仍待 |
| SRV-10g | 显式客户端来源网段 | [限定接受](SERVE_GATEWAY_SOURCES_RESULT_2026-11-04.md) | 78有效CPU含6驱动/3日志回归；1模型1网关、21探针含16来源拒绝、4响应2真实工具；日志收尾误断言原guard COMMAND_FAILED保留，558原件离线重建，GPU54.664秒释放；实际LAN另验 |
| SRV-11 | C：应用任务恢复与幂等边界 | [真实恢复限定接受](SERVE_PROJECT_TASKS_RESULT_2026-11-04.md) | 20有效CPU/2驱动；修正后3应用SIGKILL/3任务完成，7请求含1断流、4工具尝试/3结果，重复submit零执行；原EOF失败保持；输出预算/迁移/归档保键分别见11b/11c/11d |
| SRV-11g | 持久任务CA交接与连接材料绑定 | [限定接受](SERVE_TASK_TRUST_RESULT_2026-11-04.md) | 124 CPU/4驱动/27文件SIGKILL；1模型2不同CA网关、2旧应用保存工具后停止、新版只续2最终回答，共4响应2工具；旧CA拒绝/归档保键/重复零执行，394原件重建、GPU69.817秒释放；两处离线报告错误保留并修正 |
| SRV-11h | 已审核协议升级与任务交接 | [限定接受](SERVE_TASK_PROTOCOL_RESULT_2026-11-04.md) | 140有效应用/5最终驱动/31文件SIGKILL；修正轮同锁2版本4响应2工具，原配置路径失败0业务保留；656原件重建、两轮GPU131.078秒释放，未登记对仍拒绝 |
| SRV-11i | 当前运维协议版本任务交接 | [限定接受](SERVE_TASK_OPERATIONS_RESULT_2026-11-04.md) | fc63→dd/11h来源登记；23 CPU+2入口、4事务SIGKILL，同锁2模型4成功2真实工具/重复零执行；原导入/入口/封存错误保持后离线接受，278记录、GPU94.053秒释放，默认pair/day保持 |
| SRV-11j | 持久任务全程网络截止时间 | [限定交付](SERVE_TASK_DEADLINE_RESULT_2026-11-04.md) | 16有效CPU+2入口+2探针修正，12完整TLS/1模型4响应2工具及恢复通过；旧慢头1.507秒、新版约0.251秒；原夹具/重绑/封存错误保留，249原件、累计GPU63.516秒释放 |
| SRV-11k | 当前启动协议持久任务交接 | [限定接受](SERVE_TASK_STARTUP_PROTOCOL_RESULT_2026-11-04.md) | 239原件独立审核、24 CPU/4事务SIGKILL；同锁2模型4响应2工具、2应用中断后只续最终回答，重复零执行；263原件重建，GPU92.851秒释放，默认保持 |
| SRV-11b | 项目任务输出预算与逐轮预检 | [本机限定接受](SERVE_PROJECT_TASK_BUDGET_RESULT_2026-11-04.md) | 31应用CPU/3驱动；alpha512/beta64各真实工具/引用通过，超额拒绝、批准后SIGKILL重新预检；4成功/6预检/3重复零执行，GPU60.126秒释放；263原件封存，day未跑 |
| SRV-11c | 任务源码升级与同客户端Key轮换 | [本机限定接受](SERVE_TASK_MIGRATION_RESULT_2026-11-04.md) | 49应用CPU/3原驱动/5定向；原容量拒绝保持，2原任务迁移后复用结果完成，旧Key401；累计4成功/2工具，重复迁移/提交零执行，468原件封存，GPU释放 |
| SRV-11d | 终态归档、永久保键与任务容量 | [本机短验限定接受](SERVE_TASK_ARCHIVE_RESULT_2026-11-04.md) | 69应用/5驱动有效CPU；1模型1网关4成功2工具，2文件操作SIGKILL恢复、3重复提交/3恢复零执行；402原件封存，GPU62.342秒释放；原夹具失败保留 |
| SRV-11e | 一致性备份与单活恢复交接 | [本机短验限定接受](SERVE_TASK_BACKUP_RESULT_2026-11-04.md) | 88应用/4驱动首轮、23文件SIGKILL；真实1模型1网关4成功2工具、4进程中断后交接，known复用/未知保持/重复零执行；368原件封存、GPU释放；非源盘丢失灾备 |
| SRV-11f | 管理命令错误分类与输入合同 | [限定接受](SERVE_TASK_ERRORS_RESULT_2026-11-04.md) | 106有效应用/2原驱动/2定向；真实4成功2工具、原驱动失败后离线补3拒绝零执行；395原件封存、GPU释放；CA夹具/导出原失败与终场metrics缺口保持 |
| SRV-12 | 本地运营告警与排障运行手册 | [已限定接受](SERVE_OPERATIONS_RESULT_2026-11-03.md) | 17告警/2记录、只读身份/私有投影；实际3生成/1启动、资料工具/诊断/临时监控通过，两条owner规则按真实口径CPU修订；原始日志轮转与通知未部署 |
| SRV-12b | 观测端点在连接饱和时的可用性 | [已限定交付](SERVE_OBSERVATION_CAPACITY_RESULT_2026-11-03.md)；3cd3994d | 原生23项、实际4生成/136负响应/1启动；128普通满可诊断/抓取、4观测满可健康/取消，真实工具恢复；v4/v3规则分列，慢头仍共享总额 |
| SRV-12c | 标准告警接收与恢复通知 | [本机短验限定通过](SERVE_NOTIFY_RESULT_2026-11-04.md) | 16 CPU/4驱动/2入口；2模型/4成功及真实工具、10回调/7确认，失败重试/禁止重定向/20秒去重/真实Down与resolved通过；GPU释放；外部接收/心跳缺失检测/监控全故障时序待部署 |
| SRV-12d | 独立接收端心跳缺失检测 | [本机限定接受](SERVE_NOTIFY_WATCHDOG_RESULT_2026-11-04.md) | 26有效CPU/4驱动/11官方时序；4监控/2观察器，180.052秒静默失联、观察器停20.108秒陈旧/重启不重置、真实恢复；零模型，同锁228.635秒释放；正式跨主机采集/通知仍待部署 |
| SRV-12e | 跨运行目录准入与私有留存 | [本机短验限定接受](SERVE_RETENTION_RESULT_2026-11-04.md) | 22有效CPU；1模型/2网关/6成功/1真实工具；启动容量拒绝、3封存/1显式清理、同PID恢复；GPU57.807秒，199原件/2 ELF身份封存；默认/三份day保持，非全盘硬quota |
| SRV-12f | host资源观测与告警接入 | [限定接受](SERVE_HOST_OBSERVATION_RESULT_2026-11-04.md) | 37有效CPU/5+3+3驱动/22官方时序；累计2模型、1网关/4成功2工具，实际Stale/恢复/终态及只读源通过；622原件封存、GPU185.835秒释放；保留原夹具/行序/报告失败 |
| SRV-12g | 独立本机运维入口与慢头隔离 | [限定交付](SERVE_OPERATIONS_LISTENER_RESULT_2026-11-04.md) | v5观测/v6包、33原生与实际136公共满额下诊断/发现/取消；累计2模型、5成功2工具，3原失败保持；GPU159.294秒释放，默认7eff020d；12h后续补托管，实际LAN/长验待 |
| SRV-12h | 独立运维托管生命周期 | [限定接受](SERVE_OPERATIONS_MANAGED_RESULT_2026-11-04.md) | 5 CPU；实际136公共满额新监督样本/诊断，1模型/2网关/2启动拒绝、4响应2工具；64.777秒同锁释放，默认/day保持，v6+host/实际LAN/长验仍待 |
| SRV-12i | 声明/运维/host域组合 | [限定接受](SERVE_MANAGED_PROFILE_RESULT_2026-11-04.md) | 9+1 CPU；新v6包/136公共满额、1模型2网关4响应2工具，88样本见4grammar继承payload；同锁70.048秒释放，152原件封存，原prepare/归档日志失败保持；不含完整物理RAM/实际LAN/用户长验 |
| SRV-12l | 托管观测绝对期限与停止优先 | [限定交付](SERVE_SUPERVISOR_DEADLINES_RESULT_2026-11-04.md) | 114+6+3 CPU通过；4响应2工具/136满额/3传输通过；首轮ENV_ABORT保持，单代加载期stop 1.284830秒补验通过，默认/旧包/day保持 |
| SRV-12k | 原生启动双监听所有权 | [限定交付](SERVE_STARTUP_LISTENERS_RESULT_2026-11-04.md) | 默认61abe09f；41原生/5无模型CLI、1模型6尝试2工具通过，GPU58.011秒释放；291原件，清单首轮拒绝保持；新包仅准备，启动TCP不等于HTTP就绪 |
| SRV-12j | 托管双端口启动前检查 | [限定交付](SERVE_PORT_PREFLIGHT_RESULT_2026-11-04.md) | 38 CPU+4报告定向；两真实冲突约0.388秒/零后端启动、占用者不变；1模型4响应2工具、同锁64.646秒释放，182原件封存；原夹具/空argv报告失败保持，当轮原生晚绑定由12k补齐；单独预检不保证无竞争 |
| SRV-13 | 较长持续运行与重启/发布回退 | [限定接受并修复交付](SERVE_LIFECYCLE_RESULT_2026-11-03.md)；c597adc3 | 旧ELF一小时240周期通过，原停机失败保留；修复后定向关停/崩溃/重启/实际v3回退及安装真实工具通过，累计1218生成/4负例/6启动，不证明新ELF小时或天级长稳 |
| SRV-02e | 启动可用内存保护与Thor计量边界 | [已限定交付](SERVE_STARTUP_MEMORY_RESULT_2026-11-03.md)；f69982d5 | 三检查点、2启动拒绝、5真实生成/工具与4类256MiB计量；原worker断言失败保留，纠正复用后交付；cudaMalloc未见相当memcg增量，物理硬限额仍待办 |
| SRV-02f | serve设备分配/释放失败传播 | [已限定交付](SERVE_DEVICE_MEMORY_RESULT_2026-11-03.md)；40cf6376 | 仅已知OOM降级，其他设备/释放错误停服、成功终态前回收；真实工具、缓存8473-token恢复、fatal/startup/shutdown通过；原SSE取证失败保留，累计17/8/10，非完整模型资源覆盖 |
| SRV-02g | 默认文本启动降级与内部资源错误传播 | [已限定交付](SERVE_BACKEND_CLEANUP_RESULT_2026-11-03.md)；3fd31a21 | 请求MTP失败拒绝启动；直接CUDA清理/完成失败退出1，已有serve错误协议保持；原探针误断言保留，18探针/7启动/10生成/6负场景通过，非完整分配或物理保护 |
| SRV-02h | 媒体与host异常/错误响应完整性 | [组合限定交付](SERVE_MEDIA_COMPOSITE_RESULT_2026-11-03.md)；280b95dc | 原生25、视觉所有权/333上传、真实文本/工具/图片/两帧、OOM恢复/fatal/host/SSE/启动拒绝通过；原CPU失败保留 |
| SRV-02i | 媒体解压与几何资源边界 | [组合限定交付](SERVE_MEDIA_COMPOSITE_RESULT_2026-11-03.md)；280b95dc | 实际11类准入拒绝、配额、分配哨兵/同PID恢复及媒体SSE取消通过；组合32/21/11首轮完成，物理RAM硬保护与广泛媒体质量仍待办 |
| SRV-02j1 | CUDA申请额度核心 | [核心限定通过](SERVE_CUDA_QUOTA_CORE_RESULT_2026-11-04.md) | 26有效CPU/4驱动/6保管CPU；修正后4 native/112MiB/14检查点，同锁2.380秒；原CPU失败/ENV_ABORT保持；该阶段默认/day不改，生产接线由02j2补齐，非物理RAM限额 |
| SRV-02j2 | 生产CUDA申请额度接线 | [限定交付](SERVE_CUDA_QUOTA_INTEGRATION_RESULT_2026-11-04.md) | 生产wrapper/启动/CLI/指标接线；28 wrapper/268公共/234必需/9后端，修正轮6 native/25 HTTP生命周期/7生成与真实资料工具通过；463原件，原flags失败保持，非整机物理RAM硬保护 |
| SRV-02k | 托管host记账域与进程树保护 | [限定接受](SERVE_HOST_ENVELOPE_RESULT_2026-11-04.md) | 155 CPU+3驱动、六类内核；1模型1TLS/4成功2工具、385原件封存；原high探针超时保持，GPU释放，非物理RAM限额 |
| SRV-14 | 当前发布的受控服务托管 | [实际限定通过](SERVE_MANAGED_SERVICE_RESULT_2026-11-03.md) | 首轮交接503已修复；新默认同锁2启动/6成功/2真实工具、1替换及退避stop取消通过；累计托管10次、原失败保持，无常驻服务安装 |
| SRV-15 | 托管低内存保护退场 | [实际限定通过](SERVE_MEMORY_STOP_RESULT_2026-11-03.md) | CPU51有效；实际1启动/4成功/真实工具，2.25GiB有界压力触发30GiB门限退场，禁重启/清理通过；默认1GiB策略CPU证据，不代替物理RAM硬限额 |
| SRV-16 | 独立前台控制器死亡 | [实际限定通过](SERVE_CONTROLLER_DEATH_RESULT_2026-11-03.md) | CPU8有效；1启动/4成功/2完整shutdown错误，活跃/排队grammar自然退场且无辅助清理；同一锁副本与独立监控到结束，无复起；生产实现不改 |
| SRV-17 | 失联后只读运维核验 | [实际限定通过](SERVE_INSPECTION_RESULT_2026-11-03.md) | CPU51/v1-v2兼容；v3实际1启动/3成功含真实工具，live BUSY/stopped AVAILABLE，旧缺证UNKNOWN且原件不改；无复起/GPU锁释放 |
| SRV-18 | 当前托管版本短验/长稳交接 | [短验限定通过](SERVE_SOAK_RESULT_2026-11-03.md) | CPU11；1启动/8周期/40成功、8真实工具、120秒同后端；GPU/原锁释放；day仅准备，由用户开展长时间验证 |
| SRV-19 | 发布声明与冻结合同一致性 | [限定交付](SERVE_RELEASE_DECLARATIONS_RESULT_2026-11-04.md) | 四声明/三schema摘要修正；62 CPU/七schema/实际旧声明拒绝，旧包保持；仅sidecar cc83bf3a更新，ELF/协议保持，零模型/HTTP/GPU，新包仅准备 |

“待开发”只表示排期，不能写成已开始或已支持；“条件待办”不自动
触发扩展。阶段 A/B 为业务与可靠性推进。SRV-09 若包含解码、批处理
或缓存提速，必须拆出性能任务，遵守五档 E2E，不能沿用业务验收替代。

SRV-08[限定交付与证据](SERVE_PROTOCOL_RESULT_2026-11-03.md)已完成。
03b[本轮未通过](SERVE_BACKPRESSURE_RESULT_2026-11-03.md)，严格载荷未到写阶段，
不反复扩样。02b已[限定交付](SERVE_ADMISSION_RESULT_2026-11-03.md)，
01b也已[交付](SERVE_OBSERVABILITY_RESULT_2026-11-03.md)。之后的独立
[严格复制长输出候选](SERVE_STRICT_TRANSPORT_RESULT_2026-11-03.md)已限定通过，
原输出要求/token预算不变、实际token取证完整；不追认旧失败或重启旧预算。
随后[统一内存准入](SERVE_MEMORY_ADMISSION_RESULT_2026-11-03.md)已限定交付。
之后[同PID压力恢复/64轮混合业务](SERVE_PRESSURE_RESULT_2026-11-03.md)已限定接受。
本地运营[限定接受](SERVE_OPERATIONS_RESULT_2026-11-03.md)后，观测端点
饱和隔离也已[限定交付](SERVE_OBSERVATION_CAPACITY_RESULT_2026-11-03.md)。
SRV-13[限定接受并交付关停修复](SERVE_LIFECYCLE_RESULT_2026-11-03.md)。
SRV-02e[启动检查及Thor计量限定交付](SERVE_STARTUP_MEMORY_RESULT_2026-11-03.md)。
SRV-02f[文本/调度资源故障限定交付](SERVE_DEVICE_MEMORY_RESULT_2026-11-03.md)。
02g[默认文本内部清理已限定交付](SERVE_BACKEND_CLEANUP_RESULT_2026-11-03.md)。
02h与02i按[组合GPU合同](SERVE_MEDIA_COMPOSITE_2026-11-03.md)完成
[32/21/11首轮验收及默认交付](SERVE_MEDIA_COMPOSITE_RESULT_2026-11-03.md)。
SOAR恢复服务已按用户授权正常停止并持久mask，GPU/锁释放；原CPU
失败与ENV_ABORT保留，未重复未变测试。
物理硬限额、实际访问运维与多人策略仍需独立完成。
B通过不表示工业化持续目标完成。

### A：先完成入口与持续服务保障

**SRV-01：统一请求身份与错误定位。**

- 范围：有界 `X-Request-Id` 接收/生成/回传、请求上下文和错误日志；
  自动身份跨重启不重复；可定位的输入错误填写 `param` 与稳定 code。
  明确 HTTP 关联 ID、completion ID、取消目标之间的关系，保留取消
  凭据隔离；关联 ID 本身不成为权限或幂等执行凭据。
- 入口：`http_request.cpp`、`chat_http.cpp`、`chat_generation.cpp`、
  `http_response.cpp` 和请求诊断；复用 HTTP/Chat 合同测试。
- 验收：无 ID、合法自带 ID、重复/超长/非法头、JSON ID 冲突策略；
  成功、JSON 解析拒绝、容量拒绝、首帧前后错误均能定位。日志不记录
  cancel token 等凭据；已记录的请求身份与客户端 SDK 所见一致。

**SRV-02：就绪 / 排空与控制入口保障。**

- 范围：分别定义 liveness、readiness、draining；明确启动、GPU
  不健康、关闭及普通繁忙的状态转换。为健康和取消保留有界入口，
  总连接、header/body 预算仍有限，不能靠无限额豁免控制请求实现。
- 入口：`chat_http.cpp`、`chat_metrics.cpp`、请求/连接生命周期。
- 验收：CPU 假后端覆盖状态机及连接饱和；真实 HTTP 有界混合验证
  普通/严格请求、控制入口、取消和关闭。取消已接受与资源已回收
  分别观察；普通繁忙不被无依据标成 GPU 故障。

**SRV-03：HTTP / SSE 合同、心跳与慢读边界。**

- 范围：Content-Type、方法/路径/query 的明确处理；SSE 可空字段、
  usage 与终态；等待期间心跳、慢读者超时及真实代理链路。
  开发前冻结 HTTP 200 提交时机、首帧前后错误切换和 socket 单写者
  所有权，避免心跳与结果竞争写入。心跳不计首内容 TTFT 或 token。
- 入口：HTTP reader/writer、`chat_generation.cpp`、响应 parser；
  复用已有非阻塞写、取消和 30 秒逻辑写截止，不将它们误报为缺失。
- 验收：生产路径真实 socket 慢读、停止读取、断连及代理 idle timeout；
  长 prefill / 严格输出等待期间保活；终态一次、无错误后成功尾帧，
  槽/worker/连接在各自合同内回收。严格参数仍完整验证后才能执行。
  本项不承诺逐 token 工具参数发布，后者归 SRV-09 单独设计。

### 阶段 A 实施冻结

从任务配置提交48ac853继续，候选构建与证据进入
`.q4t-work/serve-stage-a-20261102/`。只改服务入口与协议，不改模型
数值、GPU调度选批或缓存计算。HTTP关联ID与JSON请求/取消ID分开；
已解析的合法追踪头回传，读取/准入早期失败使用服务生成身份。
心跳与数据帧串行写入，完整参数验证后才可执行工具。控制入口保留
有界容量，保障已分类普通业务饱和时的可用性，不承诺抵御无限慢头
或连接洪泛。

真实验收冻结30分钟、3次启动、12生成尝试、16负例上限；计划生产
候选7生成/9负例、背压专用构建2生成、安装健康2生成。见
[执行工具与命令](../tools/serve_stage_a/README.md)。专用构建只缩小
发送缓冲，在真实EAGAIN处握手；不安装到默认。代理上游7秒idle
窗口观察严格缓冲等待期间5秒心跳；不是所有长prefill/代理配置证明。
模型/数值身份未改，复用已有数值证据，不宣称五档性能接受。

SRV-08首批机器合同为[传输响应schema](serve-transport-v1.schema.json)，
只覆盖错误、completion、SSE chunk、readiness封套，配合wire顺序检查。
完整输入OpenAPI和固定JS后续已由SRV-08限定交付；所有嵌套schema错误
的精确param与其他SDK未自动覆盖。严格长payload回补见
[SRV-03b冻结范围](SERVE_BACKPRESSURE_2026-11-03.md)。

### B：完善客户端能实际使用的协议

**SRV-04：消息角色与模板兼容。** 冻结目标模型的 developer/name
映射、system 位置和历史消息规则后实现；未知或不能保持语义的形状
明确拒绝，不默默丢弃。复用原生模板与 fixture，增加角色、工具历史、
thinking 开关的渲染/逐 ID 对照，以及实际目标客户端消息。通过标准
是明确 profile 的模板语义一致，不是可运行任意 HF Jinja 模板。

**SRV-05：推理字段与控制。** 定义 `reasoning` / `reasoning_content`
版本适配、重复字段冲突和历史回填；实现独立的输出隐藏策略。
推理 token 预算单独冻结“到界后转入答案或结束”的模型合同，检查
跨 token 标记、预算耗尽、工具调用、MTP 与普通解码；不把隐藏输出
或 `reasoning_effort` 当作已限制推理计算。无法保持合同的组合明确
拒绝并登记；只通过字段适配不能把整个任务标为完成。

**SRV-06：停止条件。** 首组为 `stop` 字符串；有具体调用需求再加入
token ID 扩展。覆盖中文/UTF-8、跨 token 和 MTP 多 token 块、重叠
停止串、EOS/长度竞争；JSON 与 SSE 文本、usage 及 finish reason
一致。停止不得把截断的工具/schema 输出伪装为成功，也不得先向 SSE
泄漏本应被截掉的后缀；受约束组合的支持/拒绝必须在能力表写清。

**SRV-07：请求预检与配置发现。** 提供只读的 token 计数、聊天模板
预检、实际上下文/输出预算、槽位和默认配置来源。预检共享真实请求的
校验、模板与分词路径；输入与资源上限同样生效。验收预检 token 数
与真实 prompt usage、长工具历史和边界拒绝一致；预检不是 GPU 槽位
预留或未来执行成功的承诺，不引入模型加载/推理的隐藏副作用。

**SRV-08：机器协议与客户端矩阵。** 随各项补版本化的请求/响应
schema 或 OpenAPI 子集，将字段/null/默认/拒绝、能力声明、清单、
文档与契约用例关联；静态发布即可，不为文档另开管理端点。
保留现有 Python SDK 3.42.1 证据，新增目标版本必须固定版本；扩一个
JS SDK 消费路径，覆盖 models、普通 JSON/SSE、真实工具往返和错误。
每批复用项目资料查询，核对工具业务错误、完整批次 ID、引用和恢复
记录。只选择受改动影响的代表组合，不把宽松客户端接受当完整 wire
合同通过，也不做全笛卡尔积。清单检查仍只证明未漏登记。

### B1 实施冻结

本批仅SRV-04与SRV-08相关消息合同子集，详见
[阶段B1范围与出口](SERVE_STAGE_B1_2026-11-03.md)；
[有界交付结果](SERVE_STAGE_B1_RESULT_2026-11-03.md)。SRV-05a[字段/隐藏已交付](SERVE_STAGE_B2A_RESULT_2026-11-03.md)，
05b真实预算按[合同](SERVE_THINKING_BUDGET_2026-11-03.md)继续；
先前未接受记录保留；[后续真实HTTP/缓存/安装已交付](SERVE_THINKING_HTTP_RESULT_2026-11-03.md)；
SRV-06[已限定交付](SERVE_TEXT_STOP_RESULT_2026-11-03.md)，SRV-07[预检/配置已限定交付](SERVE_PREFLIGHT_RESULT_2026-11-03.md)；不能把角色适配当作整个B阶段通过。

### C：由具体使用条件启用

2026-11-04用户明确高级能力可在实际执行时选择性启用。正式LAN/TLS/
来源网段/独立Key/通知和host策略配置不作为基础开发前置；选用时再
完成该部署profile的验收，未启用不记基础缺陷，也不宣称已部署。
长期运行验证由用户承担；代理不因缺正式配置自动暂停其他开发。

- **SRV-09：结构化扩展。** 由真实工具 schema、严格请求并发或增量
  参数需求触发。每次冻结一个 schema 关键字族或一个允许组合；保留
  grammar 掩码和独立最终验证、编译隔离及资源账。增加并发前先证明
  总预算与公平策略；MTP/缓存/packed 加速按性能任务独立验收。
- **SRV-10：网关与多人策略。** 由实际 LAN/远程/多人入口触发，明确
  TLS、鉴权、CORS、连接复用、请求限额/排队策略分别由网关还是 runner
  承担。实际部署验证绕过入口、过载、取消、代理超时和访问日志。
  Thor 统一 RAM 预算覆盖模型、KV、worker、中转及文件缓存；2GiB
  worker 地址空间不充当整机内存保证。先有界计量再确定资源策略。
- **SRV-11：应用恢复。** 在应用层维护响应/工具结果/未知执行结果的
  持久状态，明确幂等键、重复请求和人工或业务恢复策略；serve 的
  `session_id` KV 缓存和活跃 ID 去重不充当任务库。用进程退出及工具
  结果丢失场景证明不会盲目重放；不宣称通用 exactly-once。

采样、多 choice、logprobs、Responses、多模态、LoRA 和分布式管理
继续按模型能力及实际业务单独立项，不作为本队列完成的必要条件。

### 每批实施与交付门槛

1. 开始一批前冻结源代码/默认发布身份、允许组合、任务出口和证据
   复用表；源码变更后按依赖重新判定证据有效性。SRV-08 同步跟进。
2. 成组完成实现、必要零警告构建和直接 CPU 合同。纯文档任务只做
   文档/链接检查；不为了任务登记重复模型推理或已有数值验收。
3. 真实验收前将具体请求清单、token 上限、启动次数、失败重验额度
   和绝对墙钟上限写入阶段驱动；未冻结不启动模型，预算不自动扩展。
   A 重点为混合负载/控制入口/流式与真实工具回填，B 重点为新增协议
   和目标 SDK。记录 HTTP 质量与有界时延，不推导性能优化结论。
4. 全阶段模型加载、HTTP、GPU 测试均由同一 `gpu_exclusive.py` 锁
   覆盖。竞争或监控失败记 ENV_ABORT，仅停止本阶段自有进程，保留
   现场；配置任务不授予干预其他会话服务的额外权限。
5. 仅具体失败修复后重验受影响项；达到退出条件后按既有授权封存
   回退包、部署候选与匹配元数据、必要健康检查、更新状态及日志，
   分阶段 commit/push。安装前核对默认身份，未知并发修改停止覆盖。
   完成状态必须链接验收记录，提交本身不代表通过。

## 历史计划说明

2026-10-17工具收敛见[交付记录](MAIN_CONVERGENCE_2026-10-17.md)；近期研究及未实施计划见
[只读归档](ARCHIVED_RESEARCH.md)，不作为自动执行队列。

> 以下保留历史阶段范围；当前执行队列见上文，交付状态以STATUS为准。

## 历史阶段入口（2026-09-28）

首版私有文本Runner的固定收敛清单已完成，作为下一阶段开发基线。
版本身份、支持范围及限制见[收敛验收](RELEASE_TEXT_V1_2026-09-28.md)，
进入下一阶段前的复核见[基线交接](BASELINE_HANDOFF_2026-09-28.md)。
下一阶段须单独冻结目标与出口；下面Phase 1/2/3不是自动执行队列。

性能优化采用五档HTTP E2E；Bug修复采用针对性直接验证加相关HTTP
回归。成组修改、统一验收、仅重验受影响项，详见
[EVALUATION.md](EVALUATION.md)。

下列历史“已闭合”、MTP/媒体可用和bench记录不等于当前发布范围通过。

## Phase 1 — 核心推理引擎 + PLE SSD Stream + HTTP API

**目标**: 在本机 Thor 上跑通 Qwen3.8-Flash-Next 的完整推理路径,
以真实使用环境 (HTTP API + CLI) 为准进行开发和测试。

### 范围

1. **构建与基础设施**
   - CMake 构建 (SM110a), 硬件探测 (`probe` 子命令)
   - safetensors 解析 (零拷贝 mmap), JSON 配置解析
   - NVFP4 W4A4 / FP8 量化原语
   - Tokenizer (BPE, 复用模型目录中的 tokenizer.json)

2. **PLE SSD Stream (核心特性)**
   - io_uring 异步读取器 (4 KiB 页粒度, 页去重)
   - 注册页池 + 固定暂存缓冲区
   - 独立 CUDA stream 上的 FP8→BF16 转换
   - 与 GPU 计算重叠, 仅在读取超过重叠窗口时同步
   - sidecar 完整性校验 (SHA-256, 启动时)

3. **模型 forward pass (text)**
   - 48 层: 36 linear_attention (DeltaNet SSM, FP32 state) +
     12 full_attention (GQA 24Q/2KV, head_dim 256, Paged KV)
     - **Paged KV cache 为 Phase 1 硬需求** (原为 Phase 2 目标, 因 PD-ready
       架构提前, 见第 6 项)。✅ 已实现 (2026-09-05): 按页组织
       (`kKvPageSize=16`) + 页表间接寻址, 使 KV 可按页迁移/共享。
   - MoE: 512 专家 top-10 + shared expert, NVFP4 grouped GEMM
   - PLE 嵌入 (0-indexed layer 1, 每 token 16 次查找)
   - MRoPE (interleaved, section [11,11,10], partial_rotary 0.25)
     - 纯文本 (t=h=w=position) 下退化为标准 partial RoPE, 与当前实现
       数学等价 (已验证); 完整 3D MRoPE (t/h/w 三行 + mrope_position_delta)
       已闭合 (2026-09-12, 多模态, 见 MODEL.md)。
   - MTP 推测解码 (1 层 full_attention draft)

4. **服务与 CLI**
   - `serve`: OpenAI 兼容 HTTP API (chat/completions, streaming,
     healthz), 以真实客户端 (curl) 为准测试
   - `generate`: 单次 greedy 生成
   - `version` / `probe` / `models`

5. **验证**
   - 验证标准的建设**单独讨论** (见 STATUS.md 阻塞项),
     初步方向: 与参考实现输出对比 (greedy, 逐 token)。
     参考实现**灵活选用** (非锁定): PLE 机制看 sglang-ssd-stream,
     MTP/QSA 语义看 vLLM (`reference/vllm`), MoE 量化看 qwen35-thor。
     逐 token 对比是初步方向, 正式验证标准体系见 Phase 2。

6. **PD-ready 架构 (Prefill/Decode 可分离, 设计目标)**
   - 背景: runner 后期有特殊场景需 PD 分离, 架构须早期可分离,
     避免后期返工 (用户决定, 2026-09-05)。
   - Phase 1 落地**架构可分离性** (低成本、零风险, 非完整多设备分离):
     - prefill (T 首步) 与 decode (T=1) 保持**可分离代码路径**, 不融合;
     - **Paged KV cache** (见第 3 项) 使 KV 可按页迁移/共享 — PD-ready 硬前提;
     - 暴露**阶段边界 API**: 引擎能"完成 prefill、交出 KV/SSM 状态"
       作为独立操作 (供 runner 驱动 prefill 与 decode 为两次调用);
     - MTP 留在 decode 路径内。
   - **完整 PD 分离部署** (多设备/多实例、KV 跨设备传输、独立调度池)
     归 **Phase 2** (依赖连续批处理 + 多请求调度, 单卡 Thor 无独立
     prefill/decode 池可分)。Phase 1 只保证架构不堵死该路径。

### 完成标准

- [x] `q4t serve` 在本机启动, 通过 OpenAI 兼容 API 完成多模态
      (文本 + 图像) 对话, 流式输出正常 (2026-09-07 图像接入, 见 LOG.md)
- [x] PLE SSD Stream 工作内存 < 100 MiB (不含模型权重),
      无 OOM, 无 swap (2026-09-07 实测 75.17 MiB, 见 LOG.md)
- [x] greedy 生成输出与参考实现一致 (2026-09-07 L2 噪声保真度验证,
      见 LOG.md; 标准: 置信位置 argmax 全对 + 翻转全 near-tie + l2_rel
      在 W4A4 噪声带, 无系统性错误)
- [x] MTP 推测解码可用 (2026-09-07, 见 LOG.md)
- [x] 多模态 (图像输入) 可用 (2026-09-07, 见 LOG.md)
- [x] Paged KV cache 落地 (full_attention 按页组织 + 页表间接寻址,
      替代连续 KV; 2026-09-05 完成, 见 LOG.md)
- [x] PD-ready 架构: prefill/decode 可分离路径 (✅ 现状已满足) +
      阶段边界 API (✅ ModelSequence, 2026-09-05 完成, 见 LOG.md;
      完整多设备 PD 部署归 Phase 2)

### 明确不做 (Phase 1 范围外)

- 连续批处理 / 多请求并发 (Phase 2)
- 完整多设备/多实例 PD 分离部署 (KV 跨设备传输、独立调度池;
  Phase 2, 依赖连续批处理。Phase 1 只做 PD-ready 架构, 见第 6 项)
- 性能调优 (kernel 融合、TMA、PDL 等, Phase 3)
- 视频输入 (✅ 2026-09-12 已闭合, 见下)

## Phase 2 — 多模态完善 + 并发

- [x] 视频输入 (temporal_patch_size=2) — **已闭合 (2026-09-12)**: 27 层
  ViT 逐时间组注意力 + `ProcessVideo` processor + serve 视频 part 接入 +
  混合图像/视频 batch, 见 STATUS.md / LOG.md 2026-09-11~12。
- [x] 连续批处理、多请求调度 — **已闭合 (2026-09-13~14)**: B1 多序列隔离
  + B2 连续批处理 (token 级打包 + serve 调度器) + MTP 批处理 Stage 1/2a/
  2b/2c + 调度 lockstep (计划 A), 见 STATUS.md。
- [x] 验证标准体系落地 — **已闭合 (2026-09-12)**: `tools/verify/` 三件套 +
  48 层全量基线 OVERALL PASS (置信位置 44/44 + 翻转全 near-tie + l2_rel
  噪声带), 见 STATUS.md。
- **长上下文 262K (262144) 验证** — 已闭合 (2026-09-15, 内存实测 +
  分块 prefill):
  `--max-len 262144 --max-seq 1` 加载成功, 峰值消耗 ~96 GB (余 25.6 GB,
  无 OOM), 短 prompt 生成正常且确定 — 与预算估算吻合 (见下)。**分块
  prefill 已实现**: `max_prefill` 语义从 prompt 上限改为分块大小 (上限 =
  `max_len`), `T > max_prefill` 走 chunk 0 `ModelPrefill` + chunk 1..
  `ModelDecodeBatch` 续块 (绝对位置, 不重置状态), 中间块跳过 lm_head,
  一次性 `[T, vocab]` logits 在 262K 下 = 130 GB 不可行的问题由此解决。
  200K-token prompt E2E 无 OOM 生成正常 (prefill ~12.6s/2048-token 块,
  262K ≈ 27 分钟, MoE GEMM 带宽下限)。
- **完整 PD 分离部署** — 降级为后续计划 (2026-09-14 用户决定): 当前
  PD-ready 架构 (Paged KV + 可分离路径 + `ModelSequence` 阶段边界 API)
  已满足**本机调度**需求 (持续 prefill 场景靠 Paged KV 按页迁移 + 独立
  调度池即可, 无需多设备); **多设备/双机扩展** (KV 跨设备传输) 归后续
  计划, 届时需多卡硬件验证。

### 长上下文 262K 内存预算 (2026-09-14 评估, 2026-09-15 实测确认)

关键架构事实: 36 层 linear attention 的 SSM state 是 **O(1) 不随序列增长**
(`ssm_state [max_seq, 48, 128, 128]`), 只有 **12 层 full attention 的
KV+indexer cache 随 max_len 线性增长**。运行时 ground truth: 模型加载后
GPU 进程占 **82.4 GB** (含 max_len=8192×max_seq=4 的 cache), GPU 总
122.9 GB。

max_len 8192 → 262144 的 cache 增量 (12 层 full attn, KV [max_seq,
n_pages, 16, 2, 2, 256] BF16 + idx_raw/comp [max_seq, max_len, 128] BF16 +
page_table + rope_pos):

| max_seq | cache 增量 | 总显存 (估) | 可行性 |
|---|---|---|---|
| 4 (当前) | ~78.4 GB | ~158 GB | **OOM, 不可行** (实测: 加载后统一内存耗尽, 机器崩机) |
| 1 | ~19.6 GB | ~101 GB | **可行** (2026-09-15 实测: 峰值消耗 ~96 GB, 余 25.6 GB, 无 OOM) |

另: prefill 工作区是独立约束 — 一次性 prefill 262K 的 `[T, vocab]`
logits = 130 GB 不可行, **必须分块 prefill** (✅ 已实现, 见上: `max_prefill`
改分块大小 + `ModelDecodeBatch` 续块, 不重置状态)。模型层硬伤 (如实报告):
QSA `idx_budget=2048` 在 262K 时 n_groups=65536, 只能 attend ~3% 历史块,
召回受限 — 模型设计问题, 非引擎问题。

## Phase 3 — 优化

- kernel 融合 (RMSNorm+GEMV, QKV merge, QK_norm+RoPE)
- TMA bulk copy / PDL (Programmatic Dependent Launch)
- MoE grouped GEMM 调优
- 性能基线建立：tools/evalscope HTTP E2E，见 EVALUATION.md；不沿用 bench 方法。
