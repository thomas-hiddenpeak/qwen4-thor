# A01/A02：标准输入与消息语义

总目标继续active。N01—N03已交付，本组先修复输入/消息的实际缺口，
后续生成、工具、持久对象和多模态后端任务不删除，也不因本组结束
标记总计划完成。

## 本组实现

1. 将官方37个Chat参数、嵌套消息和相关端点映射到具体实现任务，
   记录缺省/null、输入验证、执行和响应的位置。A01的登记完成不代表
   未实现的A03—A11能力通过；依赖能力完成后统一核对A01。
2. system/developer允许按原顺序出现，多条和中途指令使用原生
   ChatML system边界；developer映射原生instruction角色，客户端
   请求角色不改写。已有单条开头指令保持原提示字节。
3. name通过有转义的消息头/工具结果标识传给模型，不再静默丢失或
   一律拒绝。名称不作为鉴权/配额身份；模型控制标记不能由名称注入。
4. assistant refusal文本/内容块能回填；保留原工具ID关联/结果顺序，
   原生文本、图像/视频的内容顺序不改变。无user的合法会话也能生成，
   不要求额外伪造user消息。缺失的必填content与错误类型在预检前拒绝。
5. 同步能力声明、本地schema和相关客户端检查，既有文本提示保持，
   新输入格式具有独立profile。legacy function/custom tool历史仍与
   A04联动实现，不能用本组部分通过宣布全部工具消息兼容完成。

## 验证

先成组实现，再针对新旧消息字节、名称转义、refusal回填、真实模型
tokenizer映射、预检/工具历史和媒体位置做CPU验证；不跑全量host或
完整模型正确性。真实组上限2启动/8Chat/900秒、严格GPU租约，覆盖
多指令/参与者和实际资料工具多轮；实际执行前冻结请求与身份。
不为纯文档/原件整理重复推理，验收通过后交付并接着推进A03等。

## CPU结果与真实短验准备

33项C++、71个schema/生产校验用例、19个提示字节/tokenizer用例、
1项固定OpenAI Node SDK profile检查和3项短验驱动检查通过。新增
格式与固定旧提示分别验证；Python tokenizer与生产token ID一致。
媒体位置在CPU检查，没有追加视觉GPU验证。

保留首次夹具ID拼写、未重新configure新增CMake目标、schema来源
摘要未重新生成以及媒体feature-gate夹具分类错误。71项中70项
已通过复用，仅修复并重验1项；代码正确拒绝禁用媒体，合法形状
本身不应由schema拒绝。构建零警告；默认引擎与服务尚未替换。

真实短验固定为1模型、6条Chat，硬上限2/8/900秒，同一严格租约。
使用已固定Node 24.18.0/OpenAI SDK 7.47.0，禁用自动重试：多指令
JSON、按name查询参与者信息的SSE、refusal历史、无user指令、
实际读取docs/MODEL.md及带工具name/中途指令的最终引用。预检同步
保留实际提示与token；结束核对同PID、请求计数及进程/端口清理。

## 仍须实现的合同，保持总目标开放

[37字段映射](evidence/serve-goal-reaudit-20261108/chat-field-mapping.json)
继续作为A01入口，不把字段白名单当作支持。A01尚需落实缓存参数、
safety_identifier/user、service_tier、verbosity/moderation、完整
stream_options/default/null等；metadata/store与A08持久对象联动。
A02的legacy/custom工具历史、其余reasoning_effort及音频/文件等
内容还须与A03/A04/A10实现一同验收。本组只推进已列出的消息子集，
这些任务没有被取消或判定完成；接着推进标准生成参数和工具合同。

## 依据

[官方Chat创建合同](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
于2026-11-08读取；37个顶层字段及嵌套消息以当前页面核对。端点
OpenAPI返回包含外部$ref，不能把未展开的schema说成完整规范。
模型目录的tokenizer_config.json只读：原生模板只接受单条开头system，
没有name/refusal且强制user。本组是明确的服务适配扩展，新增提示
格式另行验证，不能声称它本来就与模型原模板逐字节等同。
