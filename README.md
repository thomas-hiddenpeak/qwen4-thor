# Qwen4-Thor

在 NVIDIA Jetson AGX Thor (SM110a, Blackwell) 上为
[Qwen3.8-Flash-Next](https://modelscope.cn/models/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream)
构建的原生 C++23 (host + device, CUDA 13.3) 推理引擎。

核心特性:**PLE SSD Stream** —— 51.2 GB FP8 n-gram 哈希嵌入查找表 (PLE,
技术报告称 N-gram Embedding Layer, 仅 Layer 2 一层) 不主动整表常驻,通过
Linux `io_uring` 按需读取本地 NVMe 页面，避免常驻约 47.68 GiB 的整表。
文件页缓存、页池、pinned staging 和 GPU scratch 都占统一RAM；
读取是否被已提交GPU工作掩盖取决于实际调度，不保证固定重叠比例。

## 能力概览

独立 Guardian 与原生服务入口已完成[有界验收交付](docs/SERVE_NATIVE_INGRESS_RESULT_2026-11-08.md)：
默认包无需nginx/systemd即可提供Bearer鉴权、JSON/SSE及模型/控制器/
入口恢复；辅助监控故障保留健康业务，恢复预算和停止意图持久化。
原生恢复151项不同CPU与3启动/18Chat真实短验通过，nginx仅可选前置。
后续[原生TLS/浏览器交付](docs/SERVE_NATIVE_ACCESS_2026-11-08.md)新增
71项不同CPU及1启动/6Chat；支持IPv4/IPv6、证书轮换、mTLS与CORS。
本机统一入口 `build/q4t service` 已安装，状态 `build/service-native-state`
保持停止；`build/q4t serve` 保留原含义。[原生生命周期](docs/SERVE_NATIVE_LIFECYCLE_2026-11-09.md)
提供容量配置、check/status/reload/stop 与持久发布/续接/旧代回退。
默认程序、包和元数据由发布事务共同选择；开发构建使用独立
`.q4t-work/` 目录，避免覆盖已管理的 `build/q4t`。Python控制面依赖仍保留，
长稳由用户实跑；其余兼容/恢复任务持续按总计划推进，旧包可回退。

当前默认程序（身份见[STATUS](docs/STATUS.md)）已包含工具/推理输出解析、JSON/SSE、带ID工具
历史、按序system/developer、参与者name及refusal回填和
`/v1/capabilities`能力/模板身份查询。消息profile为
`qwen3.8-openai-v3`，无需人为补充user消息；实现与真实SDK验收见
[消息输入交付](docs/SERVE_CHAT_INPUT_2026-11-08.md)。完整OpenAI协议
的剩余任务继续按总计划实现。

Responses 的 `truncation:auto` 已接入实际模板、分词与容量规划，
保留当前任务和工具调用组，持久历史保持完整；前台 JSON/SSE、后台
及真实文档工具闭环已验，见[实现与交付](docs/SERVE_RESPONSES_ADVANCED_2026-11-09.md)。
`reasoning.summary` 的 concise/detailed/auto 已由本机模型独立生成真实摘要，
提供 summary item/SSE、持久回放与后台执行；共用剩余输出预算和取消，
额外模型输入/输出计入 usage/TPM，外部请求只计一次 RPM。没有实际推理
时不伪造摘要。认证加密状态已交付；实际压缩/compact 的业务已验，
发布身份及后续 Prompt/缓存/策略任务继续以该计划和 STATUS 为准。
本地版本化 Prompt 已通过针对性 CPU 和真实业务并交付；客户端可通过
`prompt:{id,version,variables}`引用，省略版本时在请求提交时固定当前默认。
原生注册接口与使用边界见上述计划，当前已交付默认身份以 STATUS 为准。

[标准生成参数](docs/SERVE_SAMPLING_2026-11-08.md)已交付：temperature/
top_p、seed、presence/frequency penalty、logit_bias、n=1..128及真实
logprobs/top_logprobs；多choice共用请求/配额并逐项执行，JSON/SSE
可重组。Chat默认temperature=1，显式temperature=0且无附加采样约束
时保留greedy/MTP。采样使用目标logits的CPU实现，未作性能优化主张；
[prediction](docs/SERVE_PREDICTION_2026-11-08.md)目标验证、回滚与
accepted/rejected usage已交付；其余接口与组合按总计划继续实现。
[工具选择与增量流](docs/SERVE_TOOL_STREAM_2026-11-08.md)支持
allowed_tools和严格/多调用的逐片参数；仅成功终态及EOF后执行工具。
`messages`默认解析；`parse_response: false`保留原始输出，`prompt`
始终为raw。none / required / named、禁止多调用、strict工具参数和
有限JSON Schema已验，受约束请求仍普通解码，完整校验后发布SSE内容，
暂不组合缓存；显式单槽no-MTP媒体组合见[视觉v1](docs/VISION_SERVICE_V1.md)。范围见[应用兼容矩阵](docs/APPLICATION_SERVICE.md)。
固定12KiB严格复制输出的真实TCP取消/写截止及后续恢复已验，见
[限定结果](docs/SERVE_STRICT_TRANSPORT_RESULT_2026-11-03.md)；生成仍可能因
token预算不足而失败，canonical分词长度不构成生成上限。

启动增加metadata前、模型分配前、ready前的可用内存检查；默认2GiB
余量、绕过方式及[Thor四类分配计量](docs/SERVE_STARTUP_MEMORY_RESULT_2026-11-03.md)
均有实际证据。可用量快照不是预留，cgroup限制不覆盖全部CUDA物理RAM。

`reasoning_field`可选择`reasoning`或默认`reasoning_content`；
`include_reasoning: false`隐藏已解析的推理正文，计算和usage不变。
这两个选项只支持parsed messages。历史推理字段别名、流式字段稳定、
真实只读工具回填及安装HTTP通过，见[本批结果与回退](docs/SERVE_STAGE_B2A_RESULT_2026-11-03.md)。
`thinking_token_budget`现支持parsed messages的精确整数/null，耗尽后以真实
控制token闭合并转入答案；总输出预算优先，usage分别记录自然与强制token。
预算、MTP回退、工具/schema及缓存恢复见[本批交付](docs/SERVE_THINKING_HTTP_RESULT_2026-11-03.md)；
接口示例见[messages客户端](docs/AGENT_TOOL_CLIENT_2026-11-02.md)。
`stop`支持字符串或最多4串，每串256 UTF-8字节；null/[]禁用。
跨token与MTP批的停止串/尾部不会发布，JSON/SSE共用匹配，usage计至触发
停止的token。普通工具截断仍报错，严格输出及缓存组合首批拒绝；
[实际工具闭环、安装与回退证据](docs/SERVE_TEXT_STOP_RESULT_2026-11-03.md)。
`POST /v1/chat/preflight`只读返回实际token数量、输出预算和合格槽位，
`GET /v1/config`提供有效配置与来源；预检不会预留资源或保证生成成功。
媒体展开预检见[视觉v1](docs/VISION_SERVICE_V1.md)；原文本包装格式与[真实验证证据](docs/SERVE_PREFLIGHT_RESULT_2026-11-03.md)已公开。
`--max-inflight-requests`配置chat/preflight共同准入上限，
`--max-queued-requests`配置slot等候上限。准备完成后按容量适配FIFO授予
槽位，队列满时拒绝等待，空闲小槽仍可供不阻碍先来请求的小请求使用；
[真实队列、取消/工具恢复与回退证据](docs/SERVE_ADMISSION_RESULT_2026-11-03.md)。
接收拒绝与首次写失败分别用固定原因计数并关联trace日志；body额度
耗尽返回`body_capacity_exhausted`。写超时、取消与断连可分别定位，
计数范围及[真实HTTP/工具恢复证据](docs/SERVE_OBSERVABILITY_RESULT_2026-11-03.md)已公开。
最新验收限完整head，旧subset证据不自动扩张到新候选。
内存准入默认在全局MemAvailable低于2048MiB或读数失效时拒绝新工作，
达到2560MiB恢复，readyz同步；进行中的生成继续。可配置门限与恢复
余量，详见[限定业务验收与边界](docs/SERVE_MEMORY_ADMISSION_RESULT_2026-11-03.md)。
这是提前准入，不是物理RAM硬限额或OOM保证。
当前未启动常驻模型服务，GPU保持释放；通用SDK、完整JSON Schema、
多人运行边界和长期稳定仍按[工业化验收账](docs/SERVE_INDUSTRIAL_PLAN_2026-11-03.md)推进。

- **文本推理**: 48 层完整 forward (36 linear_attention + 12 full_attention),
  MoE 专家使用 NVFP4 W4A4，其他路径含 BF16/FP32；Paged KV cache、greedy 与实际采样生成
- **多模态**: 27 层 ViT、独立HF数值参考、3D MRoPE；`image_url`和私有
  `video_frames`帧序列接入。须显式`--allow-media --no-mtp --max-seq 1`；
  图片/工具/配额已有限接受，视频方向任务与硬解码未接受，仍标experimental。
  [矩阵、示例与回退](docs/VISION_SERVICE_V1.md)，当前身份见STATUS
- **MTP 推测解码**: 默认开启 T>1 batch verifier（k=3），`--no-mtp` 回退；
  默认使用完整草稿 head，`--mtp-subset-head DIR` 显式启用已验收的
  65536 行 BF16 独立草稿 head；当前合同、质量与性能边界见 STATUS
- **历史 suffix 草稿**: `--mtp-suffix` 显式启用固定 k3 历史匹配，适用于
  greedy 文本，可与两种草稿 head 配合；支持单流及已验收的1长＋3短agent
  配置。重复场景净收益、保护负点与本次用户接受范围见
  [长短agent接受决定](docs/MTP_SUFFIX_AGENT_ACCEPTANCE_2026-10-27.md)
- **完整会话缓存**: 显式`--session-cache-dir DIR`加请求`session_id`，保存
  完整目标/草稿状态到有界SSD缓存；工具等待期间释放执行槽，槽位被其他
  请求使用后可恢复原前缀。同进程长会话范围与成本见
  [会话缓存交付](docs/SESSION_CACHE_RESULT_2026-10-27.md)
- **专家SSD卸载**: 默认全部专家驻留，非零`--moe-resident-slots`
  显式启用配置的device槽位与SSD后备读取。Thor主机内存和GPU共用
  DRAM，CPU缓存不构成容量卸载；中转、缓存与工作集均计入统一RAM。
  与PLE是不同机制，offload研究不等于默认MTP验收，见
  [SSD offload合同](docs/SSD_OFFLOAD_CONTRACT.md)
- **连续批处理**: token 级打包、多请求调度；已完成有界槽位和生命周期
  验证，不能据此保证所有路径均不存在状态污染
- **长上下文**: 流式 top-k QSA；当前正式单流评估覆盖 1K、4K、8K、44K、
  200K，历史 262K 运行记录不代替当前发布验收
- **资源管理**: 启动内存预算、auto-length、运行时 preflight，以及由
  配置/张量形状推导的设备预留与所有权核对；受测热缓存启动已完成
  [有界可靠性交付](docs/STARTUP_DELIVERY_2026-10-21.md)。不保证任意
  配置或异常输入不 OOM，媒体解压等预算仍待完善
- **HTTP API**: OpenAI 风格 chat/completions 流式/非流式接口，以及模型、
  健康、指标和取消接口；支持采样、多choice和token概率，剩余OpenAI
  参数/接口按总计划继续实现
- **验证边界**: 已有数值缺陷修复、状态/故障测试和五档 HTTP 性能参考。
  [首版私有文本部署](docs/RELEASE_TEXT_V1_2026-09-28.md)已验收；
  公网多租户与业务SLA另行验收。当前结论见 [STATUS](docs/STATUS.md)，
  已知缺陷与未覆盖项见 [完善度审计](docs/PROJECT_READINESS_REVIEW_2026-09-27.md)

## 原生服务入口与可选前置

原生服务现已支持[密钥轮换/撤销、动态客户端限额和重启用量交接](docs/SERVE_ACCESS_CONTROL_2026-11-09.md#操作入口)。
配置修改区分持久提交与运行中实际生效；辅助文件失能保留已生效策略，
正常停止可精确交接，未知交接等待完整配额窗口。兼容包切换保留当前
密钥、限额和用量，迁移后不能恢复旧静态Key包。默认服务保持停止，
TLS和外部前置按部署需要选择；[实际交付证据](docs/evidence/serve-access-delivery-20261109/README.md)
与其余未完成总任务分别记录。

新`service prepare`默认选择原生入口，显式提供`--native-ingress`
所指的`q4t-ingress`及构建来源回执；不探测、复制或启动nginx。
`--ingress-mode nginx --nginx PATH`仅保留显式旧网关模式。直接运行
`build/q4t serve`保持原含义，服务封装由Guardian管理入口与模型生命周期。
默认保持loopback策略；原生TLS/LAN、mTLS/本地CRL、换证和CORS已
完成针对性验收，按部署配置选择启用。标准API与其他接入缺口继续按
[当前总计划](docs/SERVE_MASTER_PLAN_2026-11-08.md)实现。当前控制器/
journal仍使用Python，必要的CUDA、系统与模型依赖不变。
日志阻塞隔离见[R01交付](docs/SERVE_OBSERVATION_IO_2026-11-08.md)，
原生入口既有能力见[交付说明](docs/SERVE_NATIVE_INGRESS_RESULT_2026-11-08.md)。

首次v4接入包、实际模型TLS短验、凭据管理与回退见
[原生接入交付](docs/SERVE_NATIVE_ACCESS_2026-11-08.md)。
TLS与认证加密状态使用系统OpenSSL 3，不复制nginx代码，也不随包携带nginx。
构建默认启用`Q4T_ENABLE_NATIVE_TLS=ON`，需要匹配的OpenSSL 3开发头
文件及系统运行库；可用`-DQ4T_ENABLE_NATIVE_TLS=OFF`构建明文入口。
运行时TLS入口按需加载libssl/libcrypto；引擎的加密状态、密钥操作和
Uploads可选MD5按需加载libcrypto，普通Chat/Responses不依赖其可用性。
原生文件获取在独立子进程内按需加载同一系统libssl，
不调用外部下载服务；文件输入已通过针对性CPU和真实PDF/表格/历史业务并交付，
见[文件输入发行](docs/evidence/serve-file-inputs-delivery-20261110/README.md)。

当前发行提供`serve --file-input-policy-file FILE`及纯CPU
`q4t file-input-policy-check FILE`；文件需当前用户所有、0600或0400，
最小配置为`{"version":1}`，可覆盖获取策略、各解析域预算及表格准备
输出上限。`service prepare --file-input-policy FILE`将策略封入发行包，
`/v1/config`的`file_inputs`报告实际数值与SHA，不泄露私有地址或路径。
请求不能覆盖部署策略；网络或codec故障只失败依赖该功能的请求。
完整参数、累计预算与发行回退说明见上述文件输入计划。
关闭TLS仍需OpenSSL开发头以构建加密状态功能。缺库或坏证书不会
回退明文、停止或重启健康模型。OpenSSL按其Apache-2.0许可使用，
项目依据其官方API实现传输与AES-256-GCM，未移植其他服务器源码。

PDF解析通过独立受限进程调用系统
[Poppler](https://poppler.freedesktop.org/)的`pdfinfo`、`pdftotext`和
`pdftoppm`，使用标准输入输出提取文字与全部页面图像。q4t不链接或
随包分发这些GPL工具；本机组件版本24.02.0，许可随系统包保存在
`/usr/share/doc/poppler-utils/copyright`。只在PDF请求时启动，缺失、
超时或异常只失败该请求；普通文本服务不依赖它们。需要Linux
Landlock ABI4及seccomp支持，不能用无隔离执行作为回退。实际PDF文本/页图API与跨版本业务已验，见
[文件输入发行](docs/evidence/serve-file-inputs-delivery-20261110/README.md)。

Office容器底座在独立worker内按需加载系统libarchive 13/libxml2 2，
构建需`libarchive-dev`与`libxml2-dev`头文件。原生适配采用内存ZIP
读取及禁用外部实体的XML读取，进入纯内存沙箱后解析；不解压到主机
目录。系统包分别保留BSD与MIT等原始许可（见其copyright文件），
q4t没有复制这些库源码或随包分发库。OpenXML/ODF内容和原生RTF解析
已做针对性CPU验证；RTF代码页仅在解析子进程通过系统libc的iconv
预加载，实际未使用的代码页缺失不影响其他文档。

Legacy Office/Apple适配使用隔离子进程中的系统
[LibreOfficeKit](https://docs.libreoffice.org/libreofficekit.html) C API，
构建需`libreofficekit-dev`头；不随q4t分发或启动Office常驻服务。
只有对应文档请求按需加载系统运行库和过滤器，普通文本、原生
OpenXML/ODF/RTF不依赖它。固定系统路径可通过CMake的
`Q4T_SYSTEM_LIBREOFFICE_DIRECTORY`指定，默认`/usr/lib/libreoffice/program`。
缺失/超时仅失败对应请求，禁止以无隔离模式回退。每次转换使用独立
私有配置，关闭宏、主动内容和链接更新，限制总写入、文件/线程创建、
时间及地址空间；未确认退出时不复用占槽或清理其目录。
本机接口版本24.2.7，系统包保留MPL-2.0、Apache-2.0及各过滤器许可
（见`/usr/share/doc/libreoffice-core/copyright`和对应依赖包）。q4t按
官方C ABI实现适配，未复制其实现源码。DOC/XLS/PPT、Apple/XLSB
实际内容及隔离边界已做针对性CPU验证；转换回执保留产品版本、
BuildId及转换结果SHA，原件SHA单独保持。Google查看链接和快捷方式
通过同一来源策略获取真实导出，保留原件与跳转来源；公开Sheets样表
已做实网短验，私有文档仍须具备实际访问权限，不注入用户凭据。
嵌入文档和统一API消费已接入；各格式组件证据和综合业务分别记录，
不会将一种格式的业务短验宣称为所有文档内容都已逐一验收。
Apple/XLSB适配的固定上游测试资料及无照片派生说明见
[测试来源与MPL许可](tests/fixtures/file_adapters/README.md)。

WebP静态图片使用隔离子进程按需加载系统libwebpdecoder 3与zlib 1；
构建需要`libwebp-dev`、`zlib1g-dev`头文件。主进程仅校验容器和预算，
不链接这两个解码运行库；子进程采用固定系统路径、纯内存沙箱和既有
超时/取消/退出回收合同。原件、实际RGB PNG、库版本与SHA共同保存，
历史回放不重新依赖codec。缺库或解码失败只失败对应图片请求。
按[libwebp官方API](https://developers.google.com/speed/webp/docs/api)与
[RIFF容器规范](https://developers.google.com/speed/webp/docs/riff_container)
实现适配，未复制解码器代码；系统libwebp保留BSD及专利许可，zlib保留
zlib许可（本机`/usr/share/doc/libwebpdecoder3/copyright`和
`/usr/share/doc/zlib1g/copyright`）。完整API接入及真实模型验收继续实施。

Responses可通过`include:["reasoning.encrypted_content"]`取得本服务
签发的密文状态，用于无状态续聊或持久会话；绑定客户端、模型和模板。
原Qwen推理明文投影仍可见，这不等于整段响应或磁盘加密。
本机服务UID可用`q4t state-keys inspect|initialize|rotate
--object-store-dir ABS_DIR`管理密钥；轮换要求`--expected-revision N`，
旧密钥仅通过`--retire KEY_ID`显式退役，无需重启模型。
详见[加密状态与管理边界](docs/SERVE_RESPONSES_ADVANCED_2026-11-09.md#a073-原生密钥运维接入)。

应用状态的原生留存管理使用 `q4t state-retention inspect
--object-store-dir ABS_DIR`。自动过期清理保护未结束任务及仍被引用的
状态；永久孤立条目默认保留，可明确用 `--item-created-before UNIX_SECONDS`
选择创建时间更早的条目。确认计划后，以相同条件运行 `collect
--expected-plan SHA256`；每批最多128个同客户端对象，变化返回冲突，
丢回执时重新inspect，不盲目重试。无需重启模型；`--offline`只用于
可独占的停服目录。详见[留存治理](docs/SERVE_APPLICATION_RETENTION_2026-11-10.md)。

原生Files提供`POST/GET /v1/files`、`GET/DELETE /v1/files/{id}`和
`GET /v1/files/{id}/content`，兼容官方SDK multipart上传与原字节下载，
按客户端隔离并持久保存。单File协议上限512MiB（batch输入200MiB），
部署文件/普通body与存储限额分别生效；上传不整文件缓存到RAM。
较大文件需显式配置私有客户端策略v4的`max_file_body_bytes`及原生
入口策略v5的`file_body_bytes`，未升级配置保持原容量，不隐式扩大。
Files默认batch留存30天，其余永久，可用`expires_after`明确覆盖。
用法/安全旧包迁移见[客户端容量](docs/SERVE_ACCESS_CONTROL_2026-11-09.md)，
[业务及发行证据](docs/evidence/serve-files-delivery-20261110/README.md)。
Uploads已提供原生创建、分块、顺序/MD5完成及取消，生成标准File；重复Part保持原字节，取消有独立准入/内存，见[交付证据](docs/evidence/serve-uploads-delivery-20261110/README.md)。默认部署容量保持，较大Part需显式配置。
模型原生file_id/file_data/file_url内容消费已完成实际PDF/表格和历史
复用验收，见[交付记录](docs/evidence/serve-file-inputs-delivery-20261110/README.md)。
原生Batch提供创建、查询、列表、取消及完整JSONL结果文件，当前
Chat Completions、Completions、Responses和Moderations四类执行已交付；后台
Responses等待原任务终态和资源排空，在线/Batch使用独立分钟配额。
结构化失败与取消保存实际用量，重启不重放已开始且结果不明的行。
新审核任务回退使用已登记的61649847包，保留审核执行器、原任务和
结果；[最新交付记录](docs/evidence/serve-moderations-delivery-20261111/README.md)
列出身份。其余四类Batch后端及完整多模态接口仍按总计划继续实现。

`POST /v1/moderations`及官方SDK `moderations.create()`提供原生审核：
字符串数组逐项返回结果，文本/图像内容数组合并审核，返回本地实际
模型的完整13类分数、分类和适用输入。每请求1RPM，各分类步骤累计
计入TPM，失败或取消不发布部分成功。图像使用显式`--allow-media
--no-mtp --max-seq 1`配置；默认文本MTP保持。分数为本地模型估计，
实际良性/威胁与图像短验见[业务证据](docs/evidence/serve-moderations-business-20261111/README.md)。

标准`POST /v1/responses/input_tokens`与SDK
`responses.inputTokens.count()`已通过计数与真实生成usage业务短验。
它按本模型真实模板/分词和媒体展开计数，可读取当前客户端的历史与
加密状态，接受空请求；不生成、不写会话、不消耗生成RPM/TPM。
结果为初始输入，不包含后续摘要/审核等派生步骤。可用
`personality:"friendly"|"pragmatic"`选择本模型实际提示风格；原生
Responses生成也接受此字段，以保持计数与生成同源。

上下文压缩可在 Responses 请求指定
`context_management:[{"type":"compaction","compact_threshold":4096}]`，
或用末尾 `{"type":"compaction_trigger"}` 显式触发。阈值按实际模板/
媒体展开 token 计量，省略时取模型上下文的4/5；实际记忆模型步骤共享
原请求预算、取消和配额，自动记忆最多使用输出上限的一半且不超过512。
`POST /v1/responses/compact` 使用配置默认输出预算，返回保留用户消息
及认证密文的下一输入窗口，可将 `output` 原样作为后续 `input`。
原始持久历史不删除；截断、无法缩短或认证失败均返回具体错误，
无可压缩项不编造模型用量。入口原生支持这两种模式，无需外部网关。

部署方可自行前置nginx或其他HTTP网关，传递普通Bearer Authorization，
无需提供q4t内部凭据或准入子请求。nginx前置的最小转发配置如下；
Host须与原生入口策略的`server_name`一致，TLS/来源限制由部署方按需配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:18161;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_request_buffering on;
    proxy_cache off;
    proxy_next_upstream off;
}
```

入口支持[已发布API路由](docs/serve-openapi-v1.json)及相应CORS预检，认证后的`/livez`只表示入口存活；`/readyz`与
业务请求受模型自检、配额隔离和代次绑定约束，恢复期返回503及
Retry-After。请求头/体共享10秒绝对接收期限，头64KiB，业务体最多
96MiB，聚合业务体预留192MiB；32个有界处理槽，数据与控制分别限额。
客户端`max_body_bytes`与原生入口`body_bytes`可显式设置更低限额；既有
策略容量不变。文件输入可容纳50000000原字节的base64，模板变量预算
96MiB，保留原变量的展开结果预算192MiB；模板定义仍为256KiB。
后台任务累计保留输入默认512MiB，可用`--background-max-input-mib`
设置1—2048MiB；此为序列化逻辑量，不是进程RSS。持久化原文件、
提取快照与大inline内容同时计入对象配额，大文件业务须相应配置
`--object-max-client-mib`与`--object-max-total-mib`，不会绕过存储配额。
HTTP/1.0/1.1支持Content-Length；HTTP/1.1支持有界chunked请求和
认证/准入后的`100-continue`，有界持久连接逐请求重新认证。核心默认
每连接最多100请求、空闲5秒；原生policy v4可配置限额、路径前缀与
显式可信代理链。SSE具备完整chunk framing，截断关闭；不重放POST/
工具，不在新模型上续接被中断的流。Python/官方SDK客户端支持
`https://host/llm/thor/v1`这样的前缀地址，详情和部署例见
[原生HTTP接入](docs/SERVE_HTTP_TRANSPORT_2026-11-09.md)。
可选前置普通HTTP与SSE、原生浏览器调用已验；实际TLS已在本机网卡
地址和自身/32白名单完成模型业务短验，跨主机生产证书/域名按部署配置。

## 目标模型

- 架构: `qwen4_exp` (48 层: 36 linear_attention + 12 full_attention)
- MoE: 512 专家/层, top-10 路由 + shared expert, NVFP4 W4A4 量化
- 注意力: GQA 24Q/2KV, head_dim 256, MRoPE (interleaved)
- PLE: 每 token 16 次确定性查找, FP8 行 160 字节, 51.2 GB sidecar
- MTP: 1 层 full_attention 推测解码
- 视觉: 27 层 ViT (图像/视频输入)
- 模型配置最大上下文: 262144 token；当前正式 HTTP 矩阵最大名义档为 204800

## 硬件目标

| 项 | 值 |
|---|---|
| GPU | NVIDIA Thor, SM110a, 20 SM |
| 内存 | 122 GB LPDDR5X 统一内存 |
| CPU | 14 核 ARM Neoverse V3AE |
| 存储 | NVMe SSD |
| 工具链 | CUDA 13.3, CMake 4.0+ (pip 装 4.4.3), g++-14 (apt) (aarch64) |

## 构建

```bash
# 要求: CMake >= 4.0 (CMAKE_CUDA_STANDARD 23 仅在 4.x 映射 nvcc --std=c++23)
#   pip install --user --break-system-packages cmake==4.4.3
# 要求: g++-14 (nvcc 的 --std=c++23 需 host 编译器支持, GCC 13 会静默忽略)
#   sudo apt-get install g++-14 pkg-config liburing-dev libicu-dev \
#     libssl-dev libarchive-dev libxml2-dev libreofficekit-dev libwebp-dev zlib1g-dev
# custom grammar构建另需rustc >= 1.65（本机使用固定1.90.0）；不要求cargo
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-14 -DCMAKE_CUDA_HOST_COMPILER=g++-14 \
  -DQ4T_CUDA_ARCHITECTURES=110a -DQ4T_ENABLE_XGRAMMAR=ON
cmake --build build --parallel

# 开发测试（默认SKIP也非零退出；发布范围与入口见tools/release）
python3 tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/manual-tests/lease -- \
  ctest --test-dir build --output-on-failure
```

## 使用

构建参数 ON 复现当前默认的工具控制 / 结构化输出能力，并拉取固定
XGrammar C++ 及 regex-syntax 0.8.8 源码依赖（归档和源码摘要校验）。
后者由 rustc 静态编译，只在已有隔离工作器内准备 custom regex/Lark；
不需要部署 Rust 工具链或增加外部服务。许可随包保留于
[依赖声明](third_party/licenses/structured_output/README.md)。
CMake 选项自身默认 OFF，公共 host 构建不联网；
OFF 构建遇到需要约束的请求会明确拒绝，不降级为普通生成。

```bash
# 版本与硬件探测
./build/q4t version
./build/q4t probe

# 独立 CLI 生成路径；不属于下述 HTTP MTP 验收范围
./build/q4t generate "用一句话解释统一内存。" --max-tokens 32

# 已验收的 HTTP 单流配置；默认 MTP k=3
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 1 --max-prefill 8192 --max-len 208896
# 普通 decode 回退：在上述 serve 命令末尾添加 --no-mtp
# 显式启用历史 suffix：在上述 serve 命令末尾添加 --mtp-suffix

# 显式使用已交付的 65536 行 BF16 独立草稿 head；仍为单流 MTP k=3
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 1 --max-prefill 8192 --max-len 208896 \
  --mtp-subset-head .q4t-work/sha2-startup-20261023/delivery-package/artifact

# agent：1个长槽＋3个短槽；完整head，显式开启suffix
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 4 --max-prefill 4096 --max-len 208896 \
  --seq-capacities 208896,65536,65536,65536 --mtp-suffix
# 子集head：在该命令末尾添加上面的 --mtp-subset-head DIR
```

以上命令从仓库根运行。子集示例使用本机保留的交付产物；该目录包含
`head.bf16`、`token_ids.bin` 和 `manifest.json`，不随 Git 克隆下载。
其他机器需准备与模型及 tokenizer 匹配的产物，并将参数替换为实际目录；
产物生成方法和固定候选边界见[转换工具说明](tools/mtp_subset/README.md)。

`serve` 参数默认值来自 `ServerOptions`，不是上述示例的显式配置：

| 参数 | 默认值 / 行为 |
|---|---|
| `--host` / `--port` | `127.0.0.1` / `8000`；host 接受数字 IPv4 |
| `--max-seq` | `1` |
| `--max-inflight-requests` | 省略为max(32,2×有效槽数)；显式1..4096，共享chat/preflight，含准备/等待/输出 |
| `--max-queued-requests` | 省略为max(0,有效inflight−有效槽数)；显式0..4096且不超过inflight，0表示不等待 |
| `--seq-capacities` | 默认等长槽；显式逗号列表为每槽输入＋输出容量，须与max-seq/max-len匹配，预算不足时拒绝；推荐agent布局见上例 |
| `--max-prefill` | `0` 表示使用模型默认 `2048`；可显式设至 `8192` |
| `--max-len` | `0`，默认预算开启时自动推导有效长度 |
| `--max-tokens` | 请求省略输出上限时使用 `256` |
| `--mtp` / `--no-mtp` / `--mtp-k` | MTP 默认开，k 默认 `3`；serve 接受 k=1..16，验收仅覆盖 k=3 |
| `--mtp-subset-head DIR` | 默认不启用，草稿使用完整 head；指定产物目录启用固定 65536 行 BF16 子集，要求 MTP 开启且 k=3 |
| `--mtp-suffix` | 默认关闭；要求文本、greedy、MTP k=3、有效max-len≤262144，可配合完整或子集 head；交付覆盖单流及上述4槽布局 |
| `--session-cache-dir DIR` | 默认关闭；另须请求传session_id才缓存；同进程文本k3、显式槽位，8项/24GiB SSD/30分钟TTL；不组合媒体、trace或专家offload |
| `--prefix-cache-dir DIR` | 默认关闭；另须请求传prefix_cache_scope；同进程公共池8项/8GiB SSD/30分钟TTL，请求默认只读 |
| `--prefix-cache-checkpoints N,...` | 默认8192,32768,65536；1～4个严格递增的1024～131072边界，请求用prefix_cache_write_tokens选择最多一个写点 |
| `--request-deadline-ms` | `1200000`（20分钟）；含等待槽位和生成，显式范围1000～10800000；客户端超时需匹配 |
| `--allow-media` | 默认不启用，显式启用实验媒体路径 |
| `--mem-fraction` | `0.90`，启动分配估算占总内存的比例，不是物理预留 |
| `--cuda-allocation-limit-mib` | `auto`，生产wrapper请求字节额度为mem_fraction×MemTotal；0显式关闭，正整数指定MiB，独立于`--no-budget`。不覆盖DSO内部/host堆/driver/文件缓存/CUDA池保留，不是整机RAM硬限额；交付状态见STATUS |
| `--startup-memory-headroom-mib` | `2048`，大分配前要求真实MemAvailable容纳估算总量及余量；元数据前/ready前复查余量，0禁用，`--no-budget`同时绕过此估算检查 |
| `--min-available-memory-mib` | `2048`，运行时全局内存准入门限；0显式禁用，独立于`--no-budget` |
| `--memory-recovery-margin-mib` | `512`，正数；关闭后需达到门限+余量才恢复；门限0时忽略 |
| `--moe-resident-slots` | `0`，所有专家驻留；非零启用按需驻留 |
| `--startup-plan` | 默认`auto`；`off`保留旧分配路径，`required`要求支持并启用预留计划 |

`--model-dir DIR` 可覆盖代码中的本机默认模型路径。完整参数解析见
[src/server/server_options.cpp](src/server/server_options.cpp)。当前 main 使用
T>1 batch verifier；`--mtp-verifier` 已移除，sequential 实现在历史提交
`7f6a69c` 中保留。当前交付与基线见 [STATUS](docs/STATUS.md)。

### 独立草稿 head 与启动校验

`--mtp-subset-head` 是进程启动参数，不是 HTTP 请求参数。不能与
`--no-mtp` 或非 3 的 `--mtp-k` 同用。产物缺失、摘要或映射不合法时
明确启动失败，不静默改用完整 head。

移除该参数并重启即可回到完整草稿 head，MTP 仍开启；若要回到普通
decode，移除该参数后加 `--no-mtp`。这些切换不需要更换程序。
回退整个程序版本另见[两级回退包](docs/MTP_SUBSET_MAIN_MERGE_2026-10-23.md#两级回退包)。

子集只裁剪草稿投影，主模型完整 head 继续驻留，新增权重和映射
320.25 MiB；加载中转和文件页缓存另占统一 RAM。BF16 保持，但草稿
候选和投影舍入可能变化，不承诺任意输入输出无损。受测收益、质量
和资源边界见[有界验收](docs/MTP_SUBSET_RESUME_2026-10-23.md)。

SHA2 校验加速无需启动参数：支持时自动使用 ARM SHA2 指令，否则走
软件实现；源权重、tokenizer 和派生产物的完整 SHA-256 校验保留。
它减少子集加载时的校验耗时，不省略校验，也不改变 head 的默认选择。
实测启动成本见[SHA2 验收](docs/SHA2_STARTUP_2026-10-23.md)。

### 固定 k3 历史 suffix 草稿

在上面的完整 head 或子集 head 服务命令末尾添加 `--mtp-suffix`，即可
尝试从本请求已提交历史中取三个草稿 token。选定匹配至少32 token，
须通过数字格式过滤且首候选与已计算的d0一致；否则继续使用神经草稿。
候选使用完整词表的全局ID，不要求都属于65536行子集；仍经过目标模型
四行验证及接受前缀状态更新。索引不跨请求保存。

移除 `--mtp-suffix` 后重启即关闭该功能，不改变 head 选择。它不能与
`--no-mtp`、非3的k、`--allow-media` 或超过262144的容量
同用，不支持的组合启动失败。输出或上下文不足时沿用普通decode尾部。
`generate` 不接受该开关，其验收范围不随HTTP扩展。

每请求索引动态内存上限2MiB，共享按需token属性缓存上限256KiB，另计
对象、tokenizer临时分配和allocator成本。1长＋3短重复代码/文本的
E2E吞吐收益为完整head约1.98%/1.83%、子集约1.12%/1.05%；decode收益
分别约5.44%/5.22%、2.99%/2.85%。子集未达原3%decode目标，用户接受
本次实测收益与小幅保护负点；统计总体非劣未建立。旧单流短重复收益
属于旧程序身份的证据，不替代这些并发长输入测量。

上述4槽配置按输入加最大输出选择最小可用合适槽：最多一个活动请求
超过65536 token预算，其余三个最多65536（输出也占容量）。prefill暂存
以4096 token分块消费。执行槽仍为4个，解析/排队/生成请求总数上限32；
16/32突发只验证了短请求，不保证32个长请求的完成时限。单请求body
上限96MiB、全局body预算192MiB，超限仍会拒绝。长prefill会影响其他请求
TTFT，客户端须为排队留足超时。使用、回退及健康检查见
[长短agent交付记录](docs/MTP_SUFFIX_AGENT_DELIVERED_2026-10-27.md)。

### 完整会话缓存

为需要跨工具等待复用的长会话显式启用缓存：

```bash
install -d -m 700 .q4t-work/agent-session-cache
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 4 --max-prefill 4096 --max-len 208896 \
  --seq-capacities 208896,65536,65536,65536 \
  --mtp-suffix --session-cache-dir .q4t-work/agent-session-cache
# 子集head另加：
# --mtp-subset-head .q4t-work/session-cache-20261027/delivery-package/artifact
```

主agent和每个子代理分别生成并保存一个`secrets.token_hex(32)`作为
`session_id`（64个小写十六进制字符）。每次HTTP请求在顶层传这个字段，
同一会话串行复用，不同会话使用不同ID。仍提交完整`prompt`或`messages`，
不是只发新增工具结果。普通短请求可省略ID；只开启启动参数不会自动缓存
未传ID的请求。正常响应结束保存完整前缀并释放执行槽，其他请求使用原槽
后，下一轮可以恢复到任意容量足够的槽。长于65536的请求仍只能使用长槽。

要稳定命中，客户端需保留原始模板文本及生成历史。本轮收益使用预渲染
`prompt`测量；`messages`恢复前也逐token检查，但模板会trim文本、重建
thinking和工具调用。带thinking的整段输出直接放回`content`可能被重复
包裹，结构化接入需保留响应选定的`reasoning_content`或`reasoning`、`content`和`tool_calls`。
历史被改写、压缩或模板选项改变时可能未命中并重新prefill。当前没有针对
某个agent SDK的即插即用适配验收。

检查响应的`usage.prompt_tokens_details.cached_tokens`；流式请求需同时传
`"stream_options":{"include_usage":true}`。大于0表示实际恢复的目标前缀
token数，0表示本次重新计算。缓存保存最后一个完整MTP边界，最多4个旧
尾部输出token仍会重算。跨轮状态正确性以独立接受前缀参考和实际HTTP为准，
不承诺任意输入与fresh prefill位级相同。

首版同进程内最多8份快照、SSD总预算24GiB、30分钟TTL；淘汰/过期/进程
退出后正常重新prefill。目录需本用户私有0700、非tmpfs且支持O_DIRECT和
O_TMPFILE，匿名文件不会显示为普通缓存文件。新增RAM预算预留32MiB，
并不释放已分配GPU槽位内存。保存/恢复会占模型锁并影响其他请求等待。
要求纯文本greedy、MTP k=3和显式槽位容量；不组合媒体、router trace或
SSD专家卸载。移除`--session-cache-dir`并重启即可关闭，客户端同时停止传ID。

受测1长＋3短两轮事务（含首次保存、其他请求占槽、恢复）的净耗时降低
27.5%～27.8%，恢复TTFT降低约95%；首轮写盘增加约8.8～9.4秒。数据是两种
head各一个代码/文本描述性配对，不保证任意agent工作流收益。完整结果、
保护负点、统计限制和回退见[会话缓存验收](docs/SESSION_CACHE_RESULT_2026-10-27.md)。

对于反复恢复、续写再保存的长会话，在启动命令前加
`Q4T_SESSION_INCREMENTAL=1`可显式减少重复快照写入；程序默认关闭，
公共池不受该开关影响，客户端参数不变。两head受测生命周期吞吐分别
+5.01%/+9.43%，再存约3.4秒、重复写入−96.97%；首次仍全量，首次事务
保留−1.97%/−1.83%/−0.62%观察值。见[增量保存交付与回退](docs/SESSION_INCREMENTAL_DELIVERED_2026-10-29.md)。

### 跨会话公共prefix共享

要同时启用suffix、完整会话缓存和公共prefix共享，按1长主agent＋3子代理
实际接入，见[完整启动与客户端示例](docs/AGENT_CACHE_INTEGRATION.md)。
其中包含可运行的5请求Python流程：主保存→三子并发只读→主等待后恢复，
以及token容量检查、ID生命周期、流式usage/EOF、取消和messages适配说明。

主agent和子代理需要复用完全相同的系统提示、工具定义或共同历史时，
可在完整会话缓存上显式打开共享池：

```bash
install -d -m 700 .q4t-work/agent-session-cache .q4t-work/agent-prefix-cache
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 4 --max-prefill 4096 --max-len 208896 \
  --seq-capacities 208896,65536,65536,65536 \
  --mtp-suffix --session-cache-dir .q4t-work/agent-session-cache \
  --prefix-cache-dir .q4t-work/agent-prefix-cache \
  --prefix-cache-checkpoints 8192,32768,65536
# 子集head另加：
# --mtp-subset-head .q4t-work/prefix-seed-20261028/delivery-package/artifact
```

为同一可信共享组生成并保存`secrets.token_hex(32)`作为
`prefix_cache_scope`；不同组使用不同scope，各会话的`session_id`仍分别
生成。客户端仍发送完整历史，各agent专属角色提示放在公共token前缀之后。
这个scope是共享缓存的能力凭据，应像私有session ID一样保护。

源请求在顶层明确指定一个种子边界，例如：

```json
{
  "prefix_cache_scope": "同组共享的64个小写十六进制字符",
  "prefix_cache_write_tokens": 8192
}
```

子请求使用同一个scope，省略`prefix_cache_write_tokens`或设为0，只读复用。
示例scope需替换为实际生成的值；上述字段加入正常chat请求体即可。
源请求成功结束后才发布种子，提前到达的子请求可能未命中而正常prefill。
没有指定正边界的请求不会自动写公共缓存，已有相同种子不重复覆盖。
私有会话恢复优先；已跨过所选边界时跳过补种子，不为了保存重新计算。

公共长度可以配置，不锁死业务场景。`--prefix-cache-checkpoints`允许1～4个
1024～131072之间的边界；请求的正边界必须在此列表中，且小于实际输入
长度，否则400。业务输入长度仍由槽位/上下文容量约束，不必等于边界。
当前收益验收点是8K和32K，其他配置不自动取得相同收益保证。命中取最长
精确token前缀；相同可见文字在模板或token发生变化后可能miss。
通过`usage.prompt_tokens_details.cached_tokens`检查实际恢复长度，流式需
`"stream_options":{"include_usage":true}`。

公共池默认8项、8GiB SSD、32MiB RAM预算（含8MiB staging）、30分钟TTL；
与私有池分别有界，同进程有效，重启/过期/淘汰后正常prefill。目录要求与
私有池一致。恢复复制完整状态到独立槽，不释放预分配槽内存，也不是GPU
KV页物理共享。保存/恢复占模型锁，未测混合负载的等待时间不作保证。
移除`--prefix-cache-dir`及`--prefix-cache-checkpoints`并重启，同时停传两个
prefix请求字段，即可关闭公共缓存；私有会话缓存可继续使用。

受测1长主agent＋3子代理、两种head的事务净耗时：共享8K缩短3.85%～6.90%，
共享32K缩短21.72%～23.19%，包含第一次种子保存。8K种子约368MB、保存
0.358～0.384秒；32K约1.125GB、保存1.103～3.706秒。五档保护仍有小幅负向
观察，总体统计非劣未建立；完整数值、RAM成本、接受边界及回退见
[显式种子验收](docs/PREFIX_SEED_RESULT_2026-10-28.md)。不自动打开缓存，
也不承诺任意agent SDK接入、任意前缀长度或32个长请求的延迟收益。

### 其他入口与验收范围

启动计划在`auto`下仅对明确不支持的模式在计划开始前选择旧路径；
元数据、预留或交付失败直接停止。支持范围与本次1K可靠性验证见
[正式交付记录](docs/STARTUP_DELIVERY_2026-10-21.md)，不代表新增长输入、
并发或性能优化结论。

`generate` 是另一套调用路径：输出上限默认 64，默认尝试加载 MTP，
接受 `--mtp`、`--mtp-k`、`--mtp-subset-head DIR`、`--max-prefill`、
`--model-dir`、`--max-tokens`，指定子集时同样要求 k=3，
**目前不接受 `--no-mtp`**；不能将 serve 的参数或验收直接套用到它。

冻结验收范围为本机 Thor、既定 checkpoint、纯文本 greedy、单流 k=3、
`--max-seq 1 --max-prefill 8192 --max-len 208896`，以及上述显式1长＋3短
agent配置。其他 CLI、媒体、并发布局或 k 值不随此矩阵取得验收。
T>1 与普通 decode 的舍入路径不同，遵守
[EVALUATION](docs/EVALUATION.md) 的 N=4 ULP 近 tie 合同；不承诺跨路径
逐位一致。默认监听 127.0.0.1。

性能评估使用 [tools/evalscope](tools/evalscope/README.md) 的真实 HTTP E2E；
bench 不作为性能验收依据。归并方案、共用正确性修复与回退点见
[MTP 归并边界](docs/MTP_MERGE_BOUNDARY_2026-10-15.md)。

`POST /v1/chat/completions` 使用 OpenAI 风格请求体，content 可为字符串或
parts 数组; 图像用 `image_url` (base64 data URL), 视频用 `video_frames`
(base64 帧数组)，仅在 `--allow-media` 下可接入。另有 `GET /healthz`、
`GET /v1/models`、`GET /metrics` 与 `POST /v1/requests/cancel`。

流式响应的每个SSE事件均为完整UTF-8。成功须有正常finish、按请求选项
返回的usage、`[DONE]`和完整EOF；HTTP 200或单独finish不能替代完整响应。
每次逻辑发送最多等待30秒，取消终态共用250毫秒预算；请求deadline是
合作式取消，在途GPU工作完成后才释放资源。已知发送失败不发布新缓存。

指标口径：`q4t_ttft_seconds`仅计流式首个非空内容完成本地发送；原首目标
token就绪时点保留为`q4t_target_ready_seconds`，完整本地响应为
`q4t_response_seconds`。这些指标不代表客户端已消费。可靠性结果、
旧指标迁移和回退见[Serve交付记录](docs/SERVE_RELIABILITY_2026-10-30.md)。

**长上下文注意**: `--max-len` 按需最小 (如 44K 用 49152), 单请求场景
显式 `--max-seq 1`; 未 pin `--max-len` 时按 `--mem-fraction` 自动推导
(auto-length)。预算器可对部分超预算配置执行 CAPPED 回退；这不覆盖
系统其他进程、运行时异常输入或全部主机内存分配。

## 文档

- [AGENTS.md](AGENTS.md) — 开发入口, 面向 agent 的项目导航
- [docs/](docs/README.md) — 开发记录文档体系 (状态、架构、阶段计划、日志)
- [reference/](reference/) — 外部参考项目 (只读, 不参与构建)

## 参考项目

| 项目 | 用途 |
|---|---|
| [qwen35-thor](https://github.com/thomas-hiddenpeak/qwen35-thor) | 同硬件 Qwen3.5 推理引擎, 架构模式参考 |
| [sglang-ssd-stream](https://github.com/garnermccloud/sglang-ssd-stream) | PLE SSD Stream 机制参考 |
| [vllm](https://github.com/vllm-project/vllm) | qwen4_exp 完整实现, MTP/QSA 权威参考 |
| [Qwen3 Embedding](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) | Apache-2.0专用嵌入权重及架构/分词合同；原生后端开发中，权重不随代码仓库分发，见[实施计划](docs/SERVE_NATIVE_EMBEDDING_BACKEND_2026-11-11.md) |
| [thor-probe](https://github.com/thomas-hiddenpeak/thor-probe) | 硬件探测方法 |
| [thor-bench](https://github.com/thomas-hiddenpeak/thor-bench) | 硬件性能基线 |
| [KVMem](https://github.com/kvmem/kvmem) | 会话身份、完整状态保存/恢复、存储租约与失败处理；借鉴到 SSD 会话缓存 |
| [NInfer](https://github.com/Neroued/ninfer) | MTP 执行组织、精确元数据与 scratch 复用、独立草稿 head；本项目采用显式 65536 行 BF16 子集 |
| [Strata](https://github.com/Niko1221/Strata) | suffix proposal（本项目固定 k3）、混合模型会话边界与槽位管理思路 |
| [Megatron-LM / Inference](https://github.com/NVIDIA/Megatron-LM) | MoE 固定 IDs/当前分数诊断、上传源所有权、数值负控与调度生命周期 |
| [ActKV 社区实现](https://github.com/melis2023/kvmem-actkv)、[actkv-open（非官方）](https://github.com/Maurya30/actkv-open) | LRFU、置信度预算与 KV 淘汰/搬移的源码研究；评分及近似淘汰未移植 |

本轮五个参考项目的学习已收尾；研究不等于整套移植或运行时依赖。
实际审阅版本、已吸收能力、验收边界与暂缓项重启条件见
[阶段收尾](docs/REFERENCE_LEARNING_CLOSEOUT_2026-10-29.md)，其他参考见
[参考项目说明](docs/REFERENCE.md)。

## 原生 Completions 与模型目录

`POST /v1/completions`直接执行原始文本或 token ID，支持批量提示、
`n`、`best_of`、`echo`、真实提示/输出 `logprobs`、`suffix` 和 JSON/SSE。
`max_tokens=0`可只回显与计分提示；默认输出预算为16。
模型目录提供`GET /v1/models`和`GET /v1/models/{model}`，模型id按一个
URL路径参数编码。只读基础模型的删除返回403，未知模型返回404。

`q4t serve --served-model-name private/base --model-alias org/alias`可配置
公开名称和别名；`tools/serve_recovery/service.py prepare`与
`tools/serve_service/service.py prepare`也接收这两个参数并保存在启动
配置中。鉴权、配额、TLS与CORS均可使用原生入口，外部nginx仍可选。
字段/schema、当前验收边界和SDK示例见
[Completions合同](docs/SERVE_COMPLETIONS_2026-11-08.md)及
[官方SDK接入示例](tools/serve_completions/sdk.mjs)。

## 许可证

MIT (项目代码)。模型权重遵循其发布方条款。

Responses/Conversations的原生item形状合同派生自OpenAI Node
SDK 7.47.0类型声明，遵循Apache-2.0，保留[原始许可与来源说明](third_party/licenses/openai-node/README.md)。
提取器使用固定TypeScript5.9.3，仅为开发工具；发行程序不携带或
运行Node、TypeScript或SDK服务。类型合同不代表对应业务接口已交付，
实际能力仍以[当前状态](docs/STATUS.md)与针对性验收为准。

本次原生服务入口复用q4t自有POSIX HTTP解析器及进程/JSON组件，未移植
nginx源码。nginx仅用于可选前置互操作测试及显式旧网关模式；默认新包
不携带它。后续若引入第三方源码，将列明固定版本、来源、使用范围并
保留其许可要求的版权声明。

schema v2的格式回归使用
[JSON-Schema-Test-Suite固定测试数据](tests/fixtures/schema_formats/README.md)，
保留原始MIT许可。隔离worker按需调用系统
[libidn2](https://www.gnu.org/software/libidn/#libidn2)的IDNA2008接口
（LGPL-3.0-or-later库接口），不复制库或启动外部程序。该格式能力
仅用于hostname/email，缺失时局部报依赖不可用，不停止健康模型。
schema v2已完成针对性业务验收并安装本机默认；工作分支尚未合main。
实际交付及剩余组合任务见[当前状态](docs/STATUS.md)。


### 可选原生 Responses 访问策略

`access_programs.cyber`支持标准选择及模型/客户端分离授权。可用
`--access-program-policy-file`配置本地模型策略，发行prepare用
`--access-program-policy`封装私有副本；不依赖nginx或云端服务。
本地执行以`q4t_access_program.authority=q4t-local`标识，不能当作
OpenAI Daybreak批准或私有模型。配置、恢复边界与验收进展见
[原生执行策略](docs/SERVE_RESPONSES_EXECUTION_POLICY_2026-11-10.md#原生策略可选配置)。
