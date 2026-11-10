# A09 原生 Files、Uploads 与 Batch 连续实现计划

## 当前进展（2026-11-10）

2026-11-10。A09.5[文件输入可回退交付](evidence/serve-file-inputs-delivery-20261110/README.md)完成：默认43472e76/元数据229e023d/包0dc8948d、rev40 STOPPED；兼容回退2657eb16/包32af8803已入保留历史。15实际CLI、21.440秒、0新增GPU，原Key/显式配额与账本保持。继续A09.6 Batch执行/取消/恢复及全部总任务，未合main。

A09.4 [Uploads可回退交付完成](evidence/serve-uploads-delivery-20261110/README.md)：默认引擎cdb5ced9、元数据d4559e09、v11包96b75252，revision36、STOPPED。8实际CLI/9.794秒、0新增模型/GPU，原Key/配额/用量与容量保持，保留Files旧代。开始A09.5原生文件内容消费，之后Batch及全部总任务；未合main。

A09.1增量multipart、A09.2分块持久IO、A09.3 Files原生路由/传输与
v11发行已经过针对性CPU和真实业务接受并发布，见
[Files交付](evidence/serve-files-delivery-20261110/README.md)。
当前默认6a7404c7/包cde078fa、revision34、STOPPED；A08原包保留。
以下“已确认实现缺口”“尚未测试”等起始记录保留历史语境，
不代表当前Files仍缺失。剩余A09.4—A09.8全部继续，包括真正文件
内容消费和Batch完整8端点，不以Files完成停止。

起始默认6a90506e/包e8e75470，revision32、STOPPED；A08已交付。
本页所有子项都是必做任务，部分端点或存储底座通过不等于A09完成。
A10所需模型后端和A11执行器依赖继续保留，不以拒绝或文档代替实现。

## 已确认实现缺口与输入来源

当前`application_routes.h`、`chat_http.cpp`和原生ingress没有Files、
Uploads、Batch路由；HTTP接收全部body入string，常规上限16MiB。
对象worker只有有界JSON对象，尚无可流式写入/读取的大二进制文件；
Responses的file_id虽可作为历史类型保存，尚未接实际文件内容消费。

以固定官方Node SDK7.47.0四份实际源码建立字段/默认/响应表，身份见
下方。Files需要create/retrieve/list/delete/content；Uploads需要
create/parts/complete/cancel；Batch需要create/retrieve/list/cancel。
Batch枚举包括8个目标端点，已有文本3个之外的媒体/embedding/moderations
继续接A10，不删除这5个目标。SDK允许上传purpose=evals而FileObject
输出enum未列evals，是已发现的上游类型不一致；不能据此拒绝合法
上传或偷偷重写purpose，须单独记录实际服务返回与类型修正证据。

