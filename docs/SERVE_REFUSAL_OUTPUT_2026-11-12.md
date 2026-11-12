# A07：模型拒绝信号、输出与流式生命周期

状态：实现准备。此项是完整兼容目标的必做任务，不能以返回
unsupported或仅接受refusal历史字段代替生成能力。

## 已核对的协议与代码

2026-11-12重新读取官方
[结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs)、
[拒绝增量事件](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.refusal.delta)
和[拒绝完成事件](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.refusal.done)。
结构化schema保证适用于完成的非拒绝输出；拒绝、截断和错误分别处理。
拒绝事件需要稳定item/output/content索引、sequence以及对应增量或
最终refusal文本，不能把普通text delta改个事件名就当作实现。

当前Chat模板支持assistant的refusal历史；输出解析器只有content、
reasoning和tool三类。Responses同样没有refusal生产/增量/终态路径。
inline moderation已有独立结果，不能用其flag伪造模型的拒绝。

只读检查当前模型tokenizer_config的added_tokens_decoder，未发现
专用refusal/reject token。因此不能假定存在一个尚未核验的Qwen拒绝
token；须实现并用真实模型验证明确的输出协议适配。

## 本组实施合同

1. 建立模型生成的显式拒绝控制帧；提示模板说明其含义，只有模型
   选择该帧才进入refusal路径。不以“抱歉”等自然语言关键词分类，
   不由审核器或服务预先填入拒绝文本。控制帧属于模型适配层，不是
   新的OpenAI请求字段，不宣称是原模型自带的特殊token。
2. 解析器增量处理控制帧、UTF-8片段与关闭状态；只在合法输出起点
   识别。JSON字符串、工具参数、reasoning及普通文本中的同形字节
   保持数据语义。控制字节不泄漏给客户端，不伪造外部工具调用。
3. 约束解码明确允许模型选择拒绝分支；非拒绝路径继续满足原schema
   和tool_choice合同。拒绝不能成为未完成schema或无效工具的兜底。
   语义receipt要能区分真正拒绝分支和缺失校验，不能直接关闭校验。
4. Chat JSON/SSE的refusal字段、真实token logprobs、Responses拒绝
   content part与delta/done/item/terminal顺序、store/回填一并接通。
   截断、取消及错误保留各自终态，未闭合输出不能伪装成功。
5. 模板、缓存身份及能力声明同步；普通回答、工具和schema保护项只
   做直接受影响回归。SDK直接消费实际事件，不能仅用手工构造fixture
   证明模型生产能力。

具体控制帧编码和接线须在读取完整约束/receipt路径后定稿；本文件不
把设计草案或单独serializer实现标记为已交付。

## 有界验证

先成组完成模型适配、解析、约束、transport、状态和logprobs接线，
再冻结并运行针对性CPU用例；已有且身份未变的能力证据复用。
CPU覆盖每个分片边界、转义/同形文本负控、schema/tool分支、预算耗尽、
错误/取消、完整事件顺序和旧历史回填。

运行时短验另冻结具体请求和候选身份，初始上限1模型启动、6请求、
900秒，覆盖真实模型拒绝JSON/SSE、非拒绝schema、工具闭环及标准SDK。
此额度尚未使用，未自动授权重试。严格GPU租约保持；不开展五档性能、
全模型质量、长稳或保留视觉候选测试。失败按具体机制修复、保留原件。

完成此组后继续A07其余items/字段、A11内建工具执行、A10后端与A12
矩阵，以及全部R/Q任务；这不是总目标的停止点。
