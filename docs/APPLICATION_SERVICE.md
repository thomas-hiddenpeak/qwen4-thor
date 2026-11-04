# 应用服务层与 OpenAI 兼容矩阵

更新：2026-11-04。范围由用户确认：OpenAI 兼容 API 优先，真实业务为
只读查询本项目文档、发布与验收记录。代码优先；实现、验证和业务质量
分别记录。此页是持续维护入口，历史验收不随新功能自动扩张。

2026-11-04 [SRV-11g显式CA交接与真实恢复限定接受](SERVE_TASK_TRUST_RESULT_2026-11-04.md)：
独立信任计划、同一CA材料快照、原任务/UNKNOWN/归档保键保持。
124 CPU/4驱动、27文件操作SIGKILL与六代证据；1模型2不同CA网关，
旧应用保存2工具后停止，新版仅续2最终回答，合计4响应2工具，旧CA
在HTTP前拒绝。重复apply/submit/resume零执行，394原件重建；两处
离线身份核对错误保留并修正，无追加推理，GPU释放。实际服务身份/
模型协议变化、LAN/正式策略和用户长验仍待。

2026-11-04 [SRV-11f管理命令错误合同限定接受](SERVE_TASK_ERRORS_RESULT_2026-11-04.md)：
稳定code/处置提示、安全异常出口和逐命令参数检查。106有效应用/2原
驱动/2定向回归；真实4成功2工具通过，原驱动导出失败后离线补齐管理
拒绝，零新增执行、任务库不变；395原件重建、GPU释放。保留原失败及
终场metrics缺口，不把管理错误当成可自动重试的许可。

2026-11-04 [SRV-11e一致性备份与单活恢复交接限定接受](SERVE_TASK_BACKUP_RESULT_2026-11-04.md)：
88应用/4驱动CPU首轮通过，源未变化核对/先封源后发布/身份追加。
真实1模型1网关4成功2工具、4进程SIGKILL后交接完成；已保存结果只
续最终生成、未知仍未知，重复不执行或回滚。368原件离线重建，GPU
已释放。只支持源可核对的同机交接，不提供源盘丢失或旧快照强制激活。

2026-11-04 持久任务后续已补齐[预算及逐轮预检](SERVE_PROJECT_TASK_BUDGET_RESULT_2026-11-04.md)、
[登记源码/同client Key迁移](SERVE_TASK_MIGRATION_RESULT_2026-11-04.md)和
[终态归档/永久保键](SERVE_TASK_ARCHIVE_RESULT_2026-11-04.md)。最新11d
69应用/5驱动有效CPU、1模型1网关4成功2真实工具；2次文件操作SIGKILL
显式恢复、3重复submit/3resume零执行，402原件离线重建，GPU已释放。
正文清理不会使同键重新执行；正式LAN、容量/访问/保留策略、备份
迁移、物理RAM与用户长验仍待完成，不以本机短验代替实际部署。

2026-11-04 [持久项目任务/真实进程恢复限定接受](SERVE_PROJECT_TASKS_RESULT_2026-11-04.md)：
20有效CPU/2驱动，原TLS EOF失败保持；修正后3应用SIGKILL/3任务完成，
已知复用、未知显式处置与同key重复submit零执行通过。7请求含1断流，
4工具尝试/3结果、真实资料引用，GPU释放。源码/凭据迁移、归档保键、
客户端预算匹配及正式接入待完善；当前默认和最新运维证据以STATUS为准。

2026-11-03 [媒体失败、准入与取消组合限定交付](SERVE_MEDIA_COMPOSITE_RESULT_2026-11-03.md)：
当时默认280b95dc/18c9d2c5，211来源；实际32生成/21负场景/11启动首轮
通过，含真实资料工具回填、红图/两帧、11准入负例、解码配额、OOM/
取消后同PID恢复与错误停服。原生25和实际视觉权重通过；CPU复用。
媒体仍为显式无MTP实验配置，不扩广泛媒体质量、音频、物理RAM或长稳。