传输按[RFC7578](https://www.rfc-editor.org/rfc/rfc7578.html)和
[RFC2046](https://www.rfc-editor.org/rfc/rfc2046.html)实现：分段边界、
参数引号、文件名与正文分别处理；保留二进制，不使用文件名作为路径。
不复制第三方服务器源码，不引入nginx、外部HTTP/存储服务依赖。

## 实施子目标与必须完成的出口

| 子项 | 必须实现 | 定向验收 |
|---|---|---|
| A09.1 合同与multipart | 冻结全部字段/null/默认/分页；增量multipart、二进制/跨块边界、错误与限额 | 真实SDK multipart字节、逐切分/截断/边界碰撞、回调失败停止、非法参数 |
| A09.2 二进制持久IO | 隔离worker、私有文件/客户端配额、分块校验、原子发布、丢回执与孤立staging回收 | 实际磁盘、配额/损坏/超时/中断恢复；普通文本不依赖文件IO |
| A09.3 Files与传输 | 标准5路由、原生TLS/可选前置；流式上下载；独立受限buffer/截止/取消 | 官方SDK原字节、list/filter/cursor、隔离、删除、入口背压及错误 |
| A09.4 Uploads | 原始part IDs、指定顺序、总字节/MD5、完成原子生成File、取消与1小时到期 | 乱序多part、并发/冲突、重启、MD5不符/丢回执；不可发布半文件 |
| A09.5 文件实际消费 | Responses/Chat实际file_id/file_data/file_url内容解析与媒体交接、持久引用保护 | 本项目真实文档输入/检索、模型答案与引用；路径/下载限额、跨客户端拒绝 |
| A09.6 Batch执行 | JSONL逐行验证、唯一custom_id、原始owner/截止、实际内部生成、逐行结果/错误file、usage、取消/重启 | 实际文本3端点、部分完成后取消/故障；持久checkpoint不重放已执行行 |
| A09.7 其他Batch后端 | 剩余5端点与A10真实模型能力衔接；完整8端点任务矩阵 | 实际后端业务；缺模型资产继续保留任务并提出具体依赖，不假造结果 |
| A09.8 发行与迁移 | 发现/schema/路由/客户端/配额/原生CLI发行同步，回退和存储兼容清单 | 适用有界真实业务、旧状态保护、默认包与证据一致 |

## 资源、身份与恢复合同

- 上传/下载按固定小块处理，原生入口和核心不能为512MiB File或8GiB
  Upload保留整包RAM。SDK所列单part64MiB、File512MiB、Batch200MiB、
  Upload8GiB是协议容量，部署可配置存储/并发配额另列；配额拒绝不能
  伪装为接口不支持。实际物理空间、页缓存和中转都计入统一RAM/SSD。
- 客户端身份来自已认证上下文，不接受请求metadata/user替代。访问
  控制、生成RPM/TPM、后台公平准入与现有独立控制路径保持一致。
- 文件内容与元数据只有完整校验和持久发布后才对客户端可见。未知
  回执保留原始身份，可查询核对，不自动重试上传提交或工具副作用。
- File/Upload/Batch登记真实生命周期与引用图；活动任务保护输入与
  输出依赖，取消/到期后释放实际配额。密钥ring与API文件不能混列。
- IO worker故障只降级对应持久能力；健康核心继续，无自动模型重启。
  Batch只恢复尚未执行且原owner已死亡的行；开始但无确认的行保留
  中断结果和证据，不能重放已执行副作用。原截止与计量预算不重置。
- 外部服务/前置仍可选，新增加密/解码能力复用已有系统库并登记其
  启用条件与失效影响，不扩大核心强依赖。

## 验证顺序

先成组实现，冻结直接CPU清单再测试。非性能任务不跑全正确性或
五档性能；原生传输/存储可CPU实机验证。首次真实业务预算在驱动
完整后冻结，按实际新增生成和故障路径计量；已过且身份未变的证据
复用。当前尚未开始A09测试，不占用GPU；所有子项完成前A09不结案。

## 固定来源身份

```json
{
  ".q4t-work/serve-protocol-20261103/sdk/node_modules/openai/src/resources/files.ts": "ca78d34f73bef9a683b5fb2bbeb57af219478027b54ee243b611a1d4a12e04d7",
  ".q4t-work/serve-protocol-20261103/sdk/node_modules/openai/src/resources/uploads/uploads.ts": "53450b5df300f270948be9d9a4b395518da3bc9d1767c1d740ac5cbfa9d7aaf8",
  ".q4t-work/serve-protocol-20261103/sdk/node_modules/openai/src/resources/uploads/parts.ts": "c32650dd5cae8a377915533d12dcbab8835e47e875b6162107664a8887ab3136",
  ".q4t-work/serve-protocol-20261103/sdk/node_modules/openai/src/resources/batches.ts": "71e29b0995aab78190942e05642fbd37c082a3fcadd5005f3eb57b97d417e164"
}
```

## A09.1 multipart组件首次验证冻结

增量MIME组件及同源SDK编码捕获已成组开发。固定11项C++：

```text
multipart_boundary_quotes_case_and_ambiguity
multipart_binary_content_all_fragment_sizes_and_utf8_filename
multipart_false_delimiter_prefixes_remain_exact_payload
multipart_order_repeated_fields_empty_parts_and_mime_padding
multipart_closing_delimiter_without_final_crlf
multipart_truncation_never_authorizes_publication
multipart_rejects_ambiguous_or_malformed_part_headers
multipart_wire_parts_headers_and_ignored_text_are_bounded
multipart_consumer_failure_and_exception_are_sticky
multipart_large_input_is_incremental_with_fixed_parser_buffer
multipart_actual_official_sdk_file_and_upload_part_bytes
```

先捕获固定SDK实际Files/UploadPart编码（注入仅夹具Response，0网络/模型），
再由真实C++组件按3字节分块解码并核对256种二进制值、Unicode文件名
与字段顺序。其他用例验证逐切分、假边界、截断/双提交、限额、重复
头和回调失败；不宣称HTTP API或实际磁盘已接受。必要host构建，零警告；
未接业务路由，无新增GPU。本次组件通过后继续A09.1字段和A09.2持久
IO及全部其他子项，不结束阶段或总目标。

组件[11 C++/2 SDK编码CPU已通过](evidence/serve-multipart-cpu-20261110/README.md)，保留首SDK目录夹具失败；仅组件接受，不替代A09其余出口。

## A09.2 分块持久IO实现与CPU冻结

使用现有隔离ObjectStore worker和format1原子journal：私有tool_run分块/暂存/读租约、file发布根；无新磁盘文件格式，旧版本可读取而不理解新回收规则。每块最多1MiB，规范Base64保存、SHA256逐块校验；总manifest最多8GiB/16384块，实际JSON/编码字节计既有配额。容量配置与HTTP协议容量分别记录，不能整文件缓冲。

暂存每次追加原子写块+引用；完整请求经调用方确认后，发布根与删除暂存同事务，可附带父Upload CAS。未知回执保留原id/stage_id，不自动重放或清理。读租约与来源根revision CAS同事务，文件删除不破坏已开始的有界读；暂存和读租约最长1小时，未引用块回收；未知类型仍保护整个owner。HTTP路由尚未接入，不能据组件通过宣称Files API已可用。

冻结25项CPU，清单`.q4t-work/serve-files-20261110/required-binary.txt`：12新增实际磁盘/内层校验/二进制/权限/隔离/配额/冲突/删除/到期重启/取消/未知回执与事务恢复，受影响留存和2项IO故障隔离；必要host构建零警告要求，不涉及GPU或全性能/全模型矩阵。全部通过后继续A09.3与其余全部目标。

分块持久底座[25项CPU已通过](evidence/serve-binary-cpu-20261110/README.md)，无GPU；继续实际HTTP/Files/Uploads集成，不以底座组件终止A09或总目标。

## A09.3 HTTP流式接收组件CPU冻结

共享reader增加可信准入设置的增量消费者；固定工作内存预留与总字节分离，单次callback最多8192字节，原绝对deadline涵盖回调。CL/chunked同时适用，framing/trailer全部成功后才允许调用方发布；普通JSON路由和限额保持。8新增测试含按需生成17MiB二进制的CL/chunked（源/接收端都不持有完整body）、容量/100顺序、总量/metadata限额、回调异常/停止、接收全payload后的截断、pipeline、绝对截止与未启用时原限额。合计冻结32项受影响CPU，见`.q4t-work/serve-files-20261110/required-http-stream.txt`。当前尚未接Files路由、无GPU，不代表API已验收；全部任务继续。

HTTP流式组件[32项CPU已通过](evidence/serve-http-body-stream-cpu-20261110/README.md)，无GPU，继续Files/原生TLS实际接入，不作为API或总任务完成。

## Files接口接入细化（开发中）

官方[列表](https://developers.openai.com/api/reference/resources/files/methods/list)核对limit1..10000、默认10000及desc；[上传](https://developers.openai.com/api/reference/resources/files/methods/create)核对batch默认30天、其他用途默认永久，以及created_at锚点/3600..2592000秒。固定SDK字段保持，新增官方查询补足共享CursorPage未写明的默认。FilePurpose中的evals真实保存，不偷偷改用途。

文件名仅元数据，UTF-8/1..1024字节部署边界；512MiB File、200MiB Batch输入，batch/fine-tune后缀.jsonl。multipart字段最多4个、每文本字段4096字节；真实内容消费/JSONL任务验证在后续必做执行器。list worker按100条公开投影，省略manifest；输出逐FileObject计长并发送，避免整页JSON复制。保留8MiB上传/下载与32MiB列表工作预留，小型retrieve/delete走观察分区，list/content走业务分区，健康/取消保留。核心Files HTTP已成组接入但本组尚未构建测试，原生TLS/SDK实链仍在开发，不作为交付结案。

Files核心成组冻结16项直接CPU，清单`.q4t-work/serve-files-20261110/required-files-core.txt`。11新增字段/实际SDK编码/分页跨页/隐藏内部文件/隔离/真实socket下载/截断语义，以及5项受影响原分页/隔离/分类/响应。旧scoped max_body_bytes仍严格取交集，不以流式解码绕过原授权限额；独立file容量与原生TLS路线是下一必做集成项。必要host与引擎构建，本组无GPU，不宣称完整Files接口交付。

Files核心[16项CPU已通过](evidence/serve-files-core-cpu-20261110/README.md)，必要host/实际引擎零警告，无GPU；下一必做项是独立文件容量配置与原生流式入口、实际SDK/业务，不作为A09或总目标完成。

## 文件容量/原生传输定向验证冻结

A09.3 独立文件容量与原生流式转发已成组开发，冻结15 C++、5策略/账本Python、7实际SDK/原生传输与3既有入口检查。私有v4/入口v5显式限额，旧配置不扩大；中途错误/截断/代次切换阻止发布。开始必要独立构建，尚未测试；默认6a90506e保持STOPPED、0GPU，继续发行及A09其余全部任务。

15项C++清单`.q4t-work/serve-files-20261110/required-files-transport.txt`；5策略Python为`test_file_capacity.FileCapacityTests`全部4项及原QuotaStore的C++/Python旧版摘要核对。7实网为`test_files_transport.FilesTransportTests`，其中官方SDK直接核心与TLS/prefix完整Files两链、17MiB+13字节校验，其余为非法trailer、提前413、代次变更、旧入口限额和100-continue。另3既有入口为代次/伪造头、Completions透传与控制容量。仅使用自有CPU夹具，0GPU/模型，不跑完整性能或全模型矩阵；封装版本与迁移、真实模型业务仍必做。

TLS首次误选关闭TLS的host产物，4项启动失败原件保留；仅换启用TLS构建后3通过。剩余提前413实际暴露POLLIN|POLLERR先丢弃响应的问题。修复后冻结5项：提前核心错误、downstream disconnect、malformed response、backend quota429、truncated SSE/no replay。其他已过正常链分支未变，复用并保留来源，不重复两次17MiB SDK链。

容量/原生传输[34不同CPU最终接受](evidence/serve-files-transport-cpu-20261110/README.md)，64次官方SDK实际请求，0GPU；继续发行与引擎实际业务，再全部其余子项。

## 发现/schema/发行迁移定向冻结

A09.3 发现/schema与v11发行兼容已成组开发，冻结6 C++/15 Python：实际Files与配置产出、SDK来源/传输schema、显式v4→v3迁移保留用量/Key、不兼容旧包在发布前拒绝，以及旧包验证。默认v3策略不升级、不扩容；开始独立必要构建，尚未测试，0GPU。继续实际引擎短验、发行及A09其余全部目标。

完整6项C++名单：

```text
client_file_legacy_migration_preserves_capacity_and_usage
client_file_configuration_emits_actual_schema_value
files_schema_emits_actual_records_and_expiration
chat_capabilities_files_separates_capacity_storage_and_consumption
client_file_revision_upgrade_preserves_inflight_and_rate_recovery
client_dynamic_update_preserves_snapshots_slots_and_move_ownership
```

15 Python为 `FilesContractTests` 4项、`ManagedQuotaTests` 新显式旧版迁移与既有CLI账本2项、`DeploymentTests` 新容量阻止旧包与既有回退账本2项、`BundleTests` 默认/历史v1—v10/新v11容量/来源清单/HTTP绑定5项，以及 `FileCapacityTests` 既有持久升级和真实C++管理IPC2项。配置/Files schema验证必须读取本次真实C++输出。另核对生成合同、源码遗漏门禁与实际A08旧包只读校验，不启动服务或GPU。

安全迁移步骤见[客户端策略](SERVE_ACCESS_CONTROL_2026-11-09.md#files-容量策略与旧包回退2026-11-10a09接入中)。只有明确新字段才升级；旧包兼容需要完整两步容量/版本事务，显式null/bool/未知版本拒绝。当前默认v3不需要迁移。后续真实业务预算在驱动就绪后冻结，Files部分接受不能代替Uploads、文件消费、Batch及所有总任务。

## Files真实业务冻结

A09.3 实际业务驱动已成组准备：冻结1模型/3生成/32 Files操作/600秒，同一严格租约；官方SDK直接核心与原生TLS、真实文件下载驱动工具闭环、暂停自有IO worker后同PID stateless SSE及恢复原文件。先执行3项驱动CPU，尚无GPU；默认保持，随后交付及全部任务继续。

驱动`tools/serve_responses/files_acceptance.py`；3项CPU为`test_files_acceptance.FilesAcceptanceTests`。Files原文下载后作为真实工具结果，尚不宣称A09.5的native file_id内容消费。原生入口父准入为模型PID绑定测试适配，使用真实TLS/认证/HTTP/存储；Guardian旧证据复用。暂存目录独立、默认和源身份固定，实际文件请求不自动重试，故障后只按原ID读取；模型正常终止并核对进程、端口、锁清理。

A09.3 首实际1启动/2生成/8 Files操作/58.855秒，57 NVML全OK并独立清理。Files/TLS/真实文档工具闭环已过；存储暂停真实504/object_timeout被驱动错判503，生产不改。冻结1项CPU后仅补1启动/1生成/300秒，累计2/3/32 Files/600秒；原件保留，默认A08保持，全部任务继续。

续验驱动只修504精确错误判定，复制原专属对象状态并校验所有旧证据/来源身份、清理和NVML；拒绝任何生产漂移。额外模型启动用于只续余下一次生成，已完成两次不重做。

Files续验1启动/1生成/6 Files操作/40.253秒已完成同PID存储超时SSE、原文件恢复与删除，40 NVML全OK并独立清理；最后空列表因声明误用nullable非空游标被schema拒绝。官方Files合同为string，修声明并核对所有真实Files响应，生产引擎不改、不追加GPU。累计2启动/3生成/99.108秒，后续交付及全部任务继续。

[官方Files列表](https://developers.openai.com/api/reference/resources/files/methods/list)明确first_id/last_id为string。空页原生返回空字符串，schema禁止把空页声称has_more或伪造游标；输出purpose含SDK既有生成结果专用值，上传仍限输入6用途。冻结`FilesContractTests.test_actual_empty_file_page_and_all_business_file_outputs`，对两个业务目录原wire及负例复核，声明修复不改引擎/入口/模型合同。

## A09.4 Uploads持久生命周期与CPU冻结

对照固定SDK与官方[创建Upload](https://developers.openai.com/api/reference/resources/uploads/methods/create)、[Part](https://developers.openai.com/api/reference/resources/uploads/subresources/parts/methods/create)、[完成](https://developers.openai.com/api/reference/resources/uploads/methods/complete)，保留6种SDK输入用途。官方HTTP列4种而SDK列6种的差异按较宽兼容合同实现。assistants MIME来源为[支持文件](https://developers.openai.com/api/docs/guides/tools-file-search#supported-files)，vision MIME按[图片输入](https://developers.openai.com/api/docs/guides/images-vision)。

Upload上限8GiB/1小时，每Part64MiB；部署每Upload最多8192个Part、组合manifest最多16384块，物理JSON/base64仍计存储配额。并发分块使用同一root CAS，已知未提交冲突有界重试原Part ID，未知回执不重放。完成按传入顺序消费实际分块、逐块SHA256与可选MD5核验，总字节一致后同事务发布File和completed状态；允许选择/重复已有Part，重复块必须字节及哈希完全相同。相同完成参数可核对原结果，不新建File。

取消/到期释放分块，公共Upload 1小时截止不变；receipt只保留到截止后24小时以核对终态，不继续保留过期分块，也不延长File自身留存。没有新增公开非标准GET Upload。动态引用截止由现有worker留存规划执行，提交前额外检查绝对mutation期限；外层过期记录仍可正确返回expired。MD5仅请求时使用现有系统libcrypto私有默认provider，缺失不影响无MD5上传或普通推理；MD5不用于鉴权。

冻结27项CPU，完整清单`.q4t-work/serve-uploads-20261110/required-core.txt`。新增15项含真实SDK编码、64MiB固定块边界、8并发、重启后继续、取消到期、损坏内容、可选库失能与Create/Part/Complete三种未知回执。另12项直接覆盖原Files、原子journal、留存与加密。只构建/验证此组，无GPU；HTTP/TLS/发现/发行尚待下一组接入，不宣称Uploads交付完成。

Uploads生命周期[27项针对性CPU已通过](evidence/serve-uploads-core-cpu-20261110/README.md)，首必要构建漏头文件记录保留、最终零警告，无GPU。继续标准HTTP/TLS、实际SDK和发行，不宣称A09.4已经交付。

## Uploads原生HTTP/TLS成组验证冻结

4个标准POST端点接实际原生handler，Part流式上限64MiB+65536；Create/Cancel JSON上限16KiB、Complete 2MiB，普通JSON与文件客户端容量分开生效，旧配置不扩大。空body cancel兼容SDK，额外字段/查询/非法Content-Type/非完整HTTP不发布状态。取消在核心/原生入口均使用独立32MiB预留；48MiB业务预算或客户端data名额填满不阻止合法取消。新指标q4t_cancel_body_bytes单列预留。

冻结16 C++（`.q4t-work/serve-uploads-20261110/required-http.txt`）、`UploadsContractTests`3项、`UploadsTransportTests`7项及原生`test_response_cancel_has_reserved_capacity_beside_full_stream_controls`1项。官方SDK实际直接核心/TLS带prefix两链、17MiB Part/MD5/原字节、隔离/取消/完成核对；实网覆盖完整MIME后的HTTP trailer拒绝、提前413、data两槽占满仍可取消、旧限额与>4KiB合法JSON。前置父准入仅身份测试适配，传输/SDK/对象worker是真实实现。组件27项证据复用，不再重跑64MiB边界或无关模型矩阵。

首次合同生成在增加路由清单前触发来源门禁，按依赖先登记实际handler/路由与来源，再生成成功；不是运行时测试失败。必要host/实际引擎/TLS入口构建，未启动模型或GPU。本组完成后继续实际业务与发行，未验能力不提前替换默认。

原生HTTP组[16 C++与11 Python已通过](evidence/serve-uploads-http-cpu-20261110/README.md)，官方SDK两链各32实际请求和17MiB+13字节，0GPU。发行审查发现重复Part产生旧v11不识别的manifest；改为重复内容独立不可变块、仍计原存储配额，原BinaryStore实现与已交付v11源文件逐字一致。最终File/Upload/私有copy stage删除同事务，未知回执不清理；错误保留原Part，副本有界回收。冻结7 CPU（required-rollback.txt）及实际保留旧包`state-retention inspect --offline`读取独立测试产物，核对旧解析无uncertain；不启动模型、不检查业务目录。非重复路径的已过HTTP/SDK证据复用，最终适用业务和发行继续。

[重复Part回退兼容验证通过](evidence/serve-uploads-rollback-cpu-20261110/README.md)，7 CPU+1次真实旧包离线检查，0GPU。实际旧包不认识Upload receipt时保守保护scope；已完成File和chunks完全可读，记录保持。继续业务/发行和余项。

## Uploads实际业务冻结

A09.4 真实业务驱动已准备，冻结1启动/2生成/32文件操作/600秒。审查发现发现JSON complete/cancel重复键，已分离布尔能力与语义描述；冻结1 C++实际产出和3 Python（1合同/2新驱动）再短验。模型尚未启动，默认6a7404c7保持；文件原生消费与Batch及全部总任务继续。 实际SDK通过直接核心/原生TLS创建与分块，真实文档完成后下载并驱动两次工具生成；重复Part原字节/MD5错误后恢复/完成核对，取消与外客户端拒绝；精确核对生成用量、清理自有进程/端口/锁。Guardian原实现证据复用，前置父准入为绑定实际模型身份的测试适配。

A09.4 驱动CPU暴露真实迟到Part连接EPIPE：提前409关闭时有未读请求体。另1次诊断发现SDK本地data URI探测误计为重试，已修驱动计数并保留失败；再次诊断确认真实EPIPE。核心/原生明文统一半关闭并限100ms/64KiB只丢弃尾部，TLS复用该合同。冻结4 C++和5 Python直接影响项，不重复已过核心/64MiB矩阵；0GPU、默认保持，后续短验/发行与全部任务继续。 5 Python为新SDK桥接失败项、直接核心迟到body保留409、原生TLS提前413、明文旧容量边界及原生下游断流无重放。

A09.4 [真实Uploads业务接受](evidence/serve-uploads-business-20261110/README.md)：1启动/2生成/20文件操作/53.651秒、52 NVML全OK并独立清理。实际SDK/原生TLS文档工具闭环、重复Part/MD5/原完成核对/取消后409/隔离及精确用量通过。候选cdb5ced9/入口a8c5bba3，默认6a7404c7保持；继续可回退发行和全部余项。 发行冻结8条实际封装/发布CLI，revision34→36后保持STOPPED，原配置/Key/用量不变，保留Files v11旧代；0新增模型/GPU。

## A09.6 Batch实现入口与第一组CPU冻结

2026-11-10，固定SDK batches.ts身份不变，重新核对[官方Batch说明](https://developers.openai.com/api/docs/guides/batch)。创建合同与增量JSONL之后连续接持久任务、逐行模型执行、取消/恢复与HTTP/SDK。50,000请求/200,000,000原字节，embedding合计50,000输入，单模型身份在后端解析别名/默认后比较；实际八端点后端未实现的部分与A10继续，不用枚举通过冒充执行通过。

增量校验只持有一行与固定大小custom_id SHA256集合，原ID/精确小数和实际偏移保留；Stage只允许私有暂存，完整Finish以前不得执行任何行。错误包含行号/参数，未知写回执保持原ID与原原因，不盲目重放。部署行容量96MiB与文件总容量分开，普通请求语义由同一endpoint checker负责，不只检查body是object就接受。

后续任务根与每行checkpoint由ObjectStore CAS保存；先记录开始再执行，完成结果与计量一并持久，未确认行有独立中断结果。后台恢复核对进程身份和原owner，仅调度未开始行；绝对24小时截止不重置。结果按BinaryStore分块生成原生Files，输入/输出/快照引用进入留存图；取消控制有预留容量，普通核心不依赖Batch IO。

本组冻结14项C++，清单`.q4t-work/serve-batches-20261110/required-input-01.txt`，含全字段与事务性错误、逐分片/CRLF/UTF8、重复ID/JSON键、方法/目标/流式、别名/默认、未知回执/取消、实际200MB/50,000行。纯回调夹具不冒充后端或存储验收。必要独立构建后执行；保持已发布默认，无新增GPU。

## A09.6 持久状态CPU冻结

2026-11-10。Batch持久根与128行检查点已成组实现：固定输入分块、原子页/根更新、取消、进程身份接管与24小时原期限；结果文件独立过期不被历史记录无限保留。冻结16新+7直接影响CPU（23项），包含真实进程退出/回执丢失/清理故障，尚未测试或接执行/HTTP，0GPU/默认保持。继续逐行执行、输出文件与全部总任务。

清单 `.q4t-work/serve-batches-20261110/required-state-01.txt`。真实分块和存储worker，删除原File后重启逐字节读回；跨128行原子检查点；取消竞争；原24小时期限拒绝迟到提交；实际子进程退出后接管，活着或身份未知不得接管；创建和页提交回执丢失不重放；辅助读pin清理失败不推翻已成功创建。包括损坏计数保护、私有引用及7项直接BinaryStore/回收行为。模型执行、端点实际解析、逐行结果与恢复扫描尚待接线，组件通过不等于Batch API完成。

2026-11-10。Batch持久状态[23项CPU通过](evidence/serve-batch-state-cpu-20261110/README.md)，3.017秒：实际删File/重启读回、进程死亡接管、24小时期限/取消及创建/页回执丢失通过。构建与合同首次修正原件保留，最终零警告/469锚点，0GPU/默认43472e76保持。继续实际逐行执行、结果Files/恢复与全部总任务。

## A09.6 逐行结果与终态CPU冻结

2026-11-10。Batch逐行开始/完成与结果汇总已成组实现：先持久开始票据，完整JSON/工具/usage结果与计数原子提交；取消/到期保留部分结果，双File与终态同事务、终态私有资源可回收。冻结16新+10直接影响CPU（26项），含129行/跨页实际分块、真进程死亡及3类丢回执；模型响应为明确CPU夹具，尚未接模型/HTTP，0GPU/默认保持。继续实际执行/恢复/协议/业务和全部总任务。

清单 `.q4t-work/serve-batches-20261110/required-results-01.txt`；固定24小时业务期限与文件保留期限独立。真实已死进程的running行只可记中断，不能再取得执行票据；未开始行按取消/到期生成error，原输出完整保存。两个结果File须与Batch终态同事务，配额失败/未知回执不暴露部分文件、不重复记账。结果Files按创建时固定TTL过期，Batch历史不延长其内容寿命；完成后释放私有输入/page/result引用。CPU使用明确端点响应夹具核对完整JSON、工具及usage，不能替代实际模型/SDK业务。

2026-11-10。Batch逐行与双结果File[26项CPU通过](evidence/serve-batch-results-cpu-20261110/README.md)，6.673秒：129行实际汇总、并发开始票据、三类丢回执、死亡接管/取消/到期、配额及终态回收通过。首测试构建与合同来源刷新记录保留，最终零警告/470锚点，0GPU/默认保持。继续真实生成接线/后台恢复/HTTP与全部总任务。

## A09.6 原生完整响应接收通道CPU冻结

2026-11-10。原生有限JSON接收通道已成组实现：直接接既有端点JSON与错误/异常输出，完整保留工具/usage，无socket/回环或二次准入；单回执、容量、取消/期限与SSE/raw/binary拒绝闭合。冻结9新+9直接HTTP行为CPU（18项），尚未接Batch调度/模型，0GPU/默认保持。继续实际生成适配、恢复、全部协议/业务和总任务。

复用SendSimple/SendError/SendExceptionResponse的原始JSON合同，不从文本摘要拼装Chat/Responses结果。HTTP上下文内部输出仅由服务器设置，借用fd只用于原取消检查；socketpair断言0传输。独立上限<=96MiB、严格完整JSON对象/精确数字/无重复键、单次终态，嵌套文档准备的旧detached上下文不能污染父结果。清单 `.q4t-work/serve-batches-20261110/required-finite-01.txt`，真实模型适配与后台调度、HTTP/SDK业务仍继续，组件通过不宣称Batch API支持。

2026-11-10。原生完整JSON接收[18项CPU通过](evidence/serve-batch-finite-response-cpu-20261110/README.md)，0.016秒：真实输出/错误/异常、工具usage、零socket传输、嵌套准备隔离与取消/容量均过。host/引擎首次零警告、472锚点，0GPU/默认保持；两个独立probe源声明同步。继续真实Batch生成适配/调度/恢复/协议与全部总任务。

## A09.6 有界恢复扫描与身份CPU冻结

2026-11-10。Batch恢复扫描已成组实现，每页最多32记录/8活动任务、跨客户端归属SHA核对、游标删除可续扫，不含输入/结果。发现并修复共享owner仅检查boot ID长度会把畸形值误判已死的问题；Batch/Responses扫描统一投影4身份字段。冻结7新+9直接恢复CPU（16项），尚未执行，0GPU/默认保持。继续真实生成/任务调度/协议与全部总任务。

清单 `.q4t-work/serve-batches-20261110/required-scan-01.txt`。实际worker重启、过期活动保护/终态回收、跨scope/删除游标、32条非Batch扫描上限、1MiB私有owner扩展投影与畸形UUID均覆盖。身份未知不授予执行，真正不同有效boot/实际退出PID仍允许原规则。共享Responses扫描同步防止owner扩展泄露，复用9项直接恢复保护。

2026-11-10。恢复扫描首16 CPU为14过/2失败：已保留活动对象的过期原TTL阻止取消终态，修为仅对已有active对象按相同绝对期限CAS更新；新建/普通过期对象仍拒绝。旧容量恢复测试补齐扫描与执行器进入的共同等待，保留原断言。冻结2失败+2新边界+5直接存储/期限共9补验，其余14复用；原失败保留，0GPU/默认保持，全部任务继续。

2026-11-10。Batch恢复扫描与过期收尾[23不同CPU接受](evidence/serve-batch-recovery-scan-cpu-20261110/README.md)：首14/16、修复后9/9，畸形UUID未知、实际有界私有扫描、过期active原期限CAS收尾及普通项拒绝通过。失败/补验原件保留，最终零警告/472锚点，0GPU/默认保持。继续真实生成适配、任务调度/输入冻结/全部协议业务和总任务。


## A09.6 私有输入冻结与真实原生计划接入

2026-11-10。Batch私有输入冻结已成组实现：验证时解析实际端点、文件/历史/模板但不生成或发布Response；不可变快照递归保留依赖并同事务核对，原始行/客户端绑定，逐行execution_owner不随根接管改写。冻结12新+10直接影响CPU（22项），含17MiB、真实文件删除/重启与丢回执；尚未测试或接任务HTTP执行，0GPU/默认保持。继续实际生成/调度/协议与全部总任务。

清单：`.q4t-work/serve-batches-20261110/required-preparation.txt`。Responses保存已解析的原始历史、模板展开请求、会话revision和文件快照，store=false及待增强表格同样冻结；恢复不重查公共父资源，不提前执行模型或发布排队Response。Chat保存原始消息对应私有文件绑定，执行时仍调用原生Chat计划；Completions验证其自身token/echo合同。单行准备对象通过既有1MiB分块保存，元数据限额不截断实际长输入。InputSnapshot新增可选私有依赖，worker在同一发布决策验证归属/根SHA/绑定，retention沿不可变依赖保留；Batch页同样原子核对快照。新格式交付前必须补兼容回退reader。A10其余模型端点仍是必做，不以此文本适配宣称完整Batch通过；任务执行/公平调度/HTTP和真实业务继续。

私有准备[22不同CPU接受](evidence/serve-batch-preparation-cpu-20261110/README.md)，原失败与补验保留，0GPU。继续实际生成/调度/HTTP与其余全部任务，不以组件接受结束A09。
