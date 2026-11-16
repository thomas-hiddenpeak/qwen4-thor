# 输入归属短验：原失败保留，四条Chat业务可复用

2026-11-16，候选7a23cb30实际1启动/5生成，55.731秒；严格租约与
NVML正常，自有模型/端口/锁清理通过，默认rev158未改。
四条Chat JSON/SSE、实际default tier/归属摘要、一次文档工具及实际
SHA续答通过。第5条Responses生成也为medium/42，但GET默认隐藏
logprobs，驱动遗漏include却要求整个对象相同，因此阶段失败。

源码既有ProjectStoredItem/ViewResponse合同与实际raw wire对照证实：
唯一差异是output_text.logprobs空数组被投影移除，核心请求5/5成功。
未修改原始结果、未将失败改成通过。修正GET显式请求
message.output_text.logprobs，保持完整快照相等判定。

四条未变Chat证据经来源/二进制/日志/工具结果核对复用。冻结补验
最多1启动/2生成/600秒，只做Responses JSON、null SSE及对应GET/
DELETE。运行时/数值不变，不重跑Chat、不扩大性能或完整质量。
全部master继续；新完整业务接受与可回退发行仍待完成。
