# 当前状态（2026-11-16）

Serve总目标仍为 **active**。按[完整计划](SERVE_MASTER_PLAN_2026-11-08.md)
和[逐项任务表](serve-master-goals-v2.json)推进全部33项任务；阶段验证与
发行是实施边界，不能替代剩余能力。非性能改动仅做受影响行为的针对性
验证和必要业务短验，长稳实跑由用户负责。

## 当前实际默认

| 项目 | 当前值 |
|---|---|
| 发布 | rev134；服务包 `437e477c488e0d2b3f5cdb67cd150df379f73149b0900ef4fdb469c317351577` |
| Generation | `b9d80bcd9a0b80b1c856e97dbd6d9989b7be1cc9540818cefdeafc5ee87ee49f` |
| 引擎 | `a49b577738b704cf85c528735f564dacf5e8d1153a0fe6d3670a22f96a72e885` |
| 引擎发布记录 | `f8c1074459efccb60dddd47c112bd78bf58908d502c5acd710b02a321d5663b0` |
| Guardian | `383bcc63b26fe0911d1d99b455c1b4effc39b6f813e147a9cfbfb6bce5bf11dc` |
| 原生入口 | `8c669d808962d74612c1ae2e6cee7758d2c19bfba6bf1c938a582d69aa91585a` |
| 服务状态 | `STOPPED`；原生外层监护已配置enabled，累计启动0次 |
| 默认容量 | 单流、ctx8192、prefill2048、输出256、MTP k=3；inflight8/queue2 |
| 版本回退 | 已验旧控制/当前引擎包 `52c399d74f2885eec8fa754e105cfa342e3ae651f8db4553114cc85c87a5b40e`；保持新MCP能力 |
| 外部依赖 | 默认原生入口及自监护不要求nginx/systemd；MCP数据源、私网目标/凭据与TLS/CORS等按部署显式配置 |

[MCP发行与回退记录](evidence/serve-mcp-delivery-20261116/README.md)包含
17项STOPPED交付检查，54.136秒，0新增GPU。实际MCP的审批/拒绝、
原生隧道SSE、资料与资源读取、未知不重放、同PID普通推理已验；
主轻资格和共享程序/存储/配额的整树恢复分别有精确包证据。
Key/配额/core/outer字节保持，profile只绑定新资格，version新周期
保留前驱；旧默认程序和状态备份保留。自动版本恢复保留现代根，
人工整根回退到v12入口没有outer；不是独立旧引擎回退。
本次rev134代码已通过PR28实际合main（`35581f98`），精确head两组CI
均成功，[合并回执](evidence/serve-pr28-merged-20261116/README.md)已归档。
提供方原生适配、完整客户端矩阵、其他协议及可靠性组合继续；
后续agent配置修复尚待发行，未宣称完整OpenAI兼容或长稳通过。

PR #22基础已合main（`ea0ad39a`）。PR #23的`17d8dbe2`两组CI全部通过，已实际合main（`7d00316a`），
[合并回执](evidence/serve-pr23-merged-20261113/README.md)保留。其后namespace
提交也已实际合main（PR24，`79f6e391`），
[合并回执](evidence/serve-pr24-merged-20261113/README.md)保留。
异步实现已实际合main（PR25，`4e87a03f`），
[合并回执](evidence/serve-pr25-merged-20261113/README.md)保留。前驱rev118的
工具发现已发行并实际合main（PR26，`090f0d48`），
[合并回执](evidence/serve-pr26-merged-20261113/README.md)保留；程序调用已在rev126交付并通过PR27实际合main；全部master继续。

## 最近完成的实现和验证

[媒体版本回退驱动4项CPU首过](evidence/serve-media-version-driver-cpu-20261116/README.md)：
真实封包隔离导入、媒体启动、判定和预算通过。新派生保留控制包
73b45ed3待1启动/2生成/300秒真实资格，再对正式7a8e9cd2完成
2启动/4生成/1设备探测/600秒整树回退。rev134不变，全部master继续。

[视觉服务实际自动降级/恢复通过](evidence/serve-media-managed-business-20261116/README.md)：
3启动/12请求/205.133秒，192次NVML正常；真实RAM压力触发主→轻→主，
8.125GiB自有页全释放，视频冻结历史/一次工具执行/配额连续/晋升SSE
和普通Chat通过，控制树保持。实际受控包8fe97f50已验；正式候选
7a8e9cd2及版本回退仍须完成，rev134不变，全部master继续。

[托管视觉驱动首次失败已定位](evidence/serve-media-managed-driver-failure-20261116/README.md)：
1启动/1自检后，驱动间接导入生产包未携带的TLS夹具失败；尚无媒体
请求或RAM压力，持久STOPPED与自有资源清理完成。纯wire检查替代
测试夹具导入，实际sealed namespace判定CPU单项通过；引擎/包保持，
继续最多3启动/12请求/900秒原恢复场景，rev134保持、master继续。

[托管视觉恢复驱动/封包CPU首过](evidence/serve-media-managed-driver-cpu-20261116/README.md)：
5项CPU通过，四个原生服务程序零警告且字节与rev134相同。候选包
7a8e9cd2待实际恢复；继续最多3启动/12请求/900秒真实内存压力下的
主→轻→主、视觉冻结历史/工具/配额与SSE，rev134保持，全部master继续。

[媒体主轻真实资格通过](evidence/serve-media-profile-business-20261116/README.md)：
2启动/10请求/96.528秒，92次NVML正常；两侧均完成工具实际SHA闭环、
结构化输出、embedding和原VP8首尾识别。仅MTP开关不同，能力相等，
准入估算103887276952/93982841120字节。自有资源清理、rev134保持；
继续同能力托管恢复/发行及全部master，尚未认作自动恢复通过。

[混合文本/视觉实际业务通过](evidence/serve-managed-vision-business-20261116/README.md)：
1启动/4创建/68.710秒，66次NVML正常。前后文本各10步MTP，中间真实
视频和PDF普通解码且答案正确，同PID/计数/清理闭合，rev134保持。
[媒体资格/迁移38项CPU首过](evidence/serve-media-profile-contract-cpu-20261116/README.md)，
继续同cd640ccc最多2启动/10请求/900秒主轻真实媒体资格，再实际恢复
与发行；全部master仍active。

[托管视觉配置CPU首过](evidence/serve-managed-vision-cpu-20261116/README.md)：
9项C++/25项Python/7项来源负控通过，主机与引擎零警告。共同加载、
按请求媒体普通decode、版本2容量和独立观测、主轻媒体资格合同已实现。
候选cd640ccc尚待实际混合业务与恢复；最多1启动/4创建/600秒短验，
rev134保持，全部master继续。

[原生视频实际业务通过](evidence/serve-video-business-20261116/README.md)：
1启动/8创建/53.447秒，52次NVML正常；MP4计数、VP8 SSE、实际函数
VP9续答、MCP真实读取、删除后的冻结回放、损坏后同PID普通推理均过。
历史失败保留，自有进程/端口/锁清理，rev134未改。随后同候选的
[受影响入口短验](evidence/serve-prepared-source-business-20261116/README.md)
也通过：1启动/4创建/65.750秒、63次NVML正常；Chat PDF/WebP流式、
Responses真实项目文档与计数、同PID普通Chat闭合。继续
[托管视觉配置与恢复](SERVE_MANAGED_VISION_2026-11-16.md)，rev134保持，
全部master仍active。

[统一投影原业务](evidence/serve-video-projection-business-failure-20261116/README.md)
在VP8仍拒答：实际看见红/蓝，却将平均视觉组标签当作缺首尾帧的证据。
当前共用同一时间戳函数，并将每组标签对应到实际源帧的显示区间；
不改像素、采样、张量或均值。[7项CPU首过](evidence/serve-video-group-timing-cpu-20261116/README.md)，
主机/引擎零警告；随后最多1启动/8创建/
900秒原业务；失败未验收，rev134保持，全部master继续。

[通用说明业务](evidence/serve-prepared-input-trust-business-failure-20261116/README.md)
仍在VP8拒答，未验收；[相同输入JSON/SSE诊断](evidence/serve-video-transport-diagnostic-20261116/README.md)
结果完全相同，未观察到传输差异。当前修正纯视频的空文档包装，
共用模板层的私有数据说明接通Chat/Responses、计数、预检和Batch，
不插入额外历史消息。[16项CPU最终通过](evidence/serve-video-projection-cpu-20261116/README.md)，
主机/引擎零警告；继续最多1启动/8创建/900秒原业务；
原问题、refusal协议和答案判定保持，rev134不变，总目标继续。

