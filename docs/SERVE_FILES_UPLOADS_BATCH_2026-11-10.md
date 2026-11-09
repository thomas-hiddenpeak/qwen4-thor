# A09 原生 Files、Uploads 与 Batch 连续实现计划

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