2026-11-03 [小时段与关停/重启/回退限定交付](SERVE_LIFECYCLE_RESULT_2026-11-03.md)：
当时默认c597adc3/a350c978。旧ELF240周期一小时通过，原排队关停分类失败
保留；修复后活动/排队server_shutdown、崩溃unknown无重放、实际v3回退
及安装程序的真实工具闭环通过。累计1218生成/4负例/6启动，原预算不扩。
原小时证据只在未变路径范围复用；不宣称新ELF小时或天级长稳。

2026-11-03 [观测连接隔离已限定交付](SERVE_OBSERVATION_CAPACITY_RESULT_2026-11-03.md)：
当时默认3cd3994d/bb620240，观测4/控制4独立；锁内原生23项和真实4生成/
136负响应/1启动首轮通过。128普通连接满时观测/诊断/真实抓取可用，
4观测满时健康/有效取消可用，普通和真实资料工具恢复通过。v4指标92/
旧v3指标87与告警明确区分，旧pair和规则可核验回退。未知慢头仍共享
总额；SRV-13结果见上方，通知/日志轮转与实际访问仍待办。

2026-11-03 [本地运营能力限定接受](SERVE_OPERATIONS_RESULT_2026-11-03.md)：
只读诊断/发布身份、匿名日志投影/私有包留存、17告警与运行手册；
35项CPU、25组时序通过，实际3生成/1启动和真实资料工具回填、三份
诊断/临时Prometheus健康及停机告警通过。两条owner告警按metrics
自身占连接1修订、8组定向时序通过，GPU释放，默认pair保持。实际
通知/日志轮转未部署；当时观测端点使用普通额度，后由上方12b补饱和保障。

2026-11-03 [同PID压力恢复与64轮混合业务限定接受](SERVE_PRESSURE_RESULT_2026-11-03.md)：
当时直接复用安装84d16983/c5203331；326生成尝试/4负例/1启动及65预检
首轮符合预期，含324成功/1取消/1排队内存拒绝、65次真实工具执行。
4.875GiB匿名压力的关闭/排队回收/滞回/同进程恢复后，64轮普通JSON/SSE、
strict及真实工具闭环通过，worker196/196reap和冻结资源包络通过。
仅约646秒有界业务，未证明长稳、整机RAM硬保护或启动保护；GPU已释放。

2026-11-03 [统一内存准入已限定交付](SERVE_MEMORY_ADMISSION_RESULT_2026-11-03.md)：
当时默认84d16983/c5203331。全局MemAvailable观测/未知拒绝、门限和恢复
滞回、body前/准备/slot检查、ready和固定指标；真实5生成/4拒绝/2启动
及1预检通过，实际资料工具回填正常，GPU释放。高floor用真实读数安全
触发拒绝；该阶段动态恢复仅有原生未加载HTTP/队列注入证据，后续由
上方02d补证，完整RAM硬保护仍未具备。原生19项缺归档原始输出的限制保留。

2026-11-03 [严格复制长输出背压限定通过](SERVE_STRICT_TRANSPORT_RESULT_2026-11-03.md)：
明确复制条件下，固定12443字节严格输出实际244token含EOS完成；真实TCP
EAGAIN后的SSE取消、JSON写截止、排队普通/strict恢复、控制与资源清理通过。
6生成尝试含2预期不完整传输、4负例、2启动首轮完成；默认ed672a65/67897c81
不变、GPU释放。旧03b失败保留，不保证任意strict请求在token预算内完成。

2026-11-03 [SRV-01b接收/输出失败观测已限定交付](SERVE_OBSERVABILITY_RESULT_2026-11-03.md)：
当时默认ed672a65/67897c81。body额度专属503、首次接收/写失败固定原因、
心跳join后计数、trace日志对账；生产body/读取截止/TCP reset/真实工具及
专用普通SSE取消/写截止/恢复通过。8生成尝试含3次预期不完整流、16拒绝、
2启动；不把接收拒绝与chat结果相加。当时严格长payload未证明，后续见上方。