[帧时长业务](evidence/serve-native-video-intervals-business-failure-20261116/README.md)
仍拒答，未验收；随后[三组固定诊断](evidence/serve-native-video-diagnostic-20261116/README.md)
均答对，指向“来源可信度/指令权威”语义与结构化输出的组合问题。
当前原生适配为真实准备文件/MCP结果加入通用数据说明，保留refusal
协议、原问题/答案、权限和像素；普通文本不添加。[5项不同CPU最终过](evidence/serve-prepared-input-trust-cpu-20261116/README.md)，
首轮MCP映射类型错误保留并针对性修复；主机/引擎零警告。继续
最多1启动/8创建/900秒原业务，默认rev134保持，总目标继续。

[视频首尾说明补验](evidence/serve-native-video-followup-failure-20261116/README.md)
仍失败：模型已识别末帧蓝色，但把5.5秒PTS误解为6秒结束前缺帧。
1启动/4创建后按失败清理，rev134不变；继续补齐实际帧显示时长、
worker传递、快照完整性和半开时间区间表达，不根据容器时长虚构帧。
[20项C++/7项来源负控已过](evidence/serve-native-video-intervals-cpu-20261116/README.md)，
主机/完整引擎零警告；之后最多1启动/8创建/900秒验证原问题，业务仍未验收。

[首次视频业务短验](evidence/serve-native-video-business-failure-20261116/README.md)
在1启动/4创建后失败：直接MP4、VP8 SSE和计数通过，工具VP9续答拒答。
原始首尾帧与media执行存在，45项NVML均OK、自有资源清理完成、rev134
保持。当前补充统一投影对实际视觉内容/首尾/采样遗漏的准确说明，
明确报告refusal；冻结4项C++/2项Python及最多1启动/8创建/900秒补验，
原问题与判定不变。[4项C++/2项Python和来源检查已过](evidence/serve-native-video-followup-cpu-20261116/README.md)，
主机/引擎零警告；仍须实际业务证明修复，不将失败转成兼容完成。

[视频业务驱动CPU准备](evidence/serve-native-video-business-driver-cpu-20261116/README.md)
已过：2项C++、5项Python实际TLS/请求/判定器、7项来源负控/712锚点，
主机/引擎零警告；能力发现区分编译、媒体配置与运行库检查。
候选e5e67af8已冻结，继续最多1启动/8创建/900秒SDK/模型
业务短验；本记录时0GPU、rev134保持，不把CPU通过当实际业务通过。

[统一视频输入CPU验证](evidence/serve-native-video-input-cpu-20261116/README.md)
已完成：38项不同C++（39次执行）、1项实际CLI封包、7项来源负控/712
锚点最终通过，主机/完整引擎零警告。Files、MCP、计数、工具角色、
审核和v3快照接线；跨文件/调用零余额阻止新解码。首轮计数夹具失败
保留并单项修复复验；0GPU、rev134保持。候选8dbab79f尚待实际业务/
恢复发行；PR29前驱2276928f两组远端CI均成功，不代表本批新代码已验。
继续SDK/模型业务、格式组合和全部master。

[原生视频worker](evidence/serve-native-video-worker-cpu-20261116/README.md)
已通过13项不同C++（26次执行）、7项来源负控/712锚点，最终主机/
完整引擎零警告；固定软件H.264/VP8/VP9真实帧与PTS、隔离/配额有证据。
首轮截断容器超时已修复，失败保留；0GPU、rev134保持。PR29旧head
一组CI的PID夹具竞争也已修复，1项本机CPU及前驱两组远端CI均过。
继续[统一视频文件输入](SERVE_NATIVE_VIDEO_INPUT_2026-11-16.md)的快照、
时序投影和共享预算，不能把worker通过当完整API或云账户业务通过。

Graph分页兼容已接线保留原URL的受保护续接、等价编码/OData路径及
认证提供方返回的用户/日历别名；[47项CPU首过](evidence/serve-mcp-graph-cursor-cpu-20261116/README.md)。
原工具/参数/授权与一小时期限绑定，复用现有密码组件，无新依赖。
来源7项/706锚点过、构建零警告，0GPU、rev134保持，真实账户仍待验。
继续MCP下载视频进入实际媒体准备，不能把MP4普通文件投影当完整业务。

Google Vids长耗时下载已接线一次发起、显式pending和原任务单次续接；
[30项CPU首过](evidence/serve-mcp-drive-lro-cpu-20261116/README.md)，7项来源
负控/705锚点过，主机和完整引擎零警告。认证下载URI留在worker内，
账户令牌只发固定Google API主机；无重试或模型重启，0GPU。
候选工作已建立[PR29](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/29)，
仍为draft，真实账户/业务/发行未完成；rev134保持，全部master继续。

Dropbox六工具候选已完成[25项CPU和1项实际CLI八提供方封包首过](evidence/serve-mcp-dropbox-cpu-20261116/README.md)，
主机/完整引擎零警告，来源7项负控/704锚点过，0GPU、rev134不变。
八提供方42工具均有原生候选实现；真实账户、特殊格式、业务组合及
可回退发行仍未完成。继续Google Vids异步下载与全部master。

Microsoft Graph四提供方20工具已实现候选，[40项不同C++、1项实际CLI
最终通过](evidence/serve-mcp-graph-cpu-20261116/README.md)，7项来源负控、
703锚点通过，最终构建零警告。OAuth与文件下载能力隔离，邮件列表
游标不能扩大到正文；原始失败及针对性补验保留。0GPU、rev134保持，
七提供方候选仍待真实账户/发行；继续Dropbox、特殊格式及全部master。

Drive五项、Gmail六项原生只读工具已进入候选：实际下载/Office导出、
分页与MIME正文/可选附件接线。[36项C++、1项实际CLI封包首过](evidence/serve-mcp-google-cpu-20261116/README.md)，
最终构建零警告、7项来源负控通过，0GPU。不新增链接密码库，系统
字符转换仅在隔离worker内使用。三个提供方仍待真实账户业务/发行，
继续Graph/Dropbox及特殊格式，不将本机夹具称为云账户实际验收。

[原生提供方适配](SERVE_MCP_PROVIDERS_2026-11-16.md)的Calendar五项
只读工具、固定主机、OAuth权限目录、时区/游标及错误处理已实现；
[28项C++、14项Python/实际CLI最终通过](evidence/serve-mcp-provider-calendar-cpu-20261116/README.md)，
主机/完整引擎零警告、7项来源负控通过，0GPU。首轮小数秒与恢复
夹具失败保留并补验。实际账户业务和可用发行仍待完成；继续其余
七提供方及全部master，八提供方42工具没有缩减。

[隧道agent配置一致性修复](evidence/serve-mcp-agent-config-cpu-20261116/README.md)
已完成：11项不同C++、5项实际CLI、7项来源负控最终通过；显式非零
重定向不再静默覆盖，check使用实际worker序列化预算，避免检查成功
而启动失败。首轮夹具/来源门禁失败保留，针对性补验；0GPU。候选
9cf9af38独立保留、尚待发行；rev134默认不变。PR28此前交付已合入，
本修复随提供方原生适配继续交付，全部master任务保持active。

以下保留各阶段发生时的默认身份；当前已交付身份以上表为准。

[MCP能力包的受控恢复](evidence/serve-mcp-managed-20261116/README.md)
已通过：回退包1启动2业务，整树切换2启动4业务1探测；Guardian保留、
控制器/模型/入口换代，原程序/调用/schema/存储与预算保持，实际资料
只读1次并SSE续答。MCP远端业务与会话CPU证据分别绑定，未混称为
同一端到端故障场景。累计6启动22请求（含2embedding）1探测，全部
清理；主包437e477c、回退52c399d7待STOPPED发行，rev126仍保持。

[MCP恢复合同与主轻资格](evidence/serve-mcp-recovery-contract-20261116/README.md)
已通过：同一引擎主/轻2启动8业务、88.606秒；随后修复MCP能力迁移
和自动回退私有策略保持，7项CPU首过、来源负控最终通过。4个helper
零警告构建，3个字节不变；引擎和编译来源不变，业务资格明确复用。
当前累计3启动/16业务；继续封包与受控恢复短验，rev126保持。

