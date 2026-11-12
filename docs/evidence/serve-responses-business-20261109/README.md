# Responses同步生成与状态真实业务短验

2026-11-09（UTC原件2026-11-08）。104份普通文件、1266763字节，
[index.json](index.json)记录原始SHA256；[冻结计划](business-01/plan.json)。

实际1模型启动/7生成/58控制请求，54.739秒；严格GPU租约全程持有，
53次NVML均OK，guard COMPLETED/child 0。真实默认MTP配置，同PID完成：

- 无状态JSON与概率JSON均回答42；stateless查询404。
- 原始概率bytes与top2保留，落盘GET和原SSE重放终态一致。
- reasoning输出34个推理token/38个输出token，总usage94。
- SSE function_call读取docs/MODEL.md实际前3行，回填previous_response_id，
  模型输出实际标题和[docs/MODEL.md:L1-L3]引用的约束JSON。
- 输入历史含实际工具调用/回填；删除回执与随后404一致。
- 1token截断为incomplete，无arguments.done/completed；未执行截断工具。
- 同PID Chat/MTP继续回答42，7请求均成功，错误0。

[清理](business-01/cleanup.json)：模型退出0，0残留，端口释放；
[后核对](business-01/postflight.json)确认GPU锁可获取、默认与冻结源码未变。
引擎SHA256 `8a53f39d82b60680255256a58388a8b5e60fd37db5b588248f4833f8912f39b1`；可核验候选另保留在
`.q4t-work/serve-responses-20261108/foreground-candidate/`。

本组仅同步文本/状态/工具业务；CPU身份/TLS证据在前序目录，不把
本机匿名HTTP冒充完整生产多客户端业务。无默认替换、长稳或性能主张；
A07/A08剩余后台/实时事件/恢复/执行器及其他总任务继续，尚未完成。
