# A04 custom工具：文本与grammar输入的完整接入

legacy已交付33c52e4a；继续custom工具定义、named/allowed选择、历史、
JSON/SSE和客户端结果回填。标准wire保持type=custom及custom.name/input，
不把自定义工具伪装为function.arguments。工具由客户端明确执行。

当前模型适配采用原生function/input参数标签与JSON字符串载荷，保留输入的
换行、引号、Unicode、控制字符和标签字面量。模型编码与HTTP字符串
分开；内部编码不泄漏给调用方。增量解码处理转义/代理对，最终独立
解析与拼装结果核对；成功终态及EOF之前不能执行工具。

text、regex、Lark复用已有XGrammar工作器。用户语法作用于解码后的
原始input，而非JSON转义文本；将原始语言映射到JSON字符串表示，
不能因为原文有引号/换行或闭合标签就丢数据或放松语法。编译/匹配
仍受现有隔离进程、时间/内存、取消和回收边界约束，不在HTTP线程
使用无界regex或另拉起外部服务。实现优先使用固定XGrammar的公开
接口，参考目录只读；标准函数与legacy行为直接受影响项回归。

先成组开发，再冻结定向CPU清单：定义/选择/历史、字符串分片与
Unicode边界、真实text/regex/Lark语法、混合工具、SDK终态和错误。
真实短验上限2启动/8Chat/900秒，具体请求在开发完成后冻结；实际
只读项目资料DSL用于闭环，不执行任意生成代码。不做完整性能或
全模型质量测试；通过前默认33c52e4a保持。

A04未完成前继续实施，之后推进A05及其他全部总计划。编译支持、
parser单测、完整业务与发布分别登记，不能用阶段完成结束总目标。

依据：本地已冻结Chat create参考；[官方函数与自定义工具指南](https://developers.openai.com/api/docs/guides/function-calling)。

## 语法兼容实现决定

固定XGrammar的FromRegex采用ECMAScript子集，缺Rust flags、Unicode
属性及部分分组，不能直接充当官方custom regex实现。增加固定
regex-syntax 0.8.8（无传递依赖，MIT/Apache-2.0）静态解析组件，
Rust仅构建时需要。原始regex转换成Unicode EBNF；锚点/词边界
由有界NFA的前后字符类别闭包保留，不忽略断言。Lark词法regex
经相同路径，最终仍由XGrammar有界工作器编译/匹配；故障只影响
当前约束请求，普通生成没有新增运行时外部服务依赖。

官方语法依据：[工具指南](https://developers.openai.com/api/docs/guides/function-calling)、
[LLGuidance语法](https://github.com/guidance-ai/llguidance/blob/main/docs/syntax.md)。
实现尚待针对性正负语言样本、原始input编码和工作器验证，不据此
宣称全部通过。定义16KiB、展开512KiB及既有进程资源边界保留。

## CPU结果与真实业务冻结

`.q4t-work/serve-custom-tools-20261108/cpu-01`：43 C++、72输入/schema、
16 Python、9 JS全部通过，schema生成/源码审核一致；实际有界worker
按编码载荷逐token约束通过。`build-03`四目标零警告。`build-01`
保留三处返回引用告警和测试JSON成员操作错误，`build-02`保留测试
optional<vector>显式构造错误；两项CPU清单准备KeyError未运行测试。

真实计划1启动/8Chat，单租约上限2启动/8Chat/900秒，不自动重试：
text custom SSE实际读取、结果回填后标题/引用、regex JSON、Lark
SSE、custom/function混合真实读取、截断无成功终态、显式取消无执行、
随后同PID greedy MTP答案42。前三种custom格式均通过只读项目
DSL执行器访问真实文件；工具结果包含源SHA和引用。模型历史适配、
客户端完成屏障、运行计数及资源清理分别检查。未通过前不替换默认。

交付前复核发现Lark转义字面量可与内部标记冲突；已改成先解码
字面量再选无冲突标记，regex子语法不再次替换。`cpu-03`重验7项
受影响语法及1项新增JS/Python计划一致性正负例通过，其余复用
`cpu-01`。`cpu-02`3项驱动通过。累计44项不同C++、72输入/schema、
20 Python（含3驱动）、9 JS；最终`build-05`零警告。原生入口和
Guardian重建分别ba6baf0e/7a57af71，与当前默认字节相同。

## 首轮业务失败与定向补验

`business-01`：ef1aeb97、1启动/5Chat、60.072556秒。前四请求
text读取/结果回填标题引用、regex JSON与Lark SSE真实执行通过。
混合任务只生成函数调用，未满足两种工具各一条的业务要求；该轮
明确FAILED，未执行后续三条，模型退出0/资源清理通过。

API的required要求至少一次工具调用，本轮任务另要求两条实际
调用；不削弱后者。补充明确混合模型格式示例，只修改验收请求，
生产/客户端源码与ELF完全不变。`repair.py`补验混合、截断、取消、
同PID普通MTP四条；首轮通过项按同ELF及请求/源码摘要复用。
本项目自定短验上限据具体失败调整为累计2启动/9Chat/900秒，
补验限1启动/4Chat/800秒，不自动重试，不恢复完整质量/性能矩阵。
`cpu-04`4项驱动通过；累计不同Python检查21（含4驱动）。

## 第二次失败与原生模型格式修复

`business-02`：同ef1aeb97、1启动/1Chat、44.830930秒，明确混合
示例仍只生成两条普通函数，缺custom，整轮FAILED。两轮累计
2启动/6Chat/104.903486秒；均正常退出并释放资源，默认未改。
原候选复制到`candidate-before-framing`且SHA核对，失败原件不改。

停止提示词试探。custom工具模型定义适配为一个input字符串参数的
函数，生成/历史/约束/增量解析统一使用模型原生function标签；
按声明名称判定HTTP类型，仍输出custom.name/input原始字符串。
输入参数内保留JSON字符串编码，避免控制字符和闭合标签损坏边界。
这针对混合选择时两套不一致模型格式，不声称已经证明logits根因。

先验证受影响模板/解析/grammar及函数、legacy合同；新增错误参数/
重复参数/混合逐字节/双向语法边界。schema与客户端未改的检查复用。
随后新的完整custom短验1启动/8Chat/700秒，上限累计3启动/14Chat/
900秒；因实际模型适配已变，重验直接受影响的text/regex/Lark/历史。
恢复原始混合用户任务，不加业务特定格式示例，不降低两种工具要求。
不自动重试；此处上限是本任务自行冻结，用户已授权必要GPU推进。

原生格式修复CPU：`build-06`四目标零警告；`cpu-05`46/47 C++与
5驱动通过。新混合夹具遗漏当前必填additionalProperties=false，
生成语义来源摘要未刷新；修正后`build-07`及`cpu-06`仅重验该例与
生成检查通过。累计47项不同C++、72输入/schema、17客户端Python、
5驱动、9 JS通过；未改输入/client证据复用，两个准备失败原件保留。