2026-11-03 [SRV-02b准入/容量FIFO已限定交付](SERVE_ADMISSION_RESULT_2026-11-03.md)：
当时默认c16caf05/be4cd021。共享chat/preflight准入、slot等候上限、准备后
容量FIFO、取消注销、配置/指标；生产MTP总准入/真实JS资料工具和专用plain
异容量排队/满队列JSON/SSE拒绝/恢复通过，10生成尝试/4拒绝/2启动。
不承诺HTTP到达顺序、每租户公平或整机RAM。接收/写失败观测随后已按
上方限定交付，访问运维与长期稳定仍待完成。

2026-11-03 [SRV-07预检/配置已交付](SERVE_PREFLIGHT_RESULT_2026-11-03.md)：
当时默认5642eb68/6aca6a3e。只读预检共用生产合同/模板/分词/预算，
返回值与真实usage一致；有效配置可发现。预检不预留槽位、不编译grammar，
不保证执行或答案质量；双槽plain已验，双槽MTP生成未验。
[SRV-08完整输入/JS接入已限定交付](SERVE_PROTOCOL_RESULT_2026-11-03.md)：
38字段/10路由加必需原生语义、Node24.18.0/openai7.47.0真实JSON/SSE、
资料成功/业务错误/同响应双工具和显式答案恢复通过。10生成尝试/16拒绝/
1启动、105.73秒，含预期截断；默认pair不变，原Python与运行时证明复用。

[SRV-03b本轮未通过](SERVE_BACKPRESSURE_RESULT_2026-11-03.md)：长strict载荷
在输出预算内未闭合，1生成/1启动，没有实际EAGAIN。取证缺口/原始失败
保留，工具已CPU修复，默认不变、GPU释放；不能把CPU编码估计当真实生成。
[SRV-02b准入与容量FIFO](SERVE_ADMISSION_RESULT_2026-11-03.md)随后已交付，
不能代替03b的长载荷背压证明。

2026-11-03 [SRV-06文本停止已交付](SERVE_TEXT_STOP_RESULT_2026-11-03.md)：
当时默认a3fd64a3/49c7c631。跨token/块与中文停止、JSON/SSE、MTP逻辑
停止和完整KV退休、普通工具截断错误、真实资料回填及安装plain已验。
21有效用例，累计22生成/16负例/4模型启动，保留所有入口/夹具失败。
产品预检已在SRV-07交付，完整客户端矩阵和运营仍待后续。以下为既有交付历史。

2026-11-03 [SRV-05b已交付](SERVE_THINKING_HTTP_RESULT_2026-11-03.md)：
当时默认31ffc41e/9a71a0a7。原生thinking预算、实际控制token、MTP回退、
usage、严格工具/JSON和私有缓存恢复已按19用例验收。累计21次生成含
预算1原质量失败，修正为原生换行/close/空行后通过；完整失败与复用记录保留。
[05a字段/隐藏交付](SERVE_STAGE_B2A_RESULT_2026-11-03.md)继续有效；
stop、产品预检、完整客户端矩阵和运营要求仍待后续。

2026-11-03 [serve阶段B1已交付](SERVE_STAGE_B1_RESULT_2026-11-03.md)：
当时默认f1133bcc/2edfb882。单条开头developer作为system替代，混用、
重复、错位与非null消息name拒绝；指令媒体解码前拒绝。46组模板正例
逐字节/逐ID及12拒绝形状通过；真实SDK JSON/SSE、工具回填和安装健康
通过。推理控制、stop、预检和完整客户端矩阵仍待后续。

