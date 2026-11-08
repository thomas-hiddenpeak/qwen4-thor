# A07 Responses 原生实现与 A08 状态衔接

2026-11-08建立、2026-11-09更新；功能开发，不作性能优化结论。
同步首组已通过真实业务短验，完整A07/A08仍在实施。
默认继续使用已接受 A06 包；本组不把 store、background、历史或媒体
缺口从总计划删除。实现/测试子组结束后继续下一项，总目标保持 active。

## 合同与来源

实际官方 JS SDK 7.47.0 的 Responses 类型、流式累积器与解析器作为
冻结依据；[字段清单](serve-responses-fields-v1.json)记录 SHA256 及
32 个顶层字段的责任任务。任务归属不是排除清单；尚未完成的合法
形状仍为缺口，不能将固定拒绝计为兼容通过。

复用原生模型执行、结构化约束、配额和取消。请求转换在进程内完成，
不得转发到另一个 HTTP 服务。Responses 单独组织 output items、
sequence_number、工具参数增量及 response.completed/incomplete/failed；
不发送 Chat 的 [DONE]，不把原始 reasoning_text 假称推理摘要。

官方[工具合同](https://developers.openai.com/api/docs/guides/function-calling)
另核对缺省 strict：Responses 尝试兼容的严格化，不能兼容时显式返回
resolved strict=false；显式 true 不得降级，显式 false 保持原 schema。
Chat 的缺省行为不改。此规则与输入字段来源一起冻结，不默认为两种
接口的工具参数语义完全相同。

## 连续实施次序

1. 输入归一化：instructions、消息/图片、function/custom 调用与回填、
   text.format、生成选项。保留 item/call 身份、角色及内容顺序，错误
   参数指向原始 Responses 字段。
2. 原生输出状态机：稳定 item 身份、完整 SSE、真实 token usage 与概率、
   截断 incomplete、错误及无分配异常终态。未完成调用不授权执行。
3. 接入共享执行器、标准 SDK 与入口/发行声明。store 缺省为 true，
   必须与 A08 本地对象存储、隔离、TTL 和重启恢复衔接；不可静默丢弃。
4. A08 previous_response_id/Conversations/background/cancel，A09 文件、
   A10 媒体/缓存，A11 工具与摘要等依赖继续完成；不因文本子组通过停止。

## 针对性验证

- CPU：输入语义及负控、工具历史关联、schema 组合、SSE 生命周期与
  错误、截断、取消、资源预算；实际官方 SDK create/stream/parse 消费。
- 对直接受影响 Chat 合同作回归，未改的数值/性能证据复用；不运行
  完整正确性或五档性能矩阵。
- 功能接通后冻结具体真实业务请求，首组上限 2 次模型启动、8 次生成、
  900 秒；完整阶段使用同一严格 GPU 租约并继承 managed lease FD。
  无理由重复验证或失败后扩大预算不属于本计划。
- 实际通过后封存来源、原始结果与回退包，更新适用默认版本并提交
  推送，再继续其他必做项。CPU 模块通过不能代替 HTTP 或总任务验收。

## 输入与输出模块检查点

`responses_input` 在进程内归一化消息、图片、reasoning、function/custom
items 和回填；完整原始 items 留给 A08。工具结果按 call_id 关联，
拒绝缺失、重复和串用；助手 phase 以明确文字标签适配 Qwen，混合
refusal 保序拆成助手片段。原图 detail 不被改成 auto；媒体后端仍需
A10 实现各 detail 与工具图片输入，这一适配测试不是媒体业务通过。
文件、item 引用、加密内容及内建工具须由所属模块解析后接入，当前
解析依赖错误只是中间状态，仍是全部总计划的未完成工作。

`responses_output` 组织稳定 items/sequence、reasoning_text（summary
保持空，不伪造摘要）、function/custom 增量、真实计数入口及三类终态。
长度耗尽保持 incomplete，未完成工具无 arguments/input.done；完成
的富 schema 调用即使处于 incomplete 整体响应也必须有语义回执。
HTTP 错误可以绑定 Responses 渲染器，内存不足采用固定缓冲区 error
事件，保留同一序号与单终态；既有 Chat 错误语义不变。

本组 30 项不同 C++、7 项实际官方 SDK CPU HTTP 夹具通过；5次构建
零警告。初次测试夹具的生成前缀、合法多片空参数和缺少 messages
共3次失败保留；只修夹具及重验失败项，不修改解析器迎合断言。
SDK直接消费 C++ 输出的原始 JSON/SSE，不能等同真实模型推理。
[原始记录](evidence/serve-responses-core-20261108/README.md)含失败与源码摘要。

## 2026-11-09 共享生成与状态读接口

已接进程内共享生成、真实增量/概率/usage、JSON/SSE终态和错误观察。
store缺省true，持久事务确认前不发送completed；回执丢失返回已知ID供
查询，不盲目重放或覆盖。已接POST /v1/responses，以及按ID查询/删除、
input_items分页与include、原始事件续读；原生入口共享身份、容量、TLS。
previous_response_id延续已解析input/output，前次instructions不自动继承。
Conversations使用同一隔离对象worker及原子提交，存储故障不改变核心readiness。

[本组证据](evidence/serve-responses-execution-20261109/README.md)：23不同C++、
6官方SDK CPU、3实际原生入口CPU通过，3次构建零警告。原始失败保留；
发现并修复最终item概率原字节与delta/done事件形状混用，包含部分UTF-8
字节保真。这一CPU检查点未运行GPU或替换默认，不能认定A07/A08交付。

后台排队/执行/取消、进行中的持久事件、重启终态恢复、超额失败记录
闭合、语义item相等与独立item保留治理仍须实现。摘要、压缩/自动截断、
加密推理、prompt及工具/媒体执行依赖继续纳入A07—A11，临时依赖不可用
不能作为兼容通过。首组同步业务短验结果见下文，继续剩余实现。

读取遵循官方[输入items](https://developers.openai.com/api/reference/resources/responses/subresources/input_items/methods/list)、
[删除](https://developers.openai.com/api/reference/resources/responses/methods/delete)和
[后台续读](https://developers.openai.com/api/docs/guides/background)合同。
当前续读使用已保留原事件，终态缺事件返回503，不从文本假造事件。


## 2026-11-09 同步真实业务首组

[原件](evidence/serve-responses-business-20261109/README.md)：1启动/7生成，
54.739秒，严格租约完成0；53次NVML全部OK。官方SDK实际消费JSON/SSE，
验证概率原字节/持久查询/事件续读、34个推理token、真实项目资料工具、
previous_response_id工具回填与约束JSON、截断incomplete及同PID Chat/MTP。
模型退出0、自有端口与锁释放，默认未替换；已验8a53f39d候选单独保留。
后台、取消、恢复等必做项继续推进，同步验收不等于完整A07/A08完成。

## 活动状态保障子组

初始created/in_progress事件与初始记录一并确认后发布；运行中累计新增
64KiB或距检查点500毫秒，在收到下一片增量时提交实际snapshot及原事件。
成功终态仍以原子事务回执为准，客户端可按原sequence续读；无增量时
不制造进度或事件。已知配额/对象大小失败可以消费活动记录的4KiB预留，
保存无部分输出/无事件的failed终态，保留输入及原因；流式续读明确报告
原事件不可用。未知提交不套用此回退，需按既有ID核实，不覆盖可能的成功。
item身份比较忽略JSON对象键顺序，保留精确数值、类型和数组顺序。
7新增CPU、4直接回归通过，[原件](evidence/serve-responses-checkpoints-20261109/README.md)
保留配额夹具失败及修复定向重验。本子组不追加GPU，继续后台与恢复。
