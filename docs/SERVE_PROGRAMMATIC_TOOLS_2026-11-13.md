# 原生程序调用实施与验收（A07/A11）

目标是完整执行programmatic_tool_calling，延迟发现交付后继续推进；
不能用接收字段、返回unsupported或声明限制代替实际程序执行。
本组仍属于Serve总计划，结束后继续typed outputs、MCP及其他任务。

## 固定协议和必要依赖

按已固定OpenAI Node SDK7.47.0的program/program_output/caller形状与
[官方程序调用说明](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)
实施。程序发出的客户端工具仍由客户端执行；回填时保留caller和call_id。
程序只能访问请求已授权且已经加载的工具，不能从运行中的程序执行tool_search。

项目尚无JavaScript执行器。嵌入[QuickJS](https://bellard.org/quickjs/)
2026-06-04核心，采用MIT许可，原包SHA256
b376e839b322978313d929fd20663b11ba58b75df5a46c126dd19ea2fa70ad2a。
17个未改上游核心/许可文件存入third_party/quickjs，逐文件身份固定。
不链接quickjs-libc、动态模块加载器或外部qjs/Node程序。普通LLM不
实例化VM；单个程序失能只失败该请求，不能触发模型重启或失去readiness。
这是实现目标所需的嵌入库，不是新增前置服务。发行需携带MIT声明。

## 实现顺序与完整出口

1. 原生隔离执行部件。复用exec-only worker及100ms有界回收，新增独立
   固定容量domain；未确认回收保留槽，不能增殖。VM只暴露tools、text、
   image和语言内建；禁止网络/文件/进程/可执行内存，设置堆、栈、CPU、
   墙钟、输入、代码、输出、子调用与微任务上限。错误/取消不拖普通请求。
2. 授权与可续接身份。工具定义保存原namespace、caller、参数/结果合同；
   direct/programmatic允许集合决定实际派发。程序、子调用、结果关联及
   调用顺序必须校验。仅已有结果在隔离VM内回放，绝不重新执行外部调用。
   随机/时间等可影响控制流的输入固定并入指纹；跨客户、篡改与过期拒绝。
3. 请求/模板/执行器。模型生成真实JavaScript；立即运行到完成或等待客户端
   结果，结束worker并输出标准pending调用，不能占着模型等待客户端。
   收到结果后恢复程序直到下一次暂停或program_output，再续生成真实回答。
   支持函数/custom、namespace、异步及发现后加载；其他内建工具后续按
   A11实际执行器接入，保留必做，不缩小总范围。
4. 状态与流式。JSON/SSE只发标准items/events；同Response共享生成、工具、
   期限、取消、TPM预算。previous_response_id、Conversations、background、
   重启、无状态完整历史、截断和压缩均保存可核验的调用时身份和已完成结果。
   截断程序不可执行；异常不能将部分工具结果伪装为成功。
5. 发布。声明、SDK、许可、打包来源身份及主轻/恢复资格同步；真实资料业务
   通过后可回退交付、实际合main，再继续全部master。

## 验证规则

先成组开发，再冻结新增及直接影响的CPU清单；重点为真实语言执行、并行/
依赖调用、结果回放、身份/权限负控、未完成调用、异常/超时/内存/清理与
普通请求独立性。通过CPU部件不宣称公开接口或实际模型已可用。

真实业务准备好后冻结有界请求清单，使用严格GPU租约；覆盖程序读取本项目
实际资料、结果聚合及最后答案，分别验证JSON/SSE、存储续接及错误路径。
不运行完整性能或全模型质量矩阵，不将一次阶段通过标为总目标完成。

当前：上游核心与许可已固定，隔离执行部件13项CPU首过，源码审计通过；
[证据](evidence/serve-programmatic-worker-cpu-20261113/README.md)保留。
公开接线、授权/持久续接、业务验证和发行尚未完成。实际默认rev118保持，
PR26已实际合main（090f0d48），继续程序调用和全部master任务。

认证/回放部件19项不同CPU最终通过，见[证据](evidence/serve-programmatic-authority-cpu-20261113/README.md)。
指纹保存调用时权限及schema，绑定客户/模型/模板/原代码；续接有效期
30天，与默认对象保留期一致，密钥撤销也使待续接状态失效。VM记录
每次子调用发生前的结果数量，伪造依赖调用时序会被拒绝。公开接线、
队列/压缩、媒体语义和发行仍待完成，不能把当前部件称为完整能力。


公开接入现已完成本组针对性 CPU 验证：54项不同C++、2项官方SDK、
7项Python最终通过，[原件与失败修正](evidence/serve-programmatic-integration-cpu-20261113/README.md)保留。
完成生成调度、caller/原始合同、JSON/SSE、持久/无状态/后台快照、
裁剪/压缩、发现后授权、串行与异步、媒体准备/审核/冻结与共同预算。
程序图片在标准string结果中使用有版本的内容编码，纯文本不误解析。
公开程序只有已加载function/custom工具；其他内建执行适配仍在A11。

真实短验固定1启动/8创建/900秒：函数JSON与custom SSE实际读取
本项目docs/MODEL.md，验证previous_response_id及Conversation回填后
程序继续并生成真实标题/引用；错误结果schema拒绝、本地程序续答、
截断不派发和普通业务继续。未执行前不算通过；随后适用资格/发行/
main合入继续，媒体GPU组合在A10/R10仍是必做，总目标保持active。


真实资料闭环补验通过，见[记录](evidence/serve-programmatic-business-20261113/README.md)。
首次长度语法失败与11项CPU修复独立保留；默认rev118仍未替换。
交付资格固定：主轻2启动8业务/900秒，旧控制当前引擎独立1启动2业务/
300秒，程序等待实际结果时整树恢复2启动4业务1探测/600秒，总上限
5启动14业务1探测1800秒。每项全程持严格GPU租约，后项依赖前项的
精确资格；不重放外部工具，不跑全量性能/质量。随后停止态可回退发行、
PR27实际合main及全部master持续。