2026-11-03 [serve阶段A已交付](SERVE_STAGE_A_RESULT_2026-11-03.md)：
该阶段默认142bb9d4/94ce0825。X-Request-Id、显式错误param、livez/readyz、
4个控制连接预留、HTTP媒体/方法/query合同、提前SSE与5秒串行心跳、
nullable字段和普通流实际背压均有有界证据。严格payload继续整份
缓冲，该长响应背压组合未证明；其他角色组合/推理/停止/预检仍待B后续。

2026-11-02 [编译/匹配隔离已交付](SERVE_GRAMMAR_ISOLATION_2026-11-02.md)：
此前默认c8c412c4/sidecar da9f3ac5。F5计时口径与固定取消原因、严格
请求独立单租约继续保留；F6新增exec worker，2GiB RLIMIT_AS、10秒
编译/2秒匹配、取消kill/reap。post-exec VmHWM与继承峰值rusage分列。
边界是worker地址空间，不是整机RAM/独立RSS执法；32/64MiB storage
门仍在。已验真实工具/超时/取消/关闭，未推广到生产全schema/多租户。

2026-11-02 后续[输入合同 A 组已交付](SERVE_INPUT_CONTRACT_2026-11-02.md)：
上一默认为 ea678009 / sidecar90372798。各模式工具定义、历史参数与
结果、逐项 null 默认、取消形状和精确数字经过 host / HTTP / 真实
JSON/SSE 项目工具往返验收；没有扩大普通 auto 的 schema 保证。

2026-11-02 后续[工具控制与结构化输出已交付](STRUCTURED_OUTPUT_RESULT_2026-11-02.md)：
默认 957211c7 / 9eba5472，固定 grammar 后端与有限 schema profile
通过有界 SDK / HTTP / 项目读取验收。实现合同见
[profile 与组合限制](STRUCTURED_OUTPUT_DEVELOPMENT_2026-11-02.md)。

2026-11-02 后续[serve 全链路审计](SERVE_COMPLETENESS_AUDIT_2026-11-02.md)
补充了普通 auto 工具定义 / 历史调用嵌套校验、null 默认值、取消
端点和指标口径的具体缺口。下表“已验”只表示列出的限定合同，
不能推广到所有可空输入或所有组合。F1/F2/F3/F4/F7 已按输入合同
范围修复并验收；F5已修复并验收；F6已交付独立进程、超时/取消与工作进程地址空间限额；不宣称整机RAM
或独立RSS硬保证。

## 边界与成熟度

当前具备可用的私有文本推理服务和有界工具调用链路。尚不能宣称通用
OpenAI API 全兼容或生产多租户应用平台。模型支持、模板一致、输出可
解析、客户端可接入和业务答案正确，是五个不同的验收层级。

| 层次 | 已有实现 / 证据 | 尚欠能力 |
|---|---|---|
| 推理与请求生命周期 | GPU 错误隔离、容量准入、取消、超时、槽回收；已有 Thor 验收 | 与应用协议分开验收；不把本轮当性能优化 |
| 消息与模板 | 原生 qwen4_exp、单条developer适配、工具历史ID、推理分离；46组模板正例 | 并非运行任意 HF Jinja 模板；新增模型需要独立适配 |
| 输出协议 | JSON/SSE、工具控制、有限strict/schema、usage、错误、稳定created、预算/stop及完整输入机器合同 | 任意JSON Schema和逐字工具参数增量未实现；标准结构合同必须配合原生语义/动态准入 |
| 客户端 | 项目客户端、持久记录；Python3.42.1与JS7.47.0限定互通；SRV-11系列按client幂等/显式恢复、预算/预检、登记源码/Key迁移、归档永久保键、停写备份/单活交接、管理错误合同已短验 | 其他框架未验；正式访问/保留/容量策略、源盘丢失灾备和跨版本热升级待完善；未知结果不自动重放 |
| 应用工具 | 真实发布、历史验收与不存在资料查询通过；来源范围、工具业务错误与预算门已验 | 自主广泛搜索、逐句事实验证与实时状态查询未验 |
| 服务运维 | livez/readyz、独立4控制/4观测分区实际受载通过；固定指标/trace、17告警/版本诊断/手册、已演练关停/崩溃/重启和实际发布回退；10e新网关材料/有效期退场及同CA换证真实工具通过；10f只读TLS与10规则，临期/Stale/恢复/终态本机HTTPS投递通过 | 未分类慢头共享总额；本机TLS/Key/客户端预算、通知/心跳和跨代次留存见10c–10f/12c–12f证据；实际LAN/正式通知与留存策略、物理RAM及用户长稳仍待完成 |

