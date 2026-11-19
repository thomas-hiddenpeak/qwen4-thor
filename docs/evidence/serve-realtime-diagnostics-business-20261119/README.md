# Realtime执行诊断真实短验（2026-11-19）

实际分类与计数通过：成功转录为success/execution_complete（122token），
第二轮RPM拒绝为error/client_rpm_exhausted，实际delta后断连为
aborted/client_disconnected（257实际token结算）。同PID Chat返回42；
最终4请求=2成功/1错误/1中止，预留、连接、会话RAM和序列位均回收。
不将执行完成等同于socket终态发送成功；协议所有者仍独立处理取消。

1模型/1入口启动、2会话3提交/1Chat，41.625772秒，41次NVML正常；
单严格GPU租约，官方JS WSS+原生TLS/APIKey+原生ASR。自有进程退出、
三端口可重新绑定、GPU锁可重获并释放；无模型重启，默认rev326保持。
启动1606份分配释放；server_destroyed还有6份进程级CUDA分配，进程
随后退出且guard确认无遗留上下文，不声称该析构检查点已零分配。

引擎SHA256：`c5d0f8a4c884cbc019f74cc9a4a257a3f29ee656be71563ccf1278a61e815a2f`。
[5项CPU证据](../serve-realtime-diagnostics-cpu-20261119/README.md)及
[降噪数值/真实场景](../serve-realtime-noise-business-20261119/README.md)
复用。此次仅补观察接线与直接受影响业务，不重跑全模型质量/性能。
固定PID准入夹具不代表Guardian恢复；剩余实时处理器、对话/Live、
恢复组合与可回退发行仍需完成，整个master保持active。
