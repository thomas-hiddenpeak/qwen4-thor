# A07：模型拒绝信号、输出与流式生命周期

状态：成组实现及针对性CPU、真实模型业务通过，可回退发行待完成。此项是完整兼容目标的必做任务，不能以返回
unsupported或仅接受refusal历史字段代替生成能力。

## 已核对的协议与代码

2026-11-12重新读取官方
[结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs)、
[拒绝增量事件](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.refusal.delta)
和[拒绝完成事件](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.refusal.done)。
结构化schema保证适用于完成的非拒绝输出；拒绝、截断和错误分别处理。
拒绝事件需要稳定item/output/content索引、sequence以及对应增量或
最终refusal文本，不能把普通text delta改个事件名就当作实现。

接线前Chat模板支持assistant的refusal历史；输出解析器只有content、
reasoning和tool三类。当时Responses同样没有refusal生产/增量/终态路径。
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

具体编码为`<q4t_refusal>非空拒绝文本</q4t_refusal>`，使用普通词表
字节；仅识别初始最终回答、最多16个ASCII前导空白之后的完整起始
帧。普通答案开始后、JSON字符串、工具参数及reasoning内的同形
字节不作为拒绝。完成后仅允许最多16个ASCII空白；混合/未闭合
帧失败，预算截断走对应incomplete/length合同，不假装完成。

全部已有约束模式都增加独立拒绝分支，worker只为已闭合非空帧发
`q4t-refusal-binding-v1`/`q4t-refusal-value-v1`独立域receipt，parser
核对实际重建字节；无拒绝时原rich schema receipt保持。普通未约束
输出不为此启动grammar worker，仍由解析器核验完整帧。
Chat模板profile更新为`qwen3.8-openai-v4`，真实提示包含适配指令；
raw parse_response=false保持原模板，缓存仍用实际编码前缀身份。
服务不能靠关键词或审核结果制造模型拒绝。本文件不把接线当交付。

## 有界验证

成组接线后冻结80项具体CPU selector及来源hash，原件位于
`.q4t-work/serve-refusal-20261112/cpu-plan-01.json`及
`cpu-required-01.txt`，涵盖新增项及直接受影响的parser、模板、
约束、概率和Responses路径；不是完整host/模型质量矩阵。
已有且身份未变的其他能力证据复用。
CPU覆盖每个分片边界、转义/同形文本负控、schema/tool分支、预算耗尽、
错误/取消、完整事件顺序和旧历史回填。

运行时短验另冻结具体请求和候选身份，初始上限1模型启动、6请求、
900秒，覆盖真实模型拒绝JSON/SSE、非拒绝schema、工具闭环及标准SDK。
此额度已使用1启动/6生成/60.217秒，全部通过，无自动重试。严格GPU租约保持；不开展五档性能、
全模型质量、长稳或保留视觉候选测试。失败按具体机制修复、保留原件。

完成此组后继续A07其余items/字段、A11内建工具执行、A10后端与A12
矩阵，以及全部R/Q任务；这不是总目标的停止点。

CPU结果见[保留原失败的证据](evidence/serve-refusal-cpu-20261112/README.md)：
80项原生用例及19项协议/SDK/客户端/驱动检查通过，来源及声明检查通过；
不代替模型生成验收。Python/JS客户端同步保留拒绝、真实概率和历史，
拒绝或截断不能授权工具执行。

真实业务及清理见[验收记录](evidence/serve-refusal-business-20261112/README.md)。
默认rev72保持；新版须保留原生profile/版本恢复，不能靠删除恢复能力
绕过发行身份和兼容性检查。