[MCP真实模型与SDK业务](evidence/serve-mcp-business-20261116/README.md)
已通过：1启动/8创建/89.225秒，直接HTTPS审批/拒绝、原生隧道SSE、
实际资料与资源读取、未知结果不重放及同PID普通推理；84次NVML正常。
本机进程/端口/锁已清理，现代持久会话按续接合同保留。首次入口缺少
继承租约参数在0启动/0请求时失败，原件保留；随机凭据和私钥不公开。
默认rev126保持，继续精确主轻资格、恢复发行/main及全部33项master。
[驱动CPU证据](evidence/serve-mcp-business-driver-cpu-20261116/README.md)复用。

- [隧道公开合同与可选前置22项CPU最终通过](evidence/serve-mcp-tunnel-contract-cpu-20261116/README.md)：
  三条OpenAPI/schema、两种可选nginx的TLS/受保护头与独立准入已接通；
  修复存储Chat文件消息引用缺口。首次失败保留，来源7项过，复用未变
  C++构建；0GPU、rev126保持。继续SDK/真实业务、提供方、恢复发行及全部master。

- [自有agent 32项CPU及4项实际CLI最终通过](evidence/serve-mcp-tunnel-agent-cpu-20261116/README.md)：
  固定私网HTTP/SSE、真实broker往返、TLS入口/资料、会话清理及
  35秒监护/重启预算已验证。鉴权终态与unknown传递缺口已修复；
  实际CUDA映射超过512MiB，MCP/agent改为既有1GiB虚拟上限。
  首次失败保留，最终主机/引擎零警告、来源7项过；0GPU、rev126
  保持。继续提供方/SDK/业务/恢复发行/main，完整隧道与master未完成。


- [原生隧道隔离传输38项CPU最终通过](evidence/serve-mcp-tunnel-transport-cpu-20261116/README.md)：
  原权限/加密会话/实例亲和、审批/资源/程序调用、已知终态和清理
  已接线；头转换和会话schema遗漏已修复，首次失败保留。来源7项、
  主机/完整引擎零警告，0GPU、rev126保持；继续自有agent真实HTTP/
  SSE、SDK/业务及恢复发行，完整隧道与全部master尚未完成。

- [原生隧道接入26项CPU、5项入口/封包首过](evidence/serve-mcp-tunnel-api-cpu-20261116/README.md)：
  native配置/私有绑定、受保护头、真实TLS和独立poll/response容量
  已验证；来源7项及主机/完整引擎/ingress零警告，0GPU、rev126保持。
  继续隔离MCP传输与agent接线及完整交付，不能把入口当完整隧道。

- [原生隧道broker 16项CPU通过](evidence/serve-mcp-tunnel-broker-cpu-20261116/README.md)：
  单次取走、精确实例/token/作用域、未知不重放、进度/终态与容量
  已验证；7项来源负控、主机/完整引擎零警告通过。映射远端CI成功，
  0GPU、rev126保持；继续HTTP/实际MCP/私网agent接线与完整交付。

- [MCP部署映射29项CPU最终通过](evidence/serve-mcp-mapping-cpu-20261116/README.md)：
  独立网络/作用域、原审批/资源目标、离线冻结及程序/Batch实际HTTP
  已验证；执行策略快照遗漏已修复，首次3项失败保留。8项原生
  策略/封包、7项来源负控及完整引擎零警告通过，0GPU、rev126保持。
  继续原生隧道/提供方适配及完整业务交付，不能把映射当完整隧道。

- [MCP会话实际执行41项CPU最终通过](evidence/serve-mcp-sessions-execution-cpu-20261116/README.md)：
  加密目录、原调用/资源绑定、审批/冻结重启、凭据更新与资源失能
  持久化已验证；首次2项夹具失败保留并修复。绝对期限阻止慢发布/
  许可后派发；7项来源负控及完整引擎零警告，传输远端CI通过。
  0GPU、rev126保持；继续映射、SDK/真实业务/恢复发行及全部master。

- [原生会话传输30项CPU首过](evidence/serve-mcp-sessions-transport-cpu-20261116/README.md)：
  有状态列表/调用/资源、现代恢复、旧SSE连续、隔离与过期通过；
  7项来源负控和完整引擎零警告，0GPU、rev126保持。计数远端CI
  的四项事务恢复失败已定位并针对性补验通过，失败保留；继续
  加密目录/调用接线及完整MCP与全部master交付。

- [计数统一26项CPU首过](evidence/serve-mcp-count-cpu-20261116/README.md)：
  MCP图像与生成输入一致、多份CSV精确额度及冻结复用、新目录
  只读元数据和全部计数直接合同通过；主机/完整引擎零警告，
  7项来源负控通过。0GPU、rev126保持；继续会话/映射与全部交付。

- [MCP实际媒体执行20项CPU最终通过](evidence/serve-mcp-media-execution-cpu-20261116/README.md)：
  实际HTTP图像/文档/资源、冻结续接、共享预算、晚到sheet原配额
  和历史资源清理责任已接线；首次媒体夹具配置失败保留，补单项
  通过。主机/完整引擎零警告、7项来源负控通过、0GPU、默认保持。
  继续计数统一、会话/映射与实际模型/恢复发行，master active。

- [MCP共享媒体准备14项CPU首过](evidence/serve-mcp-shared-media-cpu-20261116/README.md)：
  图像/文档进入模型和审核类型，跨调用预算、CSV持久准备/重启
  续接与截断已验证；主机/完整引擎零警告，7项来源负控最终通过。
  0GPU、rev126保持；继续实际执行器/计数及晚到sheet模型入场。

- [MCP模型媒体投影12项CPU首过](evidence/serve-mcp-projection-cpu-20261116/README.md)：
  真实图片/文件准备、持久来源绑定、重启无codec回放及依赖回收
  通过；主机/完整引擎零警告、7项来源负控通过。0GPU，rev126
  保持，762ab1aa远端CI通过；继续实际执行/审核/计数/共享预算及交付。

- [MCP资源冻结16项CPU首过](evidence/serve-mcp-resource-freeze-cpu-20261116/README.md)：
  原调用/目标/权限绑定、单次读取、重启冻结、精确拥有者和共享
  容量已验证；为在途失败预留收尾空间。主机/完整引擎零警告，
  0GPU、默认保持，继续模型媒体准备和全部交付。


- [MCP原生内容与资源读取26项CPU最终通过](evidence/serve-mcp-content-cpu-20261116/README.md)：
  五类内容、资源许可/已知与未知结果、原生错误结构化数据恢复和
  JSON/SSE发布已接线，官方SDK新增1项通过。主机/完整引擎零警告，
  043e5786远端CI全部通过；0GPU、rev126保持。模型媒体准备/资源冻结等继续。


- [程序/MCP媒体边界复核8项CPU首过](evidence/serve-mcp-program-boundary-cpu-20261116/README.md)：
  恢复公开批次的媒体校验边界，文件准备仅映射客户端子调用，避免
  空call_id触及MCP私有结果；完整引擎零警告、0GPU、rev126保持。
  继续MCP媒体/资源、会话/映射及完整业务发行，master保持active。

- [MCP程序子调用49项CPU最终通过](evidence/serve-mcp-program-cpu-20261116/README.md)：
  原生派发、审批续接、混合工具因果顺序、原schema/权限、计数和冻结
  恢复已验证；主机/完整引擎零警告、0GPU、rev126保持。继续媒体、
  会话/映射、完整SDK及真实业务、恢复发行与主线，master保持active。

- [CI正常恢复期限修正](evidence/serve-ci-recovery-deadline-20261116/README.md)：
  277257f6远端4项失败保留；100/250ms只用于故障注入，正常恢复
  保持生产默认期限，4项本机补验通过。继续后续head远端CI确认。

- [MCP存储故障清理续办18项CPU通过](evidence/serve-mcp-cleanup-cpu-20261116/README.md)：
  派发前登记有界元数据，stateless收尾失败继续保留责任；已确认
  进程存活/存储恢复阻塞时普通请求继续，恢复后收尾不重放。主机/
  完整引擎零警告、0GPU、rev126保持；继续程序/媒体/会话/映射及交付。

