# A07.7 原生执行策略连续清单

本项继续总计划，pro、moderation和access_programs均为实现任务，不以参数回显或固定拒绝结案。非性能功能只做直接CPU与对应真实业务短验；不作全模型质量/性能结论。当前默认A07.6保持，全部总目标持续推进。

## 来源和本机行为

2026-11-10实际读取固定官方JS SDK7.47.0与[reasoning指南](https://developers.openai.com/api/docs/guides/reasoning#reasoning-mode)、[moderation指南](https://developers.openai.com/api/docs/guides/moderation#moderate-generated-content)、[Daybreak指南](https://developers.openai.com/api/docs/guides/daybreak)。Create参考抓取只提供生成页面入口，不当完整字段依据。

官方pro区分执行模式和effort，增加实际模型工作并汇总token；没有公开其内部算法。本机实现为Qwen实际有界推敲后再回答，明确原生策略，不冒称OpenAI私有算法或等效质量。请求mode=standard保持原路径，pro增加一次推敲；effort独立保留。未知mode给400，不能将未来未知值当已实现。

## A07.7a pro真实执行

1. 从已解析/裁剪、必要时已压缩的实际输入生成推敲步骤，原始消息、媒体与工具定义保留，禁止推敲步骤执行工具。其文本/推理均输出为reasoning item。额度取剩余输出的三分之一（至少1、最多512 token），实际消耗汇总到统一usage与reasoning_tokens。
2. 推敲后将真实笔记作为assistant数据加入临时执行上下文；原工具选择和最终schema保持。最终步骤只能使用同一max_output_tokens剩余预算；不修改持久原输入，不把笔记冒充工具结果或系统指令。
3. 所有步骤共享截止/取消、一次RPM、累计输入/输出TPM、客户端累计输入上限和同一响应ID。各步释放模型/语法资源后续接。耗尽全局输出预算返回incomplete，不补充额度；后台仅恢复未执行排队项，已执行部分不自动重放。
4. 与摘要、加密reasoning、压缩、缓存和JSON/SSE组合；缓存预热仍只输入预填充，不触发推敲。精确前缀缓存复用实际字节/状态；内部推敲不写缓存。
5. 成组开发已完成，冻结19项C++（9项新增、1项扩充发现、9项直接受影响）及2项schema，尚未执行。覆盖模式/effort、预算/无虚构续接、工具与schema保持、笔记权限、原输入/断点、持久事件/取消，以及实际压缩状态续接和摘要共享预算。清单保存在阶段工作目录，证据随验收归档；通过再做实际两步与工具闭环和交付。此子项不结束A07.7。

## A07.7b moderation继续必做

SDK包含moderation.model和input/output的score/block策略，输出为结果或明确错误。接入本机实际分类执行，覆盖输入/输出及实际工具参数/结果，区分文本、媒体和后端能力；不得伪造omni模型或始终安全分数。score不自动阻断正常生成，block需要在被保护内容公开/工具执行前生效；流式终态和失败策略须独立验证。与既有LLM服务共享执行预算/调度，辅助分类失能不能停止健康核心。

## A07.7c access_programs继续必做

请求选择不授予权限。必须同时核对配置的实际模型能力和客户端授权，明确标准默认与显式选择；不能无条件允许、也不能把本地授权冒充OpenAI组织/项目批准。云端Daybreak专有模型/账号由G00边界澄清，原生策略与兼容接入继续实现，未实现项保持未完成。

## 后续

完成本组后进入A07.8逐项合同/发布核对，继续A08持久治理、A09文件/批处理、A10多模态、A11工具执行、A12客户端和R/Q/G其余任务。不得在一个子阶段结束时停止总目标。

## A07.7a 当前验收进展

[19 C++与2schema已通过](evidence/serve-reasoning-pro-cpu-20261110/README.md)，首次空工具依赖缺陷及仅7项补验保留。实际SDK驱动冻结最多1启动/9请求/17模型步骤/900秒：JSON pro、SSE摘要、加密后台原回放/原输入、真实文档工具与严格JSON闭环、1-token不完整、pro预热零输出、自动压缩后pro、同PID普通Chat；精确总usage/RPM/TPM与执行次数。4项驱动CPU先行。默认A07.6未动，实际业务尚未执行。

实际驱动首次因重复start在请求前失败，1启动/0请求/40.127秒已正常清理；[修复与2项补CPU](evidence/serve-reasoning-pro-driver-fix-20261110/README.md)。累计限额改为2启动，其余9请求/17模型步/900秒保持并计入首次时间；模型/生产源码不变，原件保留，仅续未执行部分。

[实际pro业务已接受](evidence/serve-reasoning-pro-business-20261110/README.md)：累计2启动/9请求/17步骤/129.201秒，所有组合、真实工具、精确账本及清理通过；继续可回退交付和全部任务。
