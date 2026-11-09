# A07 剩余执行能力逐项推进

2026-11-09，源码基线 f979a120。原生接入 N01—N07 已交付；高级
Responses 合同继续实施。阶段通过不是 A07 或总计划终点。

## 审计结果与实施次序

`ResponsesExecution::RequiredExecutors` 仍拒绝 auto truncation、摘要、
加密推理输出、非 standard mode、prompt、access_programs、moderation、
prompt_cache_options 和 context_management；输入侧加密/文件/工具引用
也有未接通依赖。它们是实现任务，503 不能作为支持或结案依据。
基础 JSON/SSE、持久响应/会话、后台与取消、Chat 持久化已交付，
不按旧阶段日志重复开发。N04 身份/配额连续性证据已可复用。

| 子项 | 原生实现与验收出口 |
|---|---|
| A07.1 上下文裁剪 | `truncation:auto` 按实际模板/tokenizer/媒体展开后容量选择历史前缀；保留当前 user turn、call/output 与 reasoning 邻接关系；不删除原会话记录，不用裁剪绕过客户端限额；前台/后台同路径、实际 usage 与 SDK/HTTP 业务 |
| A07.2 推理摘要 | 真实摘要执行及 concise/detailed/auto，增加真实 summary items 和增量/终态；不能把原推理复制改名为摘要；资源、取消与 usage 覆盖额外模型工作 |
| A07.3 加密推理往返 | 本地认证加密、客户端/模型/模板与前缀绑定、密钥持久及轮换、完整性/重放边界；store=false 与持久会话共用；仅本服务签发状态可解析，不能假装跨厂商密钥互通 |
| A07.4 压缩 | context_management 与独立 compact 路由的实际压缩、可回填 opaque item；与 A07.3、A08 历史及 A11 工具协调，准确记录额外生成和失败，不能只删旧数据冒充压缩 |
| A07.5 Prompt | 本地版本化模板注册、变量类型和引用解析、客户端归属及不可变版本；实际进入模型上下文，不只是回显参数 |
| A07.6 缓存 | 对接既有原生前缀/会话缓存，显式 breakpoints、mode/TTL/retention、命中/写入 usage 与客户端隔离；媒体状态与 A10 合作完成 |
| A07.7 执行策略 | reasoning mode、moderation、access_programs 的明确模型/策略执行；复用现有模型和工具适配，不能把选项吞掉或无条件允许 |
| A07.8 合同交付核对 | 所有输出/输入 items、include/SSE、错误/取消/重启、官方 SDK 与发布声明逐项核对；A01/A02 余项同步闭合，仍有缺口时保持任务未完成 |

## 来源与边界