- [MCP拥有者恢复21项CPU首过](evidence/serve-mcp-recovery-cpu-20261116/README.md)：
  真实进程退出、精确请求清理、未知不重放、已知结果保留和异常续接
  已验证。主机/完整引擎零警告、0GPU、rev126保持；继续存储短暂
  失能后的清理续办及程序/媒体/会话/映射、业务与发行，master active。

- [MCP与Prompt组合11项CPU首过](evidence/serve-mcp-prompt-cpu-20261116/README.md)：
  登记不联网/不授予调用，执行时校验认证目录；当次凭据、冻结版本、
  Batch来源及延迟发现已覆盖。主机零警告、0GPU、rev126保持；继续
  失去拥有者后的调用收尾、程序/媒体等及真实业务发行，总目标active。

- [MCP发现/权限及开放schema23项CPU最终通过](evidence/serve-mcp-discovery-cpu-20261116/README.md)：
  延迟目录实际激活、客户端新定义、权限刷新及故障后续请求恢复；
  修复strict混合工具错误封闭MCP参数，原schema由独立worker校验
  并绑定回执。完整引擎零警告、0GPU、rev126保持，其他组合继续。

- [MCP Batch来源鉴权12项CPU最终通过](evidence/serve-mcp-batch-cpu-20261115/README.md)：
  原始File行与准备身份绑定、重启后计数/真实本地派发、凭据隔离及
  缺凭据不联网已验证；完整引擎零警告构建、0GPU、rev126保持。
  继续目录权限/发现/程序/媒体/恢复等组合及真实业务和发行。

- [向量任务CI夹具期限修正](evidence/serve-ci-vector-deadline-20261115/README.md)：
  正常IO恢复生产默认期限，4处故障注入保留100ms；6项相关CPU首过，
  生产/default未变、0GPU。旧head的4项失败保留，继续最新head检查
  及MCP组合实现，总目标仍active。

- [MCP实际执行拥有者22项CPU首过](evidence/serve-mcp-execution-cpu-20261115/README.md)：
  本地HTTP调用、审批/续接/不重放、冻结目录与凭据隔离、故障后文本
  续答和审核组合已验证；7项来源负控通过、0GPU、rev126保持。
  Batch/程序/媒体/恢复等组合及SDK/真实模型业务继续，尚未发行MCP。

- [MCP请求规划与历史组合19项CPU最终通过](evidence/serve-mcp-planning-cpu-20261115/README.md)：
  工具发现按server_label、原schema/选择、审批/失败历史及截断/压缩
  依赖已接线；首次两项夹具失败保留，修后仅补2项。7项来源负控
  通过、0GPU、rev126保持；继续拥有者凭据/持久准备和实际执行。

- [MCP模型绑定及认证历史13项CPU首过](evidence/serve-mcp-binding-history-cpu-20261115/README.md)：
  原schema、标准可选字段、调用ID续接及原子审批索引已验证；7项
  来源负控通过、零警告，0GPU、rev126保持。继续实际公开执行链
  和全部master，尚未交付MCP接口。

- [MCP输出与派发拥有者16项CPU、2项SDK最终通过](evidence/serve-mcp-output-owner-cpu-20261115/README.md)：
  参数逐片事件、截断/取消形状与并发失败不覆盖在途结果已验证。
  公开执行/历史和组合仍在接线，尚无MCP对外交付；0GPU、rev126
  保持，继续全部master。

- MCP公开输出部件10项CPU及2项官方SDK最终通过；继续接实际
  Responses执行器。并发复核补充派发身份，防重复请求失败覆盖
  在途结果，正在验证直接受影响项；0GPU、rev126保持。

- [MCP目录与持久审批20项针对性CPU最终通过](evidence/serve-mcp-authority-cpu-20261115/README.md)：
  单次派发、未知不重放、原schema及凭据不存储验证通过；首次清理
  夹具失败保留，修后单项通过。0GPU、rev126保持；继续公开MCP
  items/SSE与状态/程序/真实业务，全部master仍active。

- MCP目录/凭据/过滤7项及受影响传输头1项CPU首过；正在接认证
  目录、审批和单次派发的持久状态，尚未开放MCP对外能力。0GPU、
  rev126保持，真实业务与全部master继续。

- PR27精确head `9e0a4b44` 的两组CI全部通过，已[实际合main](evidence/serve-pr27-merged-20261115/README.md)
  （`1f0ed01a`）。程序调用rev126已交付；MCP仍在后续开发中，
  继续审批/公开接线及全部剩余master任务。

- [原生MCP传输部件21项针对性CPU最终通过](evidence/serve-mcp-transport-cpu-20261115/README.md)：
  JSON/SSE/旧传输、单次派发/取消、未回收隔离及HTTPS真实资料读取；
  7项来源负控通过。0GPU、rev126保持；审批/公开接线和业务发行
  继续，不能把传输部件通过当作MCP完整交付。

- [原生MCP实施清单](SERVE_MCP_2026-11-15.md)已固定，正在开发传输、
  审批和实际执行；默认rev126保持。PR27最终head检查进行中，
  全部33项继续，MCP未接线/未验收前不宣称交付。

- 原生程序调用已[可回退发行rev126](evidence/serve-programmatic-delivery-20261115/README.md)：
  17项STOPPED检查全过，实际回退再前进，密钥/配额/core/outer及
  恢复预算保持，0新增模型启动。PR27待最终检查和实际合main；
  剩余协议/工具/模型后端及恢复组合继续，未结束总目标。


- 程序调用受控续答缺陷已[通过真实补验](evidence/serve-programmatic-qualification-20261115/README.md)：
  新引擎主轻2启动8业务、独立回退1启动2业务、整树恢复2启动4业务
  1探测均通过。切换后保留原程序/caller和schema，真实资料只读一次，
  VM恢复后模型成功输出最终SSE答案；自有资源清理。原失败仍保留，
  默认rev118保持，接着17项停止态可回退发布与PR27实际合main；
  剩余全部master任务继续。

- 首次入场前程序续接配额已[修复，26项针对性CPU通过](evidence/serve-programmatic-resume-rate-cpu-20261115/README.md)：
  重新预留未入场提示，保留RPM、输出预算、已发生用量与Batch额度；
  实际引擎d7dd0c6d及主机零警告构建，4个原生helper字节保持。
  默认rev118未改；新引擎资格及受控续接/恢复补验上限5启动14业务
  1探测1800秒，含原失败资格组累计上限10/28/2/3600秒。不跑全量
  性能/质量，业务闭环尚未通过前不发行，全部master继续。

- 程序续接[整树恢复业务未通过](evidence/serve-programmatic-resume-failure-20261115/README.md)：
  控制树和持久调用恢复成功，但受控客户端在首次模型入场前续接程序，
  误用要求已入场的输入追加接口，最终响应503。2启动4业务1探测，
  自有资源已清理、rev118保持。正在修正未入场输入的重新预留，保留
  RPM/输出预算/既有用量，并验证26项直接相关CPU；不关闭配额绕过。

- PR27的无XGrammar构建断言已[修复，两种构建针对性CPU均通过](evidence/serve-programmatic-ci-grammar-20261115/README.md)：
  ON保持regex/Lark实际派发与拒绝，OFF验证零派发及明确错误；生产
  源码/引擎和rev118未变，已有业务及主轻资格复用。继续精确回退包、
  整树恢复、可回退交付及实际合main。另依据PR23/24实际合并回执
  修正N05/R01滞后的任务状态；剩余全部master任务继续。

- 程序调用[真实资料JSON/SSE闭环通过](evidence/serve-programmatic-business-20261113/README.md)：
  修后1启动/8创建/121.786秒，115次NVML正常、资源清理；含首次失败
  累计2启动/9创建/159.939秒。原schema、caller和持久续接保持，实际
  两次读取资料，纯计算/截断/普通业务均通过。PR27已建；继续主轻与
  程序等待期间整树恢复、可回退发行及合main，rev118保持，master继续。

- 大长度语法失败已CPU复现并[修复，11项针对性检查通过](evidence/serve-programmatic-length-repair-20261113/README.md)：
  紧凑计数规则保留min/max、Unicode及JSON转义；不删除约束或放宽资源
  上限。实际引擎零警告构建，补验固定1启动/8创建/900秒，连原失败
  累计上限2启动/9创建/1200秒；业务未重验前不交付，rev118保持。

