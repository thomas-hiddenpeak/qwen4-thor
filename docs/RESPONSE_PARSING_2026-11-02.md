# 工具与推理输出协议

后续有界业务验收已通过，见[结果与证据](CHAT_BUSINESS_ACCEPTANCE_2026-11-02.md)。
830694ce现已成为默认程序，两head安装健康通过、GPU保持释放，见
[交付与回退](CHAT_DELIVERY_2026-11-02.md)。以下保留此前开发快照。

后续更新：当前HTTP驱动增加能力身份核对，需`--protocol-sha256`及
支持能力接口的新候选。本页旧命令保留作历史阶段说明，不直接用于
当前驱动；统一命令见[聊天接入准备](CHAT_INTEGRATION_PREP_2026-11-02.md)。

## 冻结范围与验收计划

2026-11-02 用户选择补齐输出解析与流式协议。本轮为功能开发，
不是性能优化；复用未变化的模型计算、MTP及缓存数值证据。
基线源码24b97e7，默认程序0c5ab1de/sidecar e6fdd13b。

- messages 默认解析 Qwen XML 工具调用与 think 区域，输出独立
  content、reasoning_content、tool_calls；prompt 保持原始文本。
  messages 可用 parse_response=false 显式保留旧输出。
- 流式正文/推理增量输出；每个工具块完整校验后发送一个标准
  delta.tool_calls，包含稳定 index/id/type/name/JSON字符串arguments。
  不提前发布可能截断或无效的函数参数。正常工具结束为tool_calls，
  长度截断仍为length；残缺/非法工具块返回错误，不发布成功终态。
- 解析器知道模板预填的think状态，不将推理中的工具示例当调用。
  工具名必须来自本次tools；重复参数、非法类型/JSON和超预算拒绝。
  单工具块上限1MiB，每轮最多128次调用；不声称完整JSON Schema验证。
- 支持tool_choice=auto与parallel_tool_calls=true；需要messages及解析。
  其他选择/约束仍明确拒绝；不改模型采样、模板、工具执行职责。
- SSE与非流式共用解析器，取消/解码/解析错误不生成成功结束或发布
  私有会话缓存。UTF-8、usage、raw输出及现有终态合同保留。

CPU：所有chunk切分、UTF-8、预填think、思考中的伪调用、多调用、
参数类型/转义/大整数、截断/未知函数/重复参数/大小限制，以及
SSE重组与非流式一致性。运行发布host门和公共host检查，零警告构建。

GPU/HTTP：启动前复查业务；vLLM有实际业务则只开发、不测试。
空闲时正常暂停现有q4t，完整阶段持gpu_exclusive锁。最多3次测试模型
启动、24次生成请求、10个请求合同负例、30分钟；原始失败保留，
只有具体修复重验受影响项，不自动扩样。覆盖raw父候选对照、思考
开/关、流式/非流式工具及工具结果回填、MTP和普通decode、length。
记录质量、usage、finish、原始时间与清理，不作性能非劣声明。
验收后保留回退包，同步默认程序及元数据；正常恢复原LAN服务配置。

2026-11-02 02:49–02:50 CST初查：无vLLM引擎/容器，58001代理的58000
后端未监听；q4t唯一GPU进程，45秒10次观测均0%利用率、0聊天请求。
这不是持续空闲保证，测试前重新检查并全程持锁监控。

## 候选代码接口（尚未部署）

`messages`默认解析；顶层`parse_response=false`保留原始生成文本。
`prompt`始终原样输出，`prompt + parse_response=true`拒绝，因为任意
prompt的assistant预填状态不能可靠推断。模板与模型采样不改变。

普通回答在`message.content`，思考在`message.reasoning_content`；
流式使用对应`delta`字段。模板已经预填`<think>`时，生成内容无需再有
开始标签；只有自然stop且思考未闭合时返回解析错误，length保留部分思考。
标签之外的空白不擅自删除。`q4t_ttft_seconds`计至首个正文、推理或
完整工具delta发出，不能与旧的首个原始文本字节定义混作性能对比。

