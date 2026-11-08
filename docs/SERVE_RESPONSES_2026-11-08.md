# A07 Responses 原生实现与 A08 状态衔接

2026-11-08建立、2026-11-09更新；功能开发，不作性能优化结论。
同步首组已通过真实业务短验，完整A07/A08仍在实施。
2026-11-09声明对照发现并修复三个原线包遗漏：tool_choice、
cache_write_tokens、投影SSE的必需logprobs空数组。原同步/后台记录
保留为这些缺陷的负控，不追认完整线协议通过；详见文末新检查点。
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


## 后台生命周期与恢复核心

2026-11-09复核[官方后台模式](https://developers.openai.com/api/docs/guides/background)：
后台store=false仍需临时保存供查询，取消幂等，断开订阅不取消生成。
修正原来的直接拒绝，临时响应与未关联会话的新item保留600秒；
会话items保持原保留合同，不缩短已存在item的生命周期。
queued初始记录/原事件和转in_progress分别持久确认，未知转移不执行。
恢复queued保留原ID、输入items和序号；已in_progress的中断记录真实
failed恢复事件，不能盲重放可能执行过的工具。会话revision变化时
恢复执行被拒绝，仍可以将该响应关闭为失败。模型重启恢复策略由R组
负责，此处只闭合持久业务状态，不自行重启模型。
官方cancel响应status=cancelled/error=null；固定SDK的SSE联合没有
response.cancelled，取消只保存资源和已有原事件，不伪造completed或
新的事件类型。实际后台任务管理器和HTTP取消仍待接入，继续推进。
核心子组8新增/3直接CPU通过、实际引擎构建零警告，
[证据](evidence/serve-responses-background-core-20261109/README.md)保留。
无GPU或默认变更，继续实际后台管理器/路由。


## 后台服务接入子组（继续实施）

管理器以固定工作线程执行真正共享生成回调。任务数量、每个客户端
的任务数量、保留输入/规划的序列化字节分别有上限；字节预算不冒充
堆分配开销或总DRAM测量，模型执行仍走现有内存准入。客户端permit
从HTTP请求连续移交给服务任务，不出现额度空档或双重计费；队列
按客户端轮转。正常完成与取消由同一个单调控制器决定，完成提交
先赢时取消返回既有结果，取消先赢时不得再写completed。

未知存储回执不释放所有权后重跑。原执行者已退出才按ID及新revision
核实：已落盘终态保留，缺终态记录明确失败；恢复尝试有间隔及每组三次
后60秒退避，状态故障不重启模型。此机制和整个进程重启后的扫描不同，
后者仍必须接入，不能用本模块核实代替重启恢复。

余下接入工作按顺序继续：

1. 原生create识别background并移交任务，JSON返回持久queued；SSE订阅
   已有事件，断线只影响订阅；任务使用无socket上下文直接调用共享生成。
2. 原生cancel路由与官方SDK空请求体，幂等返回真实终态。状态查询与
   模型执行的额度分离，取消保留独立容量；后端与原生TLS入口一起修改。
3. 实际进程启动后有界扫描已授权client持久状态；未执行queued续排，
   in_progress中断记录失败；当前所有者检查、旧身份/策略、过期与停止
   意图必须纳入，禁止重放已经执行的工具。
4. 配置、健康、能力声明和发行元数据同步；异常只降低相应持久/后台
   能力，不能把健康模型readiness拉低。
5. 相关CPU、真实SDK和原生入口验证后冻结有界GPU业务清单。通过再做
   可回退发行；A07/A08的其余执行器和总计划继续，不按此子组停止。

管理器冻结10新增/3直接CPU，已有全部通过；构建中optional类型处理
错误与首次政策夹具遗漏摘要均留存，修复只重验受影响项。新增序列化
预算补入规范化items/messages与约束模板，另做额度项定向复核。

## 实际后台HTTP与独立取消通道检查点

已接入共享HandleChatImpl和无socket执行上下文；任务持有准入、取消
控制与持久终态，创建请求只持有订阅。原生cancel支持SDK空POST；
查询/SSE续读走control、取消走独立单客户端1名额，生成不占用这两类
额度。入口对应保留独立取消速率与并发，业务停机时模型仍遵循原保护。

store=false活动临时保留至持久执行期限加10分钟，完成/取消后10分钟；
后台stream要求创建时stream=true。取消不伪造response.cancelled事件。
新增健康字段明确后台任务状态不是核心readiness依赖。

[14不同C++及4原生入口/TLS](evidence/serve-responses-background-http-20261109/README.md)
已通过，TLS首次夹具参数缺失与修正保留。完整引擎构建零警告；还需
SDK/跨进程扫描与真实模型后台验收，正在继续。当前默认A06不改。

## 实际跨进程恢复及标准SDK

存储对象保存scope、boot/PID/进程出生时间/PID namespace和原执行
deadline；专用扫描只取有界元数据，不发送输入输出或绕过客户端归属。
复用Guardian既有进程识别原语，新增依赖仅本项目已编译代码与Linux
/proc，不新增外部守护程序。无状态Responses不调用此状态识别。

只确认旧进程已退出后，queued原ID/历史/原截止期续排，重新获取当前
客户端准入；已开始任务保存真实部分输出与中断失败，不重放工具。
旧客户端撤销或期限已过明确失败；活着/无法确认或不同PID namespace
不接管并显示pending。对象worker退出本身不能证明模型退出。

scanner启动失败每60秒再尝试；已运行扫描器异常可退避再扫，存储
错误三次后60秒退避。队列满保留磁盘queued并再尝试，不自动重启模型。
[13不同C++及4官方SDK/5 CPU进程](evidence/serve-responses-recovery-20261109/README.md)
通过。一次重复控制器夹具误用已修正，只重验失败/受影响项；真实进程
SIGKILL验证queued完成、started明确failed且执行次数各1。

下一组继续有界后台模型业务与发行/能力合同；前台未知提交同进程
收尾、对象控制器异常恢复、持久保留治理及Chat存储/其余Responses
执行选项仍需完成，不以本子组通过关闭A07/A08或总目标。

## 2026-11-09 声明对照与实际协议修复

能力发现、严格配置schema和26条后端方法/路由已对齐原生Responses/
Conversations。发布声明v3包含应用schema及所有引用内置的OpenAPI；
冻结SDK7.47.0的1,150节点，原运行时656节点图字节保持不变，无新增
运行时Node/SDK/网络服务依赖。结构形状包含尚待真实执行器的标准
能力；这些能力继续必做，不能以schema存在或临时503认定实现。

对照真实历史线包发现并修复：有效tool_choice没有写入响应，成功
usage遗漏cache_write_tokens，续读投影删除了text delta/done必需的
logprobs字段。现在保留Responses形状的实际工具选择、缓存写入计数
及边界；当前未启用Responses缓存写入时为0。概率未include时事件
保留空数组，原存储概率不变。后续缓存接入仍需提供实际计数。

固定TypeScript把usage声明为可选对象，但官方[创建事件](https://developers.openai.com/api/reference/resources/responses/methods/create)
和[取消响应](https://developers.openai.com/api/reference/resources/responses/methods/cancel)
示例允许usage:null；schema按实际HTTP合同保留活动/取消的空计量，
completed/incomplete仍要求真实usage，不能伪造0。

[本组证据](evidence/serve-responses-declarations-20261109/README.md)：10不同
C++、10不同Python及4官方SDK场景通过；流式修复后只重验受影响SDK
场景。新51对象/事件严格通过，23旧会话对象通过；旧同步50条中的
14条及旧后台51条中的44条保留为上述遗漏的失败证据，不改历史。
夹具悬空引用和schema/SDK空值差异的首次失败也保留；3次构建零警告。
无GPU、默认不变，接着完成后台真实业务与剩余总计划。

## 2026-11-09：前台未知提交有界协调

继续修复前台请求结束后，同PID仍活着导致跨进程扫描不敢收尾的问题。
首次持久Begin前先取得有界元数据槽（独立max_jobs/max_client_jobs，
固定scope/ID数组，不保留提示词、执行回调、HTTP fd或客户端permit）。
槽满先拒绝，不让未知提交发生后才发现无处保留所有权。

模型/grammar清理后才由非分配退出守卫交回槽，后台现有worker按
原ID读取实际revision：已成功提交的终态保持不变；未终态记录保留
已持久部分输出并明确failed，不再执行模型或工具。最多三次快速
尝试后退避60秒；存储仍失能就保留有界槽。活执行绝不接管；独立
store=false不依赖协调器，真正进程退出仍走既有跨进程恢复。

同一调度器交替处理前台协调与后台工作，避免持续一方任务压住另
一方。config新增responses_reconciliation，能力声明加入生命周期
所有权。冻结7新增及7直接CPU验证；后台真实候选保留，默认A06不变。
后续补适用真实业务短验与发行，继续全部高级执行器及总计划。
