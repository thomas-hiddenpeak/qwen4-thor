# SRV-05b：真实推理预算开发合同

基线8bda4b31，默认ecd5d21f/91ce0ed9。此批为业务协议/正确性推进，
不是性能优化；05a已有独立交付，05b代码及CPU已完成、尚待真实验收。继续完整
[工业化目标](SERVE_INDUSTRIAL_PLAN_2026-11-03.md)。

## 输入、计量与结束

- `thinking_token_budget`缺省/null关闭控制；非null必须是[0,2147483647]
  的精确整数，仅parsed messages。与输出隐藏、字段选择和effort独立。
- 预算按实际生成的token逐个累计：自然token开始时处于推理区，或该
  token完成进入推理区的标记时，计1；包括自然闭合标记token，不包括
  EOS或prompt里的前缀。一个token跨多个区仍最多计1，多个推理区共享
  一份请求预算。该保守口径显式版本化，不冒充vLLM所有版本的计量规则。
- 预算用尽且仍在推理区时，选择实际闭合token；已产生部分`</think>`
  就只补后缀。末尾UTF-8标量未完整时先用最小合法continuation补齐，
  再闭合；这些是有限控制token，不继续自由生成推理。最多3个UTF-8
  continuation字节token及8个ASCII闭合token（实际原生完整close为1个）。
- 强制控制token不占上述自然推理配额，但计入completion_tokens及
  completion_tokens_details.reasoning_tokens；额外给出预算consumed和
  forced_closure_tokens，使总数可核对。隐藏前后计量一致。
- 耗尽后在正文中禁止重新完成`<think>`标记；保留工具正文与schema中
  字面标记。预算未耗尽时允许再进入，继续累计。自然提前闭合无需强制。
- 总max_tokens/max_completion_tokens与上下文容量优先；控制token也
  占总预算。不能闭合/完成payload时保持length或既有严格错误终态，
  不增加总预算、不假装已有完整工具调用。

## 选词、MTP与状态

- JSON与SSE共用逐token预算状态；不通过裁剪最终输出实现预算。
- 结构化选择先交集grammar mask和强制token，matcher只接受最终token
  一次。无合法交集明确失败，不能先accept原token再换输出。
- MTP每批最多接受k+1个token；开批时预算至少还要多留一个自然token，
  保证回退到普通解码时预先选好的pending token仍合法。余量不足则
  在任何speculative提交前回退，禁止批中截断已提交KV。
- 初始预算较小可使用原有MTP prefill/缓存恢复，但不执行MTP批；没有
  匹配的draft边界不得保存私有快照。动态回退的pending先进入真实模型，
  后续forced token参与普通forward，缓存只保留完整的早先MTP边界。
  不宣称普通长tail可直接命中；强制闭合与后续history重算必须一致。
- 普通尾部需要按请求限制选词，使用host-logit路径；未指定预算的
  请求保持原有调度、选词和协议。日志分别记录MTP实际批数、接受token、
  回退、自然预算和强制控制token，不能把MTP启动开关当作MTP执行证据。

## 验收出口

先完成成组实现及直接合同：精确输入/null/组合；UTF-8、分片标记、
自然提前闭合、再进入、工具/schema字面值、0/边界/极值；MTP最坏批数
与pending保留；真实grammar受限强制接收、不允许token不改变matcher；
流式与整段输出、usage/accounting和总输出限制。保存原失败，不自动
扩大测试预算。

模型/数值算子不改；生成策略发生受控变化，需要本机真实HTTP质量和
显式执行路径证据，不能仅复用旧协议HTTP。在实现和CPU验证就绪后
冻结具体请求、GPU独占阶段、请求/启动/时间上限及回退条件；本页未
授权无上限测试，也不把05b单项通过当作工业化目标完成。

## HTTP冻结清单（CPU完成后，首次GPU启动前）

实现及驱动整体冻结后在一个GPU lease内运行，计划19生成/12负例/
3启动，上限22生成/12负例/4启动、首次launch起1800秒；预留仅用于
具体失败的受影响项修复，禁止自动重试、重置时钟或覆盖原失败。
生产ELF、完整head、seq1、prefill2048；MTP业务/缓存max_len16384，
plain与实际安装路径max_len8192，均loopback。工具见
[驱动说明](../tools/serve_thinking_budget/README.md)。

1. MTP服务16生成：原无预算、null、0 JSON、1隐藏SSE、16公开JSON/
   隐藏SSE、128自然闭合SSE、thinking关闭且0；严格工具读取+真实
   历史/结果回填2次；schema预算1；普通预算0且总输出1的length；
   严格预算0且总输出1的错误终态；私有SSD缓存seed/restore/fresh3次。
2. plain服务预算0/16共2次；实际默认安装路径预算0健康1次。
3. 12个输入负例：负值、越界、bool/string/array/object、分数及浮点
   舍入陷阱、raw/unparsed组合，检查400精确param且token/worker计数不增。

缓存seed原生CPU分词8467 token，保留thinking历史，要求真实restore
cached_tokens至少8192；比较fresh回答、结束原因和prompt长度，不冒充
所有张量数值等价。预算16/缓存seed要求实际MTP批和回退，0/1/strict
要求无MTP批；自然闭合与forced分别证明。所有预算usage对日志/metrics，
JSON/SSE对版本schema；无预算响应对上一阶段已交付字节语义/usage。
GPU竞争/监控失败ENV_ABORT，仅清理自己进程。通过才配对安装并健康验收，
失败保留旧默认与所有原始记录。当前CPU OFF179/ON188、原生145+后端9、
客户端27、最终驱动9（另5项生产启动预检）通过，零警告；模板120只按不变源码/历史函数身份复用。

## 本轮结果（冻结条目保留）

两次模型加载前启动夹具失败后，剩余启动额度不足完整验收，本轮
已停止；默认未替换。两处夹具已修复并增加真实argv/SSD目录CPU
预检，后续需要另行明确未执行项的有界验收，不在本轮自动扩预算。
[完整事实、失败原件与证明边界](SERVE_THINKING_BUDGET_RESULT_2026-11-03.md)。