- 程序调用[首次真实短验失败](evidence/serve-programmatic-business-failure-20261113/README.md)：
  1启动/1创建/38.153秒，首条请求在语法编译阶段返回资源限制，未执行
  程序或客户端工具。自有进程/端口/锁清理，默认rev118保持；正在CPU
  复现大字符串长度约束的编译问题，修复后补原业务，不以失败归档结案。

- 程序调用公开接入[54项不同C++、2项官方SDK及7项Python检查最终通过](evidence/serve-programmatic-integration-cpu-20261113/README.md)：
  授权派发、JSON/SSE、认证续接、媒体冻结/审核/共享预算及异步同步
  组合已验证。媒体临时引用和混合同步等待缺口已修复，失败原件保留。
  实际引擎零警告构建已通过，冻结1启动/8创建/900秒资料闭环，再完成
  适用恢复资格与发行；0GPU、rev118保持，全部master继续。

- 程序调用公开执行链正在接线：program-only工具从直接生成通道移除，
  原生程序输出及caller进入JSON/SSE，持久结果合同从认证快照绑定，
  历史/压缩保留程序依赖；续接进入原共享预算、审核与终结流程。
  这组尚未验证或发行，媒体组合等仍需补齐；rev118保持，全部master继续。

- 程序调用[认证/回放部件19项不同CPU最终通过](evidence/serve-programmatic-authority-cpu-20261113/README.md)：
  跨客户/模型/代码篡改拒绝、调用时参数/结果合同、结果因果回放、
  custom文本/regex/Lark、重启及取消/过期已验证。两项适配失败原件
  保留，仅重验受影响用例；0GPU、rev118保持。继续公开生成与
  JSON/SSE/持久接线、实际业务及全部master，不以部件通过结束总目标。

- 程序调用的授权/认证续接正在接线：调用时工具及参数/结果合同写入
  客户/模型/模板绑定的加密指纹，校验子调用身份和结果因果顺序，
  custom regex/Lark在派发前进入既有隔离校验worker。9项新增及
  直接受影响CPU准备成组验证；公开接口、业务及发行仍待完成，
  rev118保持，继续全部master。

- [程序调用隔离执行部件](SERVE_PROGRAMMATIC_TOOLS_2026-11-13.md)已通过
  [13项针对性CPU](evidence/serve-programmatic-worker-cpu-20261113/README.md)：
  真实JavaScript、并行/依赖续接、结果顺序、资源限额和回收验证通过；
  构建零警告、0GPU、默认rev118保持。公开授权/持久续接/执行接线、
  真实业务与发行仍待完成，不能算对外能力已交付；全部master继续。

- PR26精确head `1282ece2`的push/PR两组CI均通过，已实际合main
  （`090f0d48`）；[回执](evidence/serve-pr26-merged-20261113/README.md)保留。

- PR26旧head一次CI暴露正常Batch恢复误用150ms故障期限；已将三项
  同类夹具的正常/注入期限分开，[7项针对性CPU首过](evidence/serve-ci-batch-recount-deadline-20261113/README.md)。
  生产和rev118字节未变，0GPU；继续最新head检查及实际合main。

- 原生工具发现已[可回退发行rev118](evidence/serve-tool-discovery-delivery-20261113/README.md)：
  17项STOPPED检查全过、0GPU，实际回退再前进，密钥/配额/core/outer
  及恢复预算保持。PR26待检查与实际合main；继续程序调用及全部master。

- 工具发现[主轻、独立回退与整树恢复均通过](evidence/serve-tool-discovery-qualification-20261113/README.md)：
  5启动14业务1探测，244次NVML正常、资源清理；真实资料结果在切换
  后继续闭环。只做17项STOPPED可回退交付，随后实际合main；全部
  master继续，默认rev110尚未改变。

- 回退准备暴露工具发现输出枚举遗漏；已[修复并通过10项CPU及实际资格迁移](evidence/serve-tool-discovery-migration-cpu-20261113/README.md)。
  主轻2启动8请求已通过；引擎/原生helper不变，继续原定独立回退与
  整树恢复短验，再可回退发行和实际合main；默认rev110保持。

- 工具发现[真实业务补验通过](evidence/serve-tool-discovery-business-20261113/README.md)：
  1启动/10创建/104.447秒，server函数JSON/custom SSE、client目录外
  定义及additional_tools四次实际读取闭环通过，错误结果400拒绝。
  98次NVML正常，资源清理；连原失败累计2/11/144.311秒。PR26已建，
  继续主轻/恢复资格、可回退发行及实际合main；默认rev110保持。

- 已用两项大词表CPU负控复现工具发现失败：仅检查拒答的guard重复
  扫描XML候选耗尽工作额度，误报内存。[修后12项针对性CPU通过](evidence/serve-tool-discovery-guard-repair-20261113/README.md)，
  grammar/拒答/停止/schema检查及资源上限保持。补验固定1启动10创建，
  含原失败累计2启动11创建1200秒；默认rev110未变，全部master继续。

- 工具发现[首次真实短验失败](evidence/serve-tool-discovery-business-failure-20261113/README.md)：
  1启动/1创建/39.863秒，首请求触发grammar资源错误。39次NVML查询
  正常，进程/端口/锁已清理，默认rev110保持。正在CPU复现语义guard
  工作预算与内存错误的混淆，修复后补原业务闭环，不把失败归档当收口。

- 工具发现公开执行链的[77项不同CPU、4项SDK/驱动检查最终通过](evidence/serve-tool-discovery-integration-cpu-20261113/README.md)：
  原位置加载、真实派发、持久结果合同、异步/推理续答、裁剪压缩及
  JSON/SSE接线已验证；失败及修正原件保留。补齐19项协议来源漏登，
  增加来源遗漏检查。接着冻结1启动10创建900秒真实业务；默认rev110
  保持，尚未宣称GPU通过或对外交付，总目标继续。

- PR25两组CI全过并已实际合main（4e87a03f）。后续[目录/历史加载部件10项CPU通过](evidence/serve-tool-discovery-catalog-cpu-20261113/README.md)，
  失败及修正原件保留；尚待公开执行器/模板/状态接线，不能视为
  tool_search已可用。继续接线和全部master；默认rev110保持，0GPU。

- [工具发现实施清单](SERVE_TOOL_DISCOVERY_2026-11-13.md)已固定：server/client
  搜索、additional_tools、按历史位置加载与权限/结果约束持续性。
  正开发原生目录及执行接线，未宣称完成；默认rev110保持。异步
  PR25（be7c2236）两组CI已过并合main，新开发保持独立。

- 异步工具已[可回退交付rev110](evidence/serve-async-tools-delivery-20261113/README.md)：
  17项实际STOPPED检查全部通过、0GPU；回退再前进、密钥/配额/
  core/outer及profile预算保持，旧默认备份保留。继续实际合main，
  随后延迟发现、程序调用及全部master，未以本阶段结束总目标。

- 发布准备发现工具历史源码哈希误作能力变化，已[修复并通过10项CPU、旧版负控及实际资格迁移](evidence/serve-async-migration-cpu-20261113/README.md)。
  默认未写入，rev102保持；仅更新主包控制模块，复用未改引擎与业务
  证据，继续17项实际停止态发布及全部master。

- 当前异步引擎的[主轻、独立回退与实际整树切换均通过](evidence/serve-async-resume-qualification-20261113/README.md)。
  5启动/14业务/1探测，三个阶段正常清理；原调用切换后仍可接收真实
  迟到结果。默认rev102保持，接着17项STOPPED可回退发行（0GPU），
  随后合主线并继续延迟发现、程序调用及全部master。

- 新引擎在回退载荷中已正确完成异步独立回答；验收比较空logprobs与
  GET默认投影时失败，[驱动修复及两项原文回放通过](evidence/serve-async-managed-projection-20261113/README.md)。
  原失败保留，仅补1启动/2业务/300秒，生产代码与已验主轻资格不变。

- 异步续答修复后的[完整JSON/SSE业务通过](evidence/serve-async-resume-business-20261113/README.md)：
  1启动/8创建/70.180秒，输出256与实际客户端边界一致，两类真实
  迟到结果均闭环。新引擎6d7d5e92正在重新取得主轻/回退资格；
  默认rev102保持，全部master继续。

