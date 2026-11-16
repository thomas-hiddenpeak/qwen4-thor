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

## 当前交付与证据（2026-11-15）

已可回退交付rev126（d7dd0c6d），[17项停止态发布检查](evidence/serve-programmatic-delivery-20261115/README.md)
通过；默认STOPPED。PR27待最终检查与实际合main，总目标继续。

- [隔离执行部件13项CPU](evidence/serve-programmatic-worker-cpu-20261113/README.md)、
  [认证/回放19项CPU](evidence/serve-programmatic-authority-cpu-20261113/README.md)及
  [公开接入54项不同C++、2项SDK、7项Python](evidence/serve-programmatic-integration-cpu-20261113/README.md)
  最终通过，初始失败与修正保留。程序身份绑定客户/模型/模板/原代码，
  续接有效期30天，撤销密钥也使待续接身份失效。
- [真实资料JSON/SSE闭环](evidence/serve-programmatic-business-20261113/README.md)
  覆盖函数/custom namespace、实际文档结果、纯计算、截断及普通业务。
  大字符串约束问题经[11项CPU修复](evidence/serve-programmatic-length-repair-20261113/README.md)，
  保持原min/max、Unicode/转义及资源合同。
- 实际受控恢复暴露[首次模型入场前配额失败](evidence/serve-programmatic-resume-failure-20261115/README.md)。
  [26项CPU修复](evidence/serve-programmatic-resume-rate-cpu-20261115/README.md)重新预留未入场提示，
  保持RPM、共享输出预算、已发生用量与Batch额度；随后
  [新精确引擎资格及整树业务](evidence/serve-programmatic-qualification-20261115/README.md)
  5启动14业务1探测通过。原程序在切换后收取一次实际资料读取结果，
  VM恢复并由模型完成最终SSE答案。含原资格尝试累计10启动28业务
  2探测；所有资源已清理，不把只恢复进程当业务成功。
- [两种语法构建合同](evidence/serve-programmatic-ci-grammar-20261115/README.md)
  针对性通过。生产启用XGrammar；未链接它的公开CPU构建明确报错，
  不能错误派发受grammar约束的工具。

程序图片在标准string结果中使用有版本的内容编码，纯文本不误解析。
已加载function/custom工具可执行；MCP及其他内建执行适配、媒体GPU
与恢复组合等仍是A10/A11/R10必做工作，不能用本阶段替代总计划。
独立回退包使用当前引擎和旧控制实现；自动切换保留现代固定根，
人工整根回退到v12没有outer功能，不能将其称为独立旧引擎回退。

2026-11-15：[PR27已实际合main](evidence/serve-pr27-merged-20261115/README.md)，
精确head两组CI全过。后续MCP及全部master继续。