Transformers 的模型与模板接口、vLLM 的推理服务接口、Q4T 的 Thor
原生 runner 是不同层次。此矩阵评价 Q4T 对外可用合同；不从底层模型
支持推导应用兼容，也不把外部项目的“支持 OpenAI”解释为每字段等价。
此前模型调查入口见 [参考项目](REFERENCE.md)。

## API 兼容矩阵

当前交付为 c597adc3 / sidecar a350c978，盘点起点为 460ffd0 /
旧默认 830694ce。`已验`指本项目已有明确证据，不代表外部 SDK 全部
版本通过。P1 / P2 的验证范围与具体失败在本页末尾及结果文档记录。
完整 vLLM 对照与优先级见[32项服务能力盘点](VLLM_SERVICE_GAPS_2026-11-02.md)。

| API / 能力 | 当前状态 | 限制与证据 / 行动 |
|---|---|---|
| POST /v1/chat/completions | 已验，子集 | 单模型、单 choice；chat_generation.cpp |
| GET /v1/models | 已验，SDK 3.42.1 | 单个配置模型；不支持模型详情 / 管理 |
| GET /v1/capabilities | 已验，Q4T 扩展 | 实现身份，不是发布或质量证明 |
| messages | profile限定已验 | 默认解析；system/developer/user/assistant/tool；system或developer仅第0条且不能混用 |
| message.name | 明确拒绝非null | 模板不表达名称；省略/null允许，含空串的其他值400；tool_call_id负责工具关联 |
| prompt / parse_response=false | 已验，Q4T 扩展 | raw 文本；不解析工具或推理 |
| JSON / stream=true | 限定已验 | 首帧提前；5秒注释心跳；finish → 可选 usage → DONE；中断不能当完整响应 |
| content / reasoning_content / reasoning | 05a限定已验 | 默认reasoning_content，可用reasoning_field选择；JSON/SSE固定单字段；历史别名相等合并/冲突拒绝 |
| include_reasoning | 05a限定已验 | bool默认true；false隐藏解析出的推理，计算/usage保留；raw/unparsed与null拒绝，不是正文过滤或隐私隔离 |
| thinking_token_budget | 05b限定已验 | parsed messages精确整数0..2147483647/null；自然配额+最多14强制边界token，总输出优先；MTP预留pending后回退；不是FLOPs上限 |
| function tools / tool_call_id | 已验 | ID 唯一完整批次；文本结果可乱序归一；客户端执行 |
| tool_choice | 已验，子集组合 | auto / none / required / named；named 恰好一个调用，显式约束模式仅 parsed messages |
| parallel_tool_calls | 已验 | false 最多一个；true 允许多调用，不代表应用并发执行 |
| strict / JSON Schema 输出约束 | 有限 profile 已验 | q4t-json-schema-v1，逐 token 掩码＋最终验证；未知关键字拒绝，应用仍须验证业务 |
| max_tokens | 已验 | 正整数；包含推理 / 模板生成 token；受上下文容量约束 |
| max_completion_tokens | 已验 | 与 max_tokens 共用生成预算；两个字段同时非空时拒绝歧义 |
| temperature / top_p / n | 部分 | 仅 0 / 1 / 1；不可声称支持采样 |
| penalties / seed | 部分 | 仅中性 penalty；seed 在 greedy 下不改变结果 |
| stop | 已验，有限profile | 字符串或最多4串，每串256 UTF-8字节；null/[]禁用；匹配包含推理/工具标记，截断工具错误；严格输出/cache组合拒绝 |
| logprobs / logit_bias | 明确拒绝有效设置 | 仅接受现有中性空值，未实现 |
| response_format | 已验，有限 schema | text / null / json_object / json_schema；与非空 tools 同用须 none |
| stream_options.include_usage | 已验 | choices块usage:null，最后独立usage；choices的logprobs:null；中断不保证收到尾帧 |
| chat_template_kwargs | 已验，Q4T 扩展 | thinking/preserve/effort/vision ID；能力端点给出范围 |
| unknown fields | 明确拒绝 | 不静默丢弃；Responses/store/metadata 等不可直接透传 |
| 错误 JSON | 已验，Q4T 错误码 | 显式输入位置param、统一封套与追踪头；部分嵌套schema错误仍param:null |
| SSE 错误 | 已验 | 首帧后HTTP200内error＋DONE；部分写失败直接截断，不追加第二终态 |
| 请求取消 / deadline | 已验，Q4T 扩展 | 202 仅接受取消；404 不证明请求未执行 |
| request_id / session_id | 已验，Q4T 扩展 | 活跃 ID 去重 / 同进程 KV 缓存；不是持久任务与幂等键 |
| X-Request-Id | 已验 | 128字节受限ASCII，自动128随机位；与JSON请求/取消ID独立，不提供幂等 |
| GET /livez、/readyz | 限定已验 | 普通忙仍ready；GPU unhealthy/排空/内存压力或观测失效ready503；加载后才监听 |
| HTTP媒体 / 方法 / query | 已验 | application/json UTF-8；405＋Allow、query400、GET body400；连接关闭 |
| 控制入口 / 慢读 | 限定已验 | 4控制连接预留；业务/观测满仍健康和有效取消；普通增量流真实EAGAIN后取消回收；不保证慢头洪泛 |
| 观测入口 / 诊断 | SRV-12b限定已验 | 精确GET metrics/config/capabilities/models独立4名额，业务128满仍可用；满额503/observation_capacity_exhausted，v3/v4口径分开；未分类头仍共享总额 |
| POST /v1/chat/preflight | SRV-07限定已验 | 共用合同/模板/分词/预算；可选prompt/IDs；不预留资源或保证执行 |
| GET /v1/config | SRV-07/02b/12b/02i限定已验 | 实际容量/来源、准入/等候、连接分区及媒体解码/函数能力；私有v1 schema/指纹随合同更新，固定消费者需选版本；不暴露模型目录/凭据 |
| 请求准入 / slot队列 | SRV-02b限定已验 | chat/preflight共享上限，准备后的容量FIFO；满队列503或已开SSE的唯一错误终态；非每租户公平或RAM预留 |
| 全局内存准入 | 02c/02d/02e限定已验 | MemAvailable门限/滞回/失效拒绝；同PID真实压力恢复、64轮包络及启动三检查点；不是物理RAM预留/硬限额或长稳 |
| 设备/host资源错误 | 02f至02i限定已验 | 已知OOM恢复，未知设备/释放错误停服，host固定错误封套，视觉加载失败拒绝启动；不覆盖所有外部库故障或物理OOM |
| 机器schema | SRV-08限定交付 | 38字段/10路由OpenAPI加必需原生语义清单；396 CPU差分，实际25 wire/56封套；标准schema本身不证明动态准入 |
| Responses / Assistants / Batch | 未实现 | 不能通过更换 base_url 使用 |
| 图片 / 视频 / 音频 | 非首批文本兼容范围；图片/两帧实验限定通过 | 02h+02i红图/两帧、准入/配额与取消恢复已验；需显式无MTP媒体配置，不扩广泛质量；音频未支持 |
| OpenAI Python SDK | 3.42.1 限定互通通过 | models、JSON/SSE、工具往返、预算与参数错误；关闭自动重试 |
| OpenAI JS SDK | 7.47.0限定HTTP通过 | Node24.18.0；models、JSON/SSE、真实工具成功/错误/双结果、截断后显式答案恢复；完整终态批次门，零自动重试 |
| 其他 SDK / 框架 | 未验证 | 不从 Python / JS 或自有客户端通过外推 |
| 鉴权 / TLS / 多租户隔离 | 项目网关与runner客户端预算限定已验；runner不内置TLS | 独立Key/客户端状态域；新托管v4证书有效期退场、同CA换证已验；[10f只读TLS/本机告警](SERVE_TLS_OBSERVATION_RESULT_2026-11-04.md)已验，实际LAN/正式渠道及长期公平性待验 |

