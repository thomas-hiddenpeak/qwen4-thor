# 原生延迟工具发现与持续调用

属于A07/A11，接续已发行rev110的异步工具。全部master持续执行；
本文件划分实现与验证步骤，不把尚未接线的部件称为功能完成。

## 固定合同

按本机官方SDK 7.47.0和2026-11-13读取的
[官方工具搜索文档](https://developers.openai.com/api/docs/guides/tools-tool-search)
实施。server调用产生call/output两项、call_id为null；client调用
返回明确call_id，应用提交对应结果。已加载定义可跨轮保留，新的
定义在搜索结果或developer additional_tools所在位置进入上下文。
namespace内允许同时存在立即可用及延迟工具，最初只显示搜索摘要。

## 实现清单

1. 请求私有目录与标准合同。原生解析server/client工具搜索配置、
   函数/custom/namespace的延迟属性，复用既有schema及名称校验。
   目录、参数、搜索次数、结果和累积已加载定义均设资源上限；
   资源不足明确失败，不能静默截断或加载未授权定义。
2. server发现与实际后续调用。模型看到名称/说明及namespace摘要，
   经paths选择本请求目录中的工具或namespace，产生真实call/output；
   更新可调用集合并继续生成，所有步骤共享取消、期限和输出预算。
   初期采用确定性目录路径解析，不增加外部搜索服务或embedding依赖。
3. client结果及additional_tools。校验调用/结果关联、单次消费、
   原schema与租户归属；允许客户端返回原目录之外的合法新定义。
   additional_tools按developer输入合同处理并保留原位置。动态
   定义进入严格grammar/实际派发集合，不能只作为给模型看的文字。
4. 模板、状态及组合。已加载定义只在历史对应位置出现，不能塞回
   顶部破坏之前的上下文；持久previous_response_id/Conversation、
   排队快照、重启、压缩和裁剪保留调用时定义及原结果约束。工具
   选择、同步/异步、namespace与file_search混合行为一并接通。
5. JSON/SSE及SDK。公开item形状、状态、序号、失败/截断、usage和
   GET投影遵守固定SDK；不发明SDK不存在的增量事件。客户端工具
   不在服务器代执行，恢复也不得重放。声明能力必须晚于实际接线。
6. 针对性验收、可回退发行与主线合入。完成后继续程序调用及其他
   master任务，不能把本组结束当作总计划结束。

程序调用的隔离JavaScript执行器及MCP等其他内建工具仍为必做项。
本组先复用当前函数/custom/namespace能力形成真实发现闭环，不能
因执行器尚未实现把其永久排除。引入执行库前须记录许可、必要性和
失效影响；普通LLM不依赖可选执行器的可用性。

## 验证冻结

先完成成组开发，再运行新增及直接受影响的CPU用例。包括目录初始
隐藏/混合/多namespace、实际加载前后授权、非法schema/预算、重复
或孤儿结果、历史顺序与缓存前缀、持久/后台/压缩及调用时结果合同，
JSON/SSE终态/usage/取消及现有异步/文件搜索回归。执行前冻结精确
required-list，失败后只补受影响项；不跑完整性能或模型质量矩阵。

必要真实业务初定1次模型启动、最多10次Responses创建、900秒：
server函数JSON闭环2次、server custom SSE闭环2次、client搜索/
发现/真实资料结果3次、additional_tools闭环2次、错误结果拒绝1次。
在实际输出容量256下验收，查询只读本项目真实文档。整个阶段持
同一严格GPU租约，清理自有进程/端口/锁；CPU完成前不启动这组GPU。
主轻/恢复交付只重验受影响合同并复用未改证据，另冻具体清单。

当前状态：目录及有序历史部件已[通过10项CPU](evidence/serve-tool-discovery-catalog-cpu-20261113/README.md)，
继续公开执行器、模板/grammar与状态绑定接线；尚未宣称支持
tool_search或defer_loading。默认rev110保持，PR25已验异步
提交实际合main（4e87a03f）；后续新开发未混入该已检查的head。