继续冻结 `serve-responses-fields-v1.json` 的官方 JS SDK 7.47.0 类型及
既有来源摘要，避免随网页漂移悄悄改口径。本次实际检索官方
[Create 参考](https://developers.openai.com/api/reference/resources/responses/methods/create)，
其自动生成页面抓取仅提供入口，web 抓取超过大小上限；不把该简短
页面当完整 schema。另实际读取
[会话状态](https://developers.openai.com/api/docs/guides/conversation-state)和
[推理](https://developers.openai.com/api/docs/guides/reasoning)文档：
持久会话与手工 output 回填均需保留 item 身份；推理摘要与加密状态
有不同用途，不能以一个替代另一个。新抓取版本差异单列到合同审查。

当前先实施 A07.1，之后依赖顺序继续所有子项。裁剪只作用于实际
模型上下文，不修改持久 input_items/Conversations；用户显式请求的
instructions 保留，默认 disabled 的行为不改变。最多处理既有
4096 项输入，循环随请求截止/取消退出，每个候选用真实计量决定，
不假设字符数等于 token 或裁剪后 token 数严格单调。

## 验证安排

成组开发后冻结直接 CPU：完整/超长/单当前轮、工具成组、推理邻接、
非法输入不被裁剪掩盖、额度/截止/取消、后台恢复和持久历史保持。
接入实际官方 SDK 的 JSON/SSE 与错误消费。实际模型短验另在驱动
完成时冻结，默认上限 2 启动/8 生成/900 秒，严格租约，不复测无关
完整性能和全模型正确性。通过后可回退发布，随后继续所有余项。
当前 A07.1 已开始编码，尚未构建/测试，也未替换已接受默认。

### A07.1 直接验证检查点

16 项 C++ 与 2 项驱动 CPU 已通过，host/实际引擎零警告，
[包含首编译失败的原件](evidence/serve-responses-context-cpu-20261109/README.md)。
实际 Qwen 分词及官方 SDK 的 JSON/SSE、完整 input_items、真实文档
工具与后台请求仍待短验。候选 bb6e702e，默认 N06 保持。

### A07.1 实际业务与交付

[真实业务](evidence/serve-responses-context-business-20261109/README.md)
累计 2 启动/7 请求/93.414 秒，91 NVML 全 OK；真实 42 token 与
JSON/SSE/后台及完整历史、文档工具/严格 JSON 引用通过。
[可回退交付](evidence/serve-responses-context-delivery-20261109/README.md)
8 操作通过，默认 bb6e702e/包 c360746e，revision10、STOPPED。
首驱动及准备失败全部保留。继续 A07.2—A07.8，全部仍必做。

### A07.2 执行设计与开发

开始把摘要作为同一外部请求中的后续模型步骤接入：输入取本次真实
生成的 reasoning content，明确要求 concise/detailed/auto 摘要；
结果使用独立派生 reasoning item 的 summary_text，保留原 item，
不能复制原推理并改字段名。没有推理内容时不伪造摘要。

共享原请求的准入、截止/取消、HTTP/SSE 与存储身份。主步骤释放
模型槽/缓冲后才执行摘要步骤；摘要生成占用剩余 max_output_tokens，
额外输入/输出均进入 usage 与客户端 TPM，整个请求只计一次 RPM。
成功终态等待摘要步骤与持久提交，耗尽/失败/取消保留真实部分结果。
后续压缩与内建工具可复用这个内部步骤机制，不经外部服务回调。

目前已编写独立 summary item 与四种 SSE 事件、终态/取消的协议路径，
复用现有持久检查点与错误前缀；直接用例已写，尚未构建/测试。
实际摘要模型步骤、共享预算和业务验收仍在实现，不能据此宣称摘要
可用或修改已接受默认。当前拒绝点仅在执行接通后移除。

A07.2 成组实现已接通实际模型步骤与共享预算/请求计量，源码候选已移除摘要 executor 拒绝点并声明实现能力；冻结 23 直接 CPU 与实际线协议检查，尚未验证或交付。已接受默认仍为 A07.1，不能把源码声明当发布验收。

A07.2 [23 C++/3 驱动 CPU/64 schema 值](evidence/serve-responses-summary-cpu-20261109/README.md)已过，首夹具失败原件保持。实际短验最多 2 启动、8 外部请求、13 内部生成步骤、900 秒；计划 1 启动、三种真实摘要、后台/SSE 回放、无推理与预算耗尽、真实文档工具闭环与 Chat，含合成测试客户端的实际 RPM/TPM 账本。这里只验证功能及直接受影响行为，不作性能结论。

A07.2 [实际业务已通过](evidence/serve-responses-summary-business-20261109/README.md)：累计 2 启动/8 请求/13 内部步/115.690 秒，112 NVML 全 OK。首回放 framing 缺陷已修并保留失败，复用 3 次生成，完成余下 5 请求及原记录回放。三种摘要、后台/SSE、共享预算、一次 RPM/累计 TPM 与真实文档工具闭环接受；继续可回退交付，全部剩余子项仍必做。

A07.2 [默认交付完成](evidence/serve-responses-summary-delivery-20261109/README.md)：引擎 8b4899cd、包 bbb4c13b、revision12，STOPPED。8 实际命令通过，原业务账本保持，旧 A07.1 保留回退；两次发布前准备失败也保留。继续 A07.3 加密状态及全部余项，不在此停止总任务。

### A07.3 认证加密与密钥基础开发

原生 AES-256-GCM 固定 256 位密钥/96 位随机 nonce/128 位 tag，使用系统 OpenSSL 3 的独立 provider context，懒加载；错误只影响加密请求，不传播为模型失能。解密必须通过最终认证才返回明文。密钥经既有隔离对象 worker 原子持久，内部 scope 与客户端空间分离；每 key 最多 2^20 次 seal、最多 8 个保留 key，轮换要求当前 revision，旧 key 只能显式退役。普通回退仍能读取原有对象格式。

冻结 16 项直接 CPU（NIST 已知答案、所有认证组件篡改、长度/随机数、不可用恢复、重启/归属、轮换/版本/容量/退役、持久故障、并发、取消、普通 Responses 继续）；尚未构建/测试。真正 Responses 双端与前缀绑定、管理入口和真实业务继续必做，本基础不算 A07.3 交付。

密码 API 依据 [OpenSSL EVP](https://docs.openssl.org/3.0/man3/EVP_EncryptInit/) 和 [独立库上下文](https://docs.openssl.org/3.0/man3/OSSL_LIB_CTX/)，已知答案采用 [OpenSSL 公布的 NIST 数值向量](https://github.com/openssl/openssl/blob/openssl-3.0.13/demos/cipher/aesgcm.c)；未复制其实现代码，也未引入外部服务进程。

A07.3 [16 项密码/持久密钥 CPU](evidence/serve-responses-encryption-crypto-20261109/README.md)全过，host 零警告、首编译失败保留，0 GPU。继续协议双端/前缀/管理与真实业务，不把基础标为整体功能完成。

A07.3 [双端 18 CPU 与 22 个实际协议值](evidence/serve-responses-encryption-protocol-20261109/README.md)通过。密文使用 `q4t1:key_id:nonce:tag:ciphertext` 版本封装，认证整个实际 reasoning item 和来源前缀，并以客户端/canonical model/template/item 作为 AAD。完整前缀/匹配后缀/仅密文回填均可，缺失前缀恢复、冲突拒绝；同客户端重用允许，只恢复上下文，不重放工具。原输入持久项不改，生成 token/容量使用恢复后历史，`instructions` 仍属于新请求。原 Qwen reasoning 明文投影仍可见，不能误称整段输出保密或磁盘加密。

首 canonical shape 错误已修并保留失败；host/引擎零警告、候选 687fc18f，0 GPU。继续原生本机密钥管理、实际官方 SDK/模型业务和交付，不在本阶段结束 A07.3 或总目标。

### A07.3 原生密钥运维接入

新增 `q4t state-keys inspect|initialize|rotate --object-store-dir ABS_DIR`；轮换指定 `--expected-revision N`，退役逐个 `--retire KEY_ID`。在线仅同 UID Unix 对端可操作，不开放给业务 HTTP 客户端、不打印密钥、不重启模型。`--offline` 显式采用同一隔离对象 worker/独占锁；运行服务持有对象锁时拒绝，不自动关闭它。未知回执先 inspect，永不自动重放轮换。管理线程保留固定 2 连接、请求 4KiB/回执 16KiB、2 秒对象操作、1 秒写回；失败只关当前请求并继续同一线程，不增殖或重启模型。

`/v1/config.application_state_health` 增加 `key_admin_ready` 和固定 false 的 `key_admin_readiness_dependency`，保留旧记录兼容。冻结 12 C++/2 schema CPU（真实 Unix/CLI/实际提交丢回执/竞争/空闲/取消/依赖恢复），尚未构建测试。默认保持，之后继续必要实际业务及全部总任务。

A07.3 [原生管理 12 C++/2 schema](evidence/serve-responses-encryption-admin-20261109/README.md)首轮通过，实际引擎 71cb526c 零警告。准备真实 SDK/模型业务与交付，默认不动。管理信任边界是本机服务 UID；A11 的不可信代码执行必须隔离 OS 身份/密钥目录/管理套接字，不能用服务 UID 无隔离执行外来代码。

A07.3 [真实业务通过](evidence/serve-responses-encryption-business-20261109/README.md)：2 启动/8 请求/8 内部步/102.244 秒，97 NVML 全 OK；密文无状态续聊、在线换钥/旧 key 保留、文档工具、后台原事件/重启、退役拒绝与同 PID Chat 已验。候选 71cb526c，继续可回退交付后 A07.4 与全部任务。

A07.3 [可回退默认交付完成](evidence/serve-responses-encryption-delivery-20261109/README.md)：引擎 71cb526c、原生包 dca3b450、revision14，STOPPED。8 实际操作首轮通过，旧 A07.2 与核心/客户端账本保持。继续 A07.4 实际压缩、回填与独立 compact 接口及全部余项。

### A07.4 实际上下文压缩：执行设计与基础开发

重新读取[官方压缩指南](https://developers.openai.com/api/docs/guides/compaction)，并核对固定 JS SDK 7.47.0 的 CompactedResponse、ResponseCompactParams、compaction item 与事件。自动模式按真实渲染 token 阈值触发；独立 `/v1/responses/compact` 返回下一窗口，客户端可原样回填。输入 compaction 的 id 可省略，不能误用随机新 ID 使本服务状态无法认证。

本地实现分同一任务内的连续部分：

1. 原生历史分区与认证状态：保留 system/developer、当前自动模式 user turn、媒体和未完成工具依赖；standalone 保留用户消息。并行 call/result 合并区间保护，不将待执行工具摘要成已执行。来源完整语义指纹和保留窗口进入认证边界；密文只携带实际记忆与保留项，不暗藏整个原历史抵消压缩。允许原历史、返回保留窗口或省略前缀回填，冲突拒绝。
2. 真正模型步骤与容量：在同一外部请求内调用本机模型生成记忆，禁止工具执行；通过实际 tokenizer/媒体展开确认压缩窗口与后续容量，失败不发布成功压缩，不循环重试求收益。原始持久 input_items/Conversations 保持，只有生成窗口使用压缩结果。无可压缩项按 no-op 记录，不编造模型工作/摘要。
3. 自动模式与完整协议：context_management/compact_threshold、末尾 compaction_trigger、必要 SSE compacting/item 生命周期、后台/取消/重启、额外输入输出/RPM/TPM、原生 compact 路由及官方 SDK，全部接通后进入功能交付。standalone 无 stream/store 参数，不偷换为普通生成请求。缓存选项按 A07.6 同时保留未解决任务，不伪称此处已完成。
4. 针对性验证/发行：直接 CPU 覆盖分区、认证、配额/预算/终态/路由与持久历史；实际 SDK/模型验证压缩后重要事实/工具继续、实际 token 缩减与错误隔离。复用未变基础，不跑完整性能/全模型质量。通过后可回退默认交付，再继续 A07.5 及全部余项。

当前只完成分区/模型记忆请求和认证状态基础源码，写入 10 项直接 CPU，尚未执行。封装 `q4tc1:item_id:q4t1:...` 中 ID 受内层认证；用途绑定与 reasoning 分离。系统角色不被生成的记忆替代；记忆回填为 assistant 数据。压缩模型执行、计量和公开协议尚在继续，不修改 capability 为已支持。默认 A07.3 dca3b450/STOPPED 保持。

A07.4 [15 项基础/回填 CPU](evidence/serve-compaction-foundation-20261109/README.md)最终通过，首公开消息 ID 形状缺口及 2 项补验保留。host 零警告，0 GPU；原生压缩模型步骤、共享预算/计量、自动阈值/compact 路由/SSE/持久化尚在继续，默认未变，不能把基础当功能交付。

A07.4 [执行与协议 CPU](evidence/serve-compaction-execution-20261109/README.md)已接受：23 C++/5 schema/2 官方 SDK、57 线协议值。原生模型步骤、共享预算/累计 TPM/一次 RPM、自动阈值（默认上下文4/5）、显式 trigger 与独立 compact 已接通；自动记忆占共享输出一半且最多512，独立用配置默认上限。实际计量未缩短或生成截断不发布成功密文，无可压缩项不编造模型 token。此为功能实现，不作性能优化结论。既有 SSE 填充改为文档要求的 delta-only，严格 schema/SDK 补验通过。真实业务及默认交付尚待推进，之后全部余项继续。

### A07.5 本地 Prompt：不可变版本与实际上下文

2026-11-09开始。固定 JS SDK7.47.0的 ResponsePrompt 为id、可省略version、string/input_text/input_image/input_file变量映射。当前官方[文本指南](https://developers.openai.com/api/docs/guides/text#version-prompts-in-code)已提示云端Prompt对象退役与代码迁移；这是版本差异，不删去本计划中既定的本地兼容能力。注册接口标为q4t原生管理扩展，不冒称云端账号/Prompt互通。

- 复用已隔离的ObjectStore，不增加外部Web服务；原生/v1/prompts创建、分页查询、版本追加/读取、默认版本选择与删除，实际API Key scope控制所有权。每条记录保留至显式删除并计入现有配额。
- 同一Prompt的版本只追加，不覆盖；默认版本指针用expected_revision比较后更新，旧版本可显式选择。定义与别名一次原子写入，丢回执先读不自动重放。单定义256KiB/64变量/64版本，展开4MiB，容量耗尽明确拒绝。
- 定义包含generation request和变量类型；只对instructions及input内容做一次文本/媒体替换。变量内容不递归解释，也不能改变消息角色、工具名或schema。请求显式参数覆盖模板缺省；模板input在本次调用input前展开，历史仍由ResponsesState归属校验和拼接。
- 提交时解析固定版本并保存已经展开的有效请求和标准prompt引用；后台重启使用该快照，不重新读取可能已变化/删除的注册表。展开后继续真实模板/媒体/限额/工具与token计量，不绕过原合同。
- 先成组实现基础/HTTP/执行/声明，再冻结直接CPU；随后最多2启动/8请求/900秒的真实SDK文本、版本切换、工具及后台短验。已过旧阶段不重复全矩阵。文件变量结构与归属继续连接A09实际文件后端，不能伪造文件内容。
- 本轮发现SSE回放投影仍对非delta添加obfuscation，连同注册/后台直接受影响用例修复；原持久事件不改写。

A07.5 [27 项 CPU/来源检查](evidence/serve-prompts-cpu-20261109/README.md)及随后发现的[输出引用缺口修复、8 项补验](evidence/serve-prompt-output-fix-20261110/README.md)通过。响应、排队快照及 SSE 生命周期均保留固定后的 prompt 引用；首驱动失败保留。必要构建零警告，真实 SDK/模型短验待执行；默认仍 A07.4。

本地管理接口使用当前 API Key：`POST /v1/prompts` 的 body 是
`{"definition":{"request":{"input":"{{question}}"},"variables":{"question":"string"}}}`。
返回本服务 ID 后，通过 Responses 的
`{"prompt":{"id":"返回的ID","variables":{"question":"17+25等于多少？"}}}`
执行。`POST /v1/prompts/{id}/versions` 使用 definition 和 expected_revision
追加版本；`POST /v1/prompts/{id}` 使用 default_version 和 expected_revision
切换默认，`GET /v1/prompts/{id}/versions/{version}`读取不可变定义。
删除只影响未来引用，已保存响应/后台有效请求继续持有原版本和上下文。
