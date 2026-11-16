# Chat 样式首轮业务失败与原因

2026-11-16，1启动/4Chat/45.196秒，默认medium JSON、max/low SSE关闭
混淆、none双choice带混淆及概率/usage通过。第4条工具请求产生第二次
调用，被解析器报structured_output_invalid；没有成功终态，没有执行
工具。原请求/响应/日志完整保留，不能计该候选验收通过。

根因是新none选项已关闭模板thinking前缀，但output_constraints仍只
读取enable_thinking，语法worker误停留在自由思考区域。不是padding
破坏参数；须统一模板和语法的选项解析，none从首token启用约束。

NVML全程正常，自有模型正常退出、端口/进程/租约清理，默认rev150
未改。下一步真实grammar CPU负控、受影响模板/Responses检查，修复
后再冻结原六条业务；不放宽单调用合同，不追加性能或长稳测试。
