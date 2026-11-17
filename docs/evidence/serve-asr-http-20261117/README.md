# 原生ASR真实HTTP与SDK分项接受

2026-11-17，接受基础转录直接HTTP及辅助故障隔离，不替代剩余音频
能力或服务包验收，不改默认rev266。程序SHA：
`9401541172832c9f340be1dc77907823bcd2ddf6b76a142b82e95bd216f31976`。

保留service-http-04原FAILED，按原始wire及固定SDK数值合同重新
核对前4次实际创建；service-http-05仅补剩余5次并通过，45.963秒。
两次模型启动共93.585秒的9项业务成立；含此前传输/scope失败累计
4启动/11创建/170.949秒。原错误记录从不改写为整链PASS。

- 实际缺权重503、5秒冷却拒绝，核心readiness和同PID Chat回答42；
  恢复私有权重入口后同PID Python SDK转录成功，无模型自动重启。
- 中英文与参考文本完全相同，英文WER=0；指定中文及literal提示
  返回正确原文。实际token logprobs/音频和文本usage均存在。
- JS SDK实际SSE增量拼接与done一致，逐片/最终概率一致，使用转录
  事件类型；没有Chat的[DONE]。错误stream值400，未进入推理。
- 在第一段文字后主动断开，真实aborted计数恰增1，资源槽回收后
  同PID核心Chat正常；最终ASR ready、compute_failures=0。

严格NVML租约结束0，模型退出0、端口/子进程清理通过；启动分配全
释放，无unknown/backend故障。server_destroyed仍保留进程缓存计量，
不宣称退出前瞬时quota为0。发布包五项身份保持，原计算logits门限
FAILED仍保留，不宣称完整模型质量或性能接受。

下一步原生TLS/独立客户端文件配额、主轻profile及版本恢复、服务包
和可回退交付。实际对齐/字幕/说话人/VAD、Chat音频、语音输出及全部
master继续；当前分项接受不关闭A10或总目标。