- 异步续答调度及稳定历史引用[11项CPU最终通过](evidence/serve-async-resume-cpu-20261113/README.md)。
  原误答仍待真实补验；冻结1启动/8创建/900秒，JSON/SSE都按受控服务
  256输出预算验证。默认rev102保持，继续所有master。

- 异步候选[受控服务非流式短验失败](evidence/serve-async-managed-failure-20261113/README.md)：
  调用正确但续答仅省略号，1启动/2业务/50.010秒，正常清理。
  默认rev102保持；正在补内部续答回合及稳定历史引用，验证通过前不发布。

- 异步候选已取得[当前主/轻配置资格](evidence/serve-async-profiles-20261113/README.md)，
  2启动/8业务通过并清理，默认rev102保持；继续兼容回退载荷与真实
  整树切换验证。PR24已合main，async独立交付后继续全部master。

- A07异步工具[真实业务补验已通过](evidence/serve-async-business-20261113/README.md)：
  两类调用后同Response续答及custom真实迟到结果闭环通过，原函数结果
  证据显式复用；修后1启动/5创建/61.066秒，累计2/10/127.533秒。
  59次NVML全正常，模型/端口/锁清理；默认rev102保持。继续主轻配置
  资格及保持新能力的可回退交付、主线合入和全部master任务。

- 异步历史模板修复已[通过8项CPU及审计](evidence/serve-async-history-cpu-20261113/README.md)。
  下一轮冻结1启动/5创建/600秒，只补受影响路径；含原失败累计
  最多2启动/10创建/900秒，默认rev102保持。

- A07异步首轮[真实业务失败原件已保留](evidence/serve-async-business-history-failure-20261113/README.md)：
  函数闭环通过，custom续答误拒绝；1启动/5创建/66.467秒，资源已清理。
  定位到禁止新调用时历史工具定义被通用过滤器一起移除，正在修复。
  默认rev102保持，失败不改判，全部master继续。

- A07原生异步工具的[36项CPU、2项官方SDK及schema检查已通过](evidence/serve-async-tools-cpu-20261113/README.md)。
  补同Response独立回答、迟到结果、持久身份/计费和图片工具结果接入，
  失败原件保留；尚未真实模型验收或交付，默认rev102保持。继续
  冻结的1启动/8创建/900秒短验、PR24实际合入和全部master任务。

- PR24完整CI定位到custom内部Item/公开Conversation投影夹具错用，
  [6项针对性CPU通过](evidence/serve-ci-custom-views-20261113/README.md)。
  32输入及31会话形状均保留校验，生产源码/默认rev102未变；继续CI及实际合并。

- A07下一组已冻结[异步工具原生接线及验证计划](SERVE_ASYNC_TOOLS_2026-11-13.md)：
  补历史/模板/持续生成及迟到结果，不只接受async字段；程序调用与
  延迟发现保留连续任务。历史/模板/持续生成/绑定及配额已接线，12项新增与24项直接旧例
  已冻结，尚未执行；实际默认rev102保持。

- 工具命名空间/结果合同与控制器停止修复已[可回退发行rev102](evidence/serve-tool-identity-delivery-20261113/README.md)。
  17项停止态检查通过，实际回退再前进、原账本/恢复预算保持；
  原生现代根与兼容旧控制均具备新应用能力。继续PR实际合入及
  async/caller/延迟发现等全部master，不以本阶段结束总目标。

- 新工具合同与ObjectStore/CacheIo停止修复已取得[真实主/轻量资格](evidence/serve-tool-identity-profiles-20261113/README.md)，
  含同一存储会话跨重启读取/删除。兼容回退载荷的独立运行和
  [实际整树切换后工具闭环](evidence/serve-tool-identity-managed-20261113/README.md)也通过，
  合计3启动/6生成/1探测/165.913秒，全部资源清理。继续可回退发行；
  rev94保持，PR23最新17d8dbe2续验中，全部master仍active。

- PR23续验发现HTTP夹具把随机请求ID中的999误判成子用量泄漏；
  已确定性负控复现并只检查真实用量头。批任务正常存储与故障期限
  也已分开，[17项针对性CPU首过](evidence/serve-ci-trace-storage-20261113/README.md)。
  生产运行时与rev94未变；继续真实CI、合并和全部master任务。

- PR23续验定位到真实停止竞态：ObjectStore及session/prefix缓存可能
  丢失唤醒并永久占槽。[三项旧序负控复现、修后16项CPU首过](evidence/serve-controller-stop-race-20261113/README.md)。
  已将停止通知与等待条件用同一把锁；100ms等待及防增殖保护保持。
  0GPU、rev94保持，继续业务交付、实际CI/合并及全部master。

- PR23完整CI中的分页终结、正常存储与故障期限夹具已修复，
  [12项不同CPU最终通过](evidence/serve-ci-fixture-boundaries-20261113/README.md)。
  原失败及新增断言修正保留；生产预算未放宽，0GPU、rev94保持。
  继续真实CI、实际合并与全部master任务。

- A07命名空间/结果合同/标准custom语法的[真实业务已通过](evidence/serve-tool-identity-business-20261112/README.md)：
  修后1启动6请求66.366秒，含失败累计3启动10请求159.271秒。64次
  NVML全正常，模型/端口/锁已清理；继续主/轻资格与可回退发行，
  默认rev94保持，剩余A07及全部master继续。

- PR23的x86文档转换诊断已过；完整CI暴露64MiB上传容量夹具未检查
  错误回复的解引用。[补齐检查和容量IO预算后本机通过](evidence/serve-upload-capacity-fixture-20261112/README.md)。
  生产预算与故障用例未改，继续真实CI及合并，rev94保持。

- A07真实custom请求发现Responses/Chat grammar形状缺转换，已原生
  [补齐并通过7项针对性CPU](evidence/serve-responses-custom-format-20261112/README.md)。
  第二轮1启动1请求在生成前失败并清理；新引擎重验1启动6请求，累计
  上限3启动10请求，默认rev94保持。继续所有master任务。

- A07首轮函数调用/结果拒绝/真实资料JSON闭环通过；整体因SDK分页桥接
  丢游标FAILED，服务线包正确。[修复后6条CPU回放通过](evidence/serve-tool-identity-business-prefix-20261112/README.md)。
  首轮1启动3请求52.758秒已清理；只补custom的1启动3请求，rev94保持。

- A07真实引擎CPU worker暴露512MiB小于动态库映射，已改为有界1GiB，
  [实际正/负例及回收通过](evidence/serve-tool-result-worker-20261112/README.md)。
  复核候选源码审计锚点；继续1启动/6请求业务短验，默认rev94保持。

- PR23编解码修复的两个生产源码锚点已按实际diff复核并更新审计索引；
  清单与5项防遗漏负控通过，沿用16项CPU证据。继续真实CI及合并。

- A07工具命名空间/custom公开投影/调用时output_schema合同的
  [37项不同CPU及官方SDK/schema检查已通过](evidence/serve-tool-identity-cpu-20261112/README.md)。
  独立结果校验worker、存储续接及压缩历史已接线；冻结1启动/6请求/
  900秒实际业务短验，尚未执行或交付，默认rev94保持。其余A07及
  全部master缺口继续，不能把这组通过当总目标完成。

- PR23的实际退出已定位为x86旧mkdir未进有界IO代理；已补齐等价ABI，
  [16项针对性CPU通过](evidence/serve-codec-syscall-20261112/README.md)。
  文件创建仍计费、路径仍受Landlock限制，等待真实远端结果再合并；
  0GPU，默认rev94不变，继续全部master。

- A07命名空间19项不同CPU及custom公开投影两项已过，官方SDK与严格
  schema校验通过；继续调用时output_schema绑定和独立校验worker，
  冻结14项针对性检查。尚未真实模型验收或交付，默认rev94保持。

- PR23实际沙箱ready后仍有编解码退出，另三项存储/过期夹具
  [针对性CPU通过](evidence/serve-host-storage-codec-20261112/README.md)。
  原失败完整保留，远端增加有界系统调用诊断；尚未合并，rev94保持，
  继续全部master。