实现入口：[请求合同](../src/server/chat_contract.cpp)、
[能力声明](../src/server/chat_capabilities.cpp)、
[HTTP](../src/server/chat_http.cpp)、
[响应](../src/server/http_response.cpp)、
[客户端](../tools/examples/chat_client.py)、
[恢复判定](../tools/examples/tool_recovery.py)。
已有接受证据：[聊天业务](CHAT_BUSINESS_ACCEPTANCE_2026-11-02.md)、
[正式交付](CHAT_DELIVERY_2026-11-02.md)。

## 冻结阶段

P0：完成以上盘点、明确接入子集和场景，保留基线身份。

P1：补 max_completion_tokens 与 max_tokens 共用预算；统一 HTTP / SSE
错误封套，状态分类与稳定 Q4T code；客户端暴露错误字段；能力声明和
协议源码身份覆盖对应实现。保留不支持参数的显式拒绝，不引入假约束。
参照 vLLM 盘点新增首批项：SSE created 请求身份保持一致，未实现的
嵌套消息语义显式拒绝（允许 name/refusal/audio/annotations/function_call
为null，拒绝其他值；developer后续B1已按单槽profile适配，reasoning别名后续05a已交付）。

P2：新增有界项目资料查询应用，工具只读本仓库允许范围内的真实文件，
返回路径、行号和内容身份；模型输入不作为任意文件 / 命令 / URL 执行。
完整工具批次检查后执行；预期工具业务错误回填，传输失败与不完整终态
停止且保留记录。复用持久记录和只读恢复检查，不引入未知结果自动重放。
使用具体 OpenAI Python SDK 版本做本地互通验证，与自有客户端分开记录。