工具响应为标准`tool_calls`数组，`function.arguments`是JSON字符串。
`id`由request_id与调用序号组成；SSE另有从0连续递增的`index`。
流式每个完整工具块只发送一次，不在参数尚未完整时发布调用。只有
本轮声明的函数会被识别；未声明tools时工具标签留作普通正文；思考
区域里的工具标签不触发调用。正常stop有调用时转换为tool_calls，
length不会被伪装为正常工具结束。客户端应等成功终态再执行工具。

XML参数使用原生`<function=…><parameter=…>`格式。已声明string
保留字符串及引号/空格，只去掉模板包裹的一对换行；integer按十进制
原字面判断，避免大整数及近整数被double舍入；number/boolean/null/
object/array和直接type数组按声明类型处理。JSON大整数保留原字面，
重复JSON键、重复参数、未知函数、畸形/超预算工具块拒绝。
这是参数解码及顶层类型检查，不校验全部required/enum/$ref/嵌套schema，
也不是约束解码。`strict:true`拒绝，不能据此宣称schema强保证。

仅接受`tool_choice="auto"`和`parallel_tool_calls=true`，且要求
messages解析模式。none/required/指定函数/parallel=false仍返回400。
不执行工具；客户端将返回的assistant message及tool结果、tool_call_id
一起放进下一轮完整messages，不再把思考/XML文本重复塞进content。

解析/解码失败：非流式HTTP502，流式error事件加单个[DONE]；错误码
response_parse_failed，不发送正常finish/usage，不发布成功私有缓存。
工具调用最多128个，单块1MiB。服务原有取消/超时错误语义保持。

## 待执行入口

CPU夹具已接入共享host清单与发布精确清单；未运行测试前不得将
编译成功记作合同通过。`run_chat_template.py`显式使用原始输出，
保持冻结模板与raw prompt差分门的原意，输出协议另测。

真实HTTP驱动：`tools/response_protocol/verify.py`。仅在业务空闲并
清理经授权的空闲服务后，完整持锁执行。例如（路径必须绝对化）：

```bash
response_stage=/home/rm01/models/dev/qwen4-thor/.q4t-work/response-protocol-http-01
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output "$response_stage/lease" -- \
  /usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/response_protocol/verify.py \
  --baseline /ABS/PREVIOUS/q4t --candidate /ABS/CANDIDATE/q4t \
  --model-dir /home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --output "$response_stage/http"
```

驱动计划21次生成、10次400负例、3模型启动；全部原始请求/响应、
usage/finish、原始总时延及退出记录保存。首次具体失败即停止，不重试
找有利结果；驱动与外层guard必须同时成功。业务占用时不得运行此命令。

后续[工具历史关联开发](CHAT_TOOL_HISTORY_2026-11-02.md)在原24生成
上限内增加顺序/乱序两次正例，当前驱动计划23生成；10负例、3启动、
30分钟上限保持。原21是396a401阶段计划，不代表曾执行过。

## 结果

03:12 CST设备复查发现实际vLLM业务：容器
`soar-flash-restore-20261102`，EngineCore PID724780，running=1，
成功请求8，generation_tokens=5762，GPU97%。按用户条件切换为
**只开发、不测试**；CPU/GPU/HTTP测试均暂缓，未停止业务或恢复q4t。
候选程序已链接为354b20ae，Thor host和公共host程序均编译完成、
最终构建零警告；11项新增host合同及真实HTTP驱动均未执行。
首次候选链接缺少CMake的startup_reservation对象，补编该对象后链接
成功，失败日志保留；不是运行时测试失败，也没有运行候选程序。
尚未验收或替换默认0c5ab1de/e6fdd13b，完整旧程序/元数据/原构建缓存
保存在`.q4t-work/response-parsing-20261102/previous/`。
原始观测：`.q4t-work/response-parsing-20261102/vllm-active.json`。
构建、身份、观测及源码清单见
[开发快照证据](evidence/response-parsing-20261102/README.md)。