- PR23第二轮定位到CI的`/etc/localtime`实际mode0777及exec前父死亡
  夹具竞态；[18项Guardian针对性CPU通过](evidence/serve-host-localtime-exec-20261112/README.md)。
  临时CI路径权限与exec握手已修，生产沙箱/默认rev94保持，原两轮
  失败原件保留，等待真实远端通过再合并；全部master仍active。

- A07命名空间的定义/模型身份/JSON/SSE/历史及存储续接已成组编码，
  [结果合同的连续计划](SERVE_TOOL_IDENTITY_2026-11-12.md)保留output_schema
  与其余高级工具任务。冻结19项针对性CPU，尚未验收或交付；0GPU，
  rev94保持，PR23实际CI仍在运行。

- PR23剩余Batch夹具已[9项针对性CPU通过](evidence/serve-host-prerequisites-batch-20261112/README.md)，
  包含完整50000行/391轮/792对象；故障注入短超时不变，容量用例单独
  使用存储预算。CI的legacy资源诊断与信任权限修复已准备，等待真实
  远端结果；默认rev94保持，不宣称已合主线，继续全部master。

- R01取消清理、N05可选路由和配置schema已[可回退发行rev94](evidence/serve-cache-route-delivery-20261112/README.md)。
  缓存7请求同PID成功并恢复目标/MTP布局，含失败累计4启动15请求。
  11项STOPPED交付保持恢复预算。PR23仍修实际CI的Batch/legacy沙箱，
  尚未合主线；继续全部master任务。

- R01主/轻配置资格通过；缓存短验发现配置schema漏向量入库状态，
  [正式合同已补齐并校验3份真实响应及39负控](evidence/serve-cache-config-contract-20261112/README.md)。
  缓存业务尚未发请求，补验仅该项；累计上限4启动16请求2100秒，
  原资格复用、默认rev92保持，继续封包/CI/主线及全部master。

- Q02续修发现并实修R01取消清理等待2秒的问题，[24项针对性CPU通过](evidence/serve-public-host-repair-followup-20261112/README.md)。
  原200ms边界保持；公共测试目录、CI沙箱信任前提及旧合同夹具已修，
  继续实际CI与有界业务/封包，默认rev92保持，R01新增修复尚未交付。

- N05重审发现可选nginx入口漏同步29项后续API；[路由全集已补齐并通过7项CPU/实际双入口HTTP](evidence/serve-optional-route-parity-20261112/README.md)。
  当前66项公开API逐项闭合，续验主线/封包前保持待交付；原生默认不受影响。

- Q02 PR23公共检查的旧夹具/SDK准备已修复，[18项针对性CPU通过](evidence/serve-public-host-repair-20261112/README.md)。
  GitHub文档沙箱失败继续定位，尚未合并或宣称CI通过；0GPU、默认rev92保持。

- A11[原生资料检索已可回退发行](evidence/serve-retrieval-delivery-20261112/README.md)，
  默认rev92。实际检索、引用、历史/后台/schema、受监护版本回退及
  官方SDK原生TLS均通过；人工换根遗漏登记已修复并实际通过18项检查。
  MCP/其他内建工具仍未完成，继续全部master任务。

以下为各实施组的历史进展，当前发行结论以上方表格为准。

- A11实际发布已到rev88主包，但人工整根回退检查因驱动漏登记控制根
  恢复周期失败，保留原件，尚不宣称发行接受。已恢复已验主包，核心/
  客户端/outer/profile/version状态未被恢复操作改写。冻结最多20项
  STOPPED交接检查：明确换根、显式登记、检查再切回，旧周期完整保留；
  0GPU，不通过放宽控制根身份校验来消除失败。

- A11[受监护检索/真实回退/原生TLS均通过](evidence/serve-retrieval-managed-business-20261112/README.md)：
  含原TLS驱动失败累计5启动/10请求/1探测/314.133秒，原失败保留；
  只补TLS，SDK原始SSE及解析结果一致。自有进程、端口与锁已清理，
  下一步17项STOPPED可回退发行，默认rev80暂保持。

- A11受监护检索、版本切换及原生TLS驱动已通过
  [5项CPU](evidence/serve-retrieval-managed-driver-cpu-20261112/README.md)，
  后续分三租约累计最多4启动/8请求（含4自检）/1探测/1200秒，
  使用实际前驱控制回退payload与私有TLS副本，默认rev80保持。

- A11[实际主/轻量资格与候选封包通过](evidence/serve-file-search-profiles-20261112/README.md)：
  2启动/8模型HTTP/90.379秒，新能力声明一致；原始检索业务复用。
  候选32d3b819已含实际资格，显式v3升级许可已核对但未安装；
  默认rev80保持，继续原生TLS及受监护检索/版本恢复交付。

- A11能力发现与profile升级合同已补齐，
  [8项针对性CPU首过](evidence/serve-file-search-capabilities-cpu-20261112/README.md)。
  默认rev80保持；新增绑定未封包构建的资格入口，
  [5项CPU通过](evidence/serve-profile-build-driver-cpu-20261112/README.md)，执行
  后续2启动/8模型HTTP/900秒实际主/轻量短验，TLS/受监护交付继续。

- A11实际项目资料检索[真实业务已通过](evidence/serve-file-search-business-20261112/README.md)：
  1启动/6带模型HTTP/85.158秒，JSON/SSE/schema、实际引用、后台/历史及
  共享计量通过。原最终健康判据错误已修正，保留失败并离线复核原件，
  未重跑GPU。继续能力发现、原生TLS及受监护发行，默认rev80保持。

- A11审核识别、匿名Batch动态输入预算和取消状态已修复，
  [18项组合CPU首过](evidence/serve-file-search-combinations-cpu-20261112/README.md)。
  实际后台所有者、原ID恢复、审核持有/阻断、Batch冻结和加密/pro/
  summary/schema及连续检索通过；继续必要真实模型业务，默认保持。

- A11 Responses原生检索循环、标准事件/引用/历史、最大工具数与
  include投影已通过[34项CPU和3项SDK/schema](evidence/serve-responses-file-search-cpu-20261112/README.md)。
  原构建、夹具和Python调用问题保留，修复后仅重验受影响项；继续
  后台/Batch/审核等组合与实际模型业务。0GPU、默认rev80保持，
  尚未交付这组候选，也未完成A11或总计划。

- A11多库统一查询/加权排序与去重已通过
  [16项CPU首验](evidence/serve-vector-multi-store-cpu-20261112/README.md)，
  所有归属/索引先核验及全局预算、最终源/版本保护保持；继续
  Responses执行/事件/历史，0GPU、默认rev80保持。

- A11 Responses file_search定义/调用与混合权重已通过
  [13项针对性CPU](evidence/serve-file-search-contract-cpu-20261112/README.md)；
  首次12/13，修复精确极小数错误分类，仅重验该项。继续多库检索、
  原生工具循环/事件/引用与真实业务，0GPU、默认rev80保持。

- A11原生search/实际模型回调及共享配额已接线，
  [11项C++与3项SDK/schema首过](evidence/serve-vector-search-api-cpu-20261112/README.md)。
  官方JS直连36请求、Python原生TLS34请求；实际Files/存储，CPU
  模型夹具。0GPU、默认rev80保持，继续Responses执行及真实业务。

- A11查询执行/改写合同与搜索快照一致性已通过
  [18项针对性CPU](evidence/serve-vector-query-cpu-20261112/README.md)：实际源/完整索引、
  全查询预算、错误消费及并发版本保护；原夹具失败保留，仅重验该项。
  0GPU、默认rev80保持，继续原生HTTP/search与Responses内建循环。

- A11公开管理与内容读取已接通15条原生操作，统一schema/原生入口/
  客户端RPM及独立取消容量已接线；[12项C++和3项SDK/协议通过](evidence/serve-vector-api-cpu-20261112/README.md)。
  官方JS直连与Python经原生TLS均验证真实文件/索引文本，向量为CPU
  数学夹具；原失败保留，仅重验受影响项。实际search/Responses继续
  实施，0GPU，默认rev80保持。统一应用schema遗漏的Chat refusal
  类型也已同步，2项复用既有真实JSON/SSE检查通过。

- A11文件入库批次的持久接口已成组编码，
  [17项CPU首过](evidence/serve-vector-batches-cpu-20261112/README.md)：2000文件
  合同、原子计数/取消及批次分页；删除中的旧执行保留至实际清理，
  重挂载不能串用。继续公开HTTP/SDK/search/Responses接线。