P3 第一组（已交付）：required/named/none、parallel=false 和有限
schema 解码；18 生成 / 12 拒绝 / 3 启动通过。受约束 SSE 完整验证后
发布，单槽 / full head / 8192 容量有界验收，不使用 MTP / packed decode，
cache / media 组合拒绝。CMake 重建须 Q4T_ENABLE_XGRAMMAR=ON。

后续独立阶段：推理字段/隐藏/预算、停止条件、请求预检、可恢复
任务状态机、网关鉴权和租户配额；分别冻结业务合同与验收。

## 本轮验证与交付合同

本轮为业务功能 / 缺陷修复，不作性能优化收益声明。先成组开发与 CPU
合同，再进行一次有界真实 HTTP 业务验收；只因具体失败重验受影响项。
GPU 阶段完整驱动持共享锁；检测竞争 / 监控失败为 ENV_ABORT，停止自有
进程并保留现场。结束后 GPU 释放，不启动常驻服务。

原冻结预算：最多 3 次模型启动（候选、MTP 配置、安装健康）、16 次生成、
12 个负例 HTTP、30 分钟 GPU 阶段；原始请求 / 响应 / 工具结果 / 模型
进程日志均留存。验证文本质量、工具事实与引用、预算生效和错误分类；
不重跑已通过且身份未变的内核数值、模板和性能矩阵。交付前保留旧
二进制 / sidecar、候选源码身份、构建配置和可核验回退入口。

