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