- A11持久入库调度与真实ChatServer模型回调已接线，
  [18 项 CPU 首过](evidence/serve-vector-jobs-cpu-20261112/README.md)，实际引擎零警告构建：
  有界公平队列、原进程确认退出才恢复、取消持久化及实际清理后闭合，
  未知提交只核对原索引。辅助健康独立于核心readiness；公开API/
  SDK/Responses工具循环继续实施，默认rev80尚未变更。

- A11入库的客户端 TPM/并发许可与 BF16 输出适配已编码，
  [14 项针对性 CPU 通过](evidence/serve-vector-quota-cpu-20261112/README.md)。
  内部任务不重复外部 RPM，异常保留消费、未知用量保守结算；
  原生调度器与实际 GPU 回调继续接线，默认 rev80 保持。
- A11真实Files/隔离文档提取与tokenizer接线已编码，[11项CPU首过](evidence/serve-vector-ingestion-cpu-20261112/README.md)；
  原文件与NFC文本分别绑定，完整分块/EOS预算先准入，失败保留实际
  用量与未知提交身份。生产控制器/API/模型回调仍继续接线。
- A11持久父对象/文件关联、原子计数与索引发布、过期/回收和有界
  claim恢复状态已编码，[19项CPU已通过](evidence/serve-vector-stores-cpu-20261112/README.md)。实际提取/embedding
  执行器、HTTP/SDK及Responses仍继续实施，当前未增公开能力。
- A11原生资料检索开始成组实现：官方合同核对完成，参数、精确属性
  过滤及有界cosine/BM25排序16项CPU首过（0.026秒）；持久索引、
  API和Responses执行循环接着实现，尚未声明生产file_search可用。
  [实施与验收计划](SERVE_NATIVE_FILE_SEARCH_2026-11-12.md)，默认rev80保持。
- A11索引构建/持久化成组编码：实际token分块保UTF-8、逐块EOS和
  已消费用量，索引绑定源文件及embedding/tokenizer身份，复用不可变
  快照持久化；9新+2资产直接回归CPU首过（0.984秒）。尚未接公开存储API和
  Responses执行器，下一步继续对象管理/任务恢复/工具闭环。
- 原生外层恢复、真实资源域采集与版本回退组合已验并发行。真实root
  死亡后清树重建、未知配额保守窗口、单次文档工具、JSON/SSE与原存储
  Chat保持通过。[业务记录](evidence/serve-outer-resource-business-20261112/README.md)：
  三轮累计6启动/8Chat/1探测，严格租约283.572秒，原失败全部保留。
- 运行入口降权现在仅保留原用户已有video/render组；真实设备权限
  缺陷已修复，实际模型启动通过。[CPU与原失败](evidence/serve-runtime-identity-cpu-20261112/README.md)。
- 冷准入验收的root入口现在在永久降权子进程中核验私有策略；
  [5项CPU通过](evidence/serve-cold-owner-cpu-20261112/README.md)，未操作GPU或设备规则。
- A07模型生成拒绝已发行：Chat/Responses JSON/SSE、约束独立分支、
  真实概率、对象/历史与Python/JS客户端均接通。80项原生+19项适配
  [CPU](evidence/serve-refusal-cpu-20261112/README.md)、6请求
  [真实业务](evidence/serve-refusal-business-20261112/README.md)、
  [主/轻量资格](evidence/serve-refusal-profiles-20261112/README.md)通过。
- 新能力升级及回退已发行：修复旧启动自检对合法refusal的TypeError，
  [受监护及真实版本回退](evidence/serve-refusal-managed-business-20261112/README.md)
  累计含失败4启动/7Chat/1探测/248.927秒；拒绝对象、历史和预算保持。
  [发布副本归档](evidence/serve-publication-archive-20261112/README.md)保留
  原包及证据，0文件删除；发行后活动池31/32，归档2代可核验恢复。
- 已发布资源域约束覆盖内核memcg计量及独立物理可用内存观察，
  **没有声称实现统一DRAM/GPU物理硬上限**；R08仍须解决剩余边界。

## 全部任务进展与剩余缺口

详细实现、来源和证据以[任务表](serve-master-goals-v2.json)为准。
下列合并行只用于导航，不改变各任务的完成条件。

| 任务 | 当前状态与仍需完成的内容 |
|---|---|
| G00 | 推理/应用服务之外的组织、账单、训练管理范围待用户决定；不阻塞已确定任务 |
| A01/A02 | 已交付多指令、name、refusal历史等输入能力；剩余字段/模板及媒体、工具组合待逐项闭合 |
| A03/A04 | 采样/penalty/logprobs/multi-choice/prediction与工具选择、增量流、legacy/custom工具已验交付 |
| A05 | 丰富JSON Schema及工具/媒体组合已交付；A10媒体身份与状态缓存的组合尚待实现 |
| A06 | Completions/Models已验交付 |
| A07 | Responses核心、状态、reasoning/summary/encryption/compaction等已交付；原生refusal生产/SSE及恢复已发行；内建工具及媒体items、async/caller/namespace/output_schema等仍须实现 |
| A08 | 持久Chat/Responses、Conversations、background/cancel等已交付 |
| A09 | Files/Uploads/Batch已有实际交付；剩余8类endpoint后端及对应SDK/发行仍须与A10完成 |
| A10 | 原生embedding检索及moderation等已有交付；audio/realtime/image generation/edit/video等后端、媒体缓存及组合仍待实现 |
| A11 | 原生资料检索/Responses闭环已完成实际业务短验并发行；MCP及其余内建工具仍须实现 |
| A12 | 官方Python/JS及现有客户端的最终兼容、错误/流、迁移和发行矩阵待闭合 |
| N01—N07 | 原生接入、TLS/证书、CORS、Key/配额治理、HTTP、运维与发行入口均已验交付；N05可选前置路由已随rev94发行，主线待完成 |
| R01—R06 | 已验隔离与恢复保持；R01取消后缓存清理修复已过CPU/真实业务并随rev94发行；后续组合继续见R07—R10 |
| R07 | NVML失能冷准入代码/CPU验证已推进；实际设备独占资格、冷业务与组合验收未完成 |
| R08 | 原生资源域、压力观察与实际恢复组合已推进；物理内存硬边界及完整资源验收仍须完成 |
| R09 | 正常NVML下原生外层/资源/版本恢复已验发行；冷准入及宿主故障组合仍须闭合 |
| R10 | 视觉与恢复、降级、工具/schema组合待实现和验证 |
| Q01—Q03 | 跨接口真实资料查询闭环、最终可用主线发行、全任务核对和长稳交接仍待完成 |

## 接下来

1. 基于已修复入口与实际rev80身份重建R07冻结计划。原v1 plan-v2
   因入口缺陷及来源/默认变化而失效，不能执行。涉及nvargus/jtop的
   维护问题仍待回复，不以等待阻塞独立开发，不擅自干预其他服务。
2. [能力升级及恢复连续性](SERVE_CAPABILITY_UPGRADE_2026-11-12.md)已发行，继续A11项目资料查询的服务端工具执行/Responses事件，以及A10所需后端；先按官方合同
   和代码明确缺口，再成组实现，做针对性CPU及必要真实业务。
3. R08/R09剩余项与R10组合持续推进；各阶段可回退交付后继续下一项，
   全部计划完成前不结束总目标。

## 工作区、证据与保留候选

当前有效开发入口为主工作区的 `codex/serve-resilience-cd`。
隔离分支 `codex/serve-outer-recovery` 固定在 `38b0d29f`，已被当前工作
分支完整包含；其工作区与 `.q4t-work` 原路径继续保留，因为已发布包
与验收原件依赖这些路径。此时不删除工作区或证据。

视觉行最大值候选仍保留，未追加该候选GPU测试；范围和阻断记录见
[视觉补验结果](VISION_ROWMAX_COMPLETION_RESULT_2026-11-07.md)。
长稳实跑交给用户，短验不替代长稳结论。

此前STATUS五千多行原件完整保存在
[历史状态快照](HISTORY_STATUS_2026-11-12.md)，SHA256：
`40ae237add2d4203a4722e722a6cbfcf1925e00e7c5724e128f7bb84b15deae0`。历史结论不代表当前默认或剩余任务已完成；开发过程记录见
[当日日志](log/2026-11-12.md)。