驱动：`tools/application_service/stage.py prepare|execute|finalize`。
prepare 不加载模型；execute 必须由同一个 gpu_exclusive 驱动持锁。
失败不隐式重试；安装后的完整 / 部分已知 pair 均可恢复。候选与旧
程序 / sidecar、真实 CMakeCache、运行时 manifest、源代码 bundle
及只读封条放在 `.q4t-work/application-service-20261102/package/`。
安装前持久化 `build/application-service-recovery.json`，guard 成功
后 finalize 才清除；两文件分别原子替换，不声称断电事务。

CPU 先验：138 host、22 客户端、13 恢复、9 项目工具、6 回退、3 SDK
mock 合同通过。mock 仅验证 SDK 驱动，不替代后续真实 HTTP。
默认全 head / MTP k3、缓存和 suffix 的启用策略不因服务补齐改变。

首轮实际失败与定向修正：SDK 文本 JSON/SSE、1-token 上限、8 参数
负例与 1 路由负例通过；发布查询成功。历史验收查询连续查证，在
3 轮上限前未给答案，驱动安全停止（8 生成 / 9 负例 / 1 启动），
未安装、资源归零。失败原始记录和原封包保留，不能计为业务通过。
修正应用工具结果，显式提供剩余请求 / 工具预算和最后回答提示；
历史查询上限为 4 轮，仍不保证模型一定遵循提示，超限继续拒绝执行。
只重验受影响的应用查询与剩余 MTP / 安装健康，SDK 已通过证据按同
二进制、协议、SDK 版本和相同解析方法 AST 复用。
此次具体失败修正允许累计最多 4 启动 / 20 生成；负例上限 12，原
07:41:21 UTC 开始的 30 分钟截止不重置。独立修正包与执行路径为
`.q4t-work/application-service-correction-20261102/`，入口加 `--correction`。

修正业务已通过（9 生成 / 2 启动），安装健康也通过（1 生成 / 1
启动），但末尾 SDK venv 的独立 NVML worker 缺 pynvml，流程自动
恢复旧 pair。此项记 ENV_ABORT_FINAL_MONITOR_IMPORT，原 guard 的
COMMAND_FAILED 保留；不能把查询错误当 GPU 为空。修复固定使用
系统解释器并在启动前检查，3 项失败 / 回退 / finalize 合同通过。
使用 `tools/application_service/finish_delivery.py` 封存已通过身份
和旧现场，持租约重新安装、核验 pair 与前后 NVML，再等 guard 成功
后 finalize；不新增模型启动或 HTTP，原 30 分钟截止继续有效。

## 官方协议依据

2026-11-02 核对官方文档：
[Chat Completions 请求与输出](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、
[Function calling](https://developers.openai.com/api/docs/guides/function-calling)、
[错误与状态码](https://developers.openai.com/api/docs/guides/error-codes)。
max_completion_tokens 包含可见和推理 token；工具执行属于应用端；
不同模型支持的参数有差异。Q4T 的错误 code、取消、缓存与能力端点是
本地合同，不冒充 OpenAI 平台的所有行为。

## 首批交付历史

P0 盘点与 P1 协议修补已交付；P2 真实发布查询、历史验收引用及资料
不存在三场景闭环通过，OpenAI Python SDK 3.42.1 的限定接入通过。
该阶段默认 c3772d11 / sidecar c80f1f14，结束已释放 GPU；后续
外部服务恢复观察见结果文档。194 项 CPU / 驱动
合同、累计 18 生成 / 9 负例 / 4 启动及两次失败的修正边界，详见
[交付结果与275份证据](APPLICATION_SERVICE_RESULT_2026-11-02.md)。
P3 工具控制 / 有限结构化输出后续已在 957211c7 交付（见页首）；
其余规划仍未完成。自主搜索、实时状态、逐句事实验证和更多 SDK
不能由三项有界任务通过外推。
