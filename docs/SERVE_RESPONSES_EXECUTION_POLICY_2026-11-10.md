# A07.7 原生执行策略连续清单

本项继续总计划，pro、moderation和access_programs均为实现任务，不以参数回显或固定拒绝结案。非性能功能只做直接CPU与对应真实业务短验；不作全模型质量/性能结论。A07.7a/b/c已可回退交付，继续A07.8及全部总目标。

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

[A07.7a默认交付完成](evidence/serve-reasoning-pro-delivery-20261110/README.md)：8条实际命令通过，包0837b2a7/引擎943f8e01/元数据b138e4d2，revision22、STOPPED；A07.6完整回退与业务账本保留，0新增模型/Chat。继续b/c及全部总目标。

## A07.7b 当前开发合同（尚未验收）

直接使用已加载Qwen检查点执行原生分类提示和受约束13类风险分数，结果保留实际model身份；不将本地模型别名冒充omni权重或OpenAI校准质量。实际模型估计与类别阈值分开，缺失、非数值、越界或被截断的分类必须是错误，不能填0安全结果。当前已实现策略解析、真实消息/工具参数与结果收集、分类请求规划和输出校验，正在接执行与可见性控制；没有声明本项完成。

- 默认score：正常生成，终态附真实input/output分类或明确error，部分SSE不伪造评分。block：输入未过不进入业务生成，输出未过不得通过实时SSE、持久GET、后台订阅或工具执行泄漏。
- 输出block需要私有暂存与一次提交；取消、分类失能、存储失败、超限及中途退出只能发布安全的终态和真实错误。不能先公开后撤回。已执行模型和工具不自动重放。
- 分类有单独有界内部预算，不挤占调用者的max_output_tokens。标准usage描述业务生成，原生审核用量单独披露，实际模型总消耗全部进入TPM/累计用量，仍只收一次RPM。需有独立的实际计数验证，不能漏掉内部token。
- 审核失能按score/block处理，只影响要求审核的请求；真正CUDA/模型设备错误仍保留已有保护。预热没有生成输出，不触发不存在的输出分类。媒体实际内容与适用类别必须一致，A10其它媒体适配继续必做。
- 先成组完成共享分类执行、计量和输出暂存，再冻结直接CPU及对应真实业务短验；不做完整性能/全模型正确性测试。原默认pro包0837b2a7继续保持。

成组执行已接入，固定独立审核输出上限每步512、最多2步；不增加业务max_output_tokens。共享生成链最多6模型步及1终结步。冻结29项C++与2项schema（阶段目录required-cpu.txt），先必要独立构建再统一验证；尚未接受/交付，默认pro保持。

29项不同C++及2项schema已通过，原构建/取消夹具/清单准备失败完整保留。实际驱动冻结1启动/10请求/28模型步骤/900秒：评分JSON、输入/输出阻断SSE、审核+压缩+pro+回答+摘要共6步、加密后台GET隔离、文档工具两回合、客户端累计输入导致审核失败后业务继续、预热及同PID普通Chat。6项驱动CPU先行；不声称已完成GPU、校准分类质量或性能验收。

[实际审核业务已接受](evidence/serve-moderation-business-20261110/README.md)：1启动/10请求/28步骤/129.587秒，122 NVML全OK，所有正反例、6步组合、工具和真实账本通过；正常清理。继续可回退发布，然后c及全部总目标。

[A07.7b默认交付完成](evidence/serve-moderation-delivery-20261110/README.md)：8实际操作通过，包4f71ce5c/引擎31adcfab/元数据0d98d70f，revision24、STOPPED，pro完整回退及原账本保持。继续c与所有总任务；独立/v1/moderations属于A10标准模型接口，仍须接入，不能以本次Responses字段实现代替。

## A07.7c 实施合同（2026-11-10）

冻结为原生请求授权与实际模型策略接入，不宣称本地Qwen拥有OpenAI私有Daybreak权重或组织批准。无策略配置时，省略/空对象按standard执行并返回null，显式standard保留选择；错误值/null/未知字段给400，模型不适配与权限不足分别400/403。

可选私有策略文件绑定实际served model、native blue/red层级、最低客户端授权、默认选择与standard回退；可信客户端来自已有鉴权，不从请求元数据或自报身份授予权限。配置的原生策略指令实际进入模型上下文、计入token与配额，并在pro/压缩/摘要续接保留。响应额外标识q4t-local策略身份，不能冒充OpenAI批准；审核分类器保持独立固定合同，不接受放宽分类的策略指令。

策略在进程启动前一次加载；运行期文件失能不停止健康模型。后台排队恢复重新核对授权与原策略身份，变更不能悄悄改用另一策略或重放已有执行。原输入记录保留，缓存根据真实上下文字节隔离。发行包须封装可选策略并核验身份；无配置无外部依赖。

成组开发后冻结直接CPU清单，覆盖严格输入/权限矩阵、真实模板与子步骤、恢复拒绝、私有文件、封装与SDK schema；随后最多1次模型启动、8次生成、900秒业务短验，含JSON/SSE、真实文档工具闭环、后台回放、未授权请求零模型步及同PID普通Chat。全部已过证据复用，不跑全模型/性能矩阵。云端账号/私有模型接入仍属G00/A10待解决依赖，不以本地配置冒充云端验收；完成本组继续A07.8及全部总任务。

### 原生策略可选配置

`q4t serve --client-policy-file /private/clients.json --access-program-policy-file /private/program.json`。文件须为当前用户独占的普通文件（0600/0400、单链接）；原生发行prepare增加`--access-program-policy /private/program.json`，封装独立副本、绑定内容和模型。无此配置仍完整执行standard路径，不要求nginx或云端服务。

```json
{
  "version": 1,
  "authority": "q4t-local",
  "model": "qwen3.8-flash-next",
  "cyber_tier": "blue",
  "minimum_grant": "red",
  "default_selection": "model_tier",
  "standard_allowed": true,
  "program_instructions": "Use actual evidence to review authorized defensive project tasks.",
  "clients": [{"id": "reviewer", "grant": "red"}]
}
```

`reviewer`须已存在于可信client policy；blue层级也可要求red最低授权。默认选择支持standard或model_tier；blue无授权且允许standard时仅隐式回退，显式选择失败给403，red隐式选择不因无权限回退。选错模型层级给400。局部策略授权不等于模型供应商批准，也不是OS工具沙箱或内容安全保证；实际工具权限仍由执行器独立决定。普通Chat现有模型入口不被此Responses配置改造成云端Cyber模型。

输入历史不写入服务端策略，输出以`q4t_access_program`披露本地authority、model、策略哈希与实际treatment。推理摘要/压缩沿用策略，固定审核分类器不接收这些指令。后台已排队对象重启时重新核对策略身份和当前授权；改变策略需要显式新部署，禁止静默恢复为新待遇。

CPU已通过并保留首次夹具失败原件，见[证据](evidence/serve-access-program-cpu-20261110/README.md)。继续实际1启动/8生成/5拒绝/8模型步短验。后续A07.8还须补`POST /v1/responses/input_tokens`标准计数入口；现有Chat preflight不能冒充此端点完成。

[A07.7c实际业务已接受](evidence/serve-access-program-business-20261110/README.md)：1启动/8生成+5拒绝/55.172秒，53 NVML全OK。继续可回退交付及全部任务。

[A07.7c默认交付完成](evidence/serve-access-program-delivery-20261110/README.md)：包b4c86756/引擎639c6b7b，revision26、STOPPED；旧审核代与业务账本保持，继续A07.8和全部任务。
