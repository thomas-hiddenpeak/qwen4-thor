# VAD实际转录运行时CPU合同（2026-11-17）

16/16针对性C++用例一次通过，0.007762秒；3项验收器正负控通过，
0.214010秒。0GPU。host与独立CUDA构建通过、零警告；候选
`f6f3370f481431cf2d0d32bc1f677ab800653edd6a787d5efd3c916b1f3f7c71`
保存在`.q4t-work/serve-audio-aligner-20261117/engine-03/q4t`，未发布。

覆盖真实片段偏移/word/segment编号及实际统计、字幕cue/唯一VTT头、
JSON/SSE终态/文本/实际用量、全静音空结果、失败后禁止发布先前
部分成功、边界/CLI/能力、短尾真实上下文与原协议保护。
固定VAD数值与资产14项旧证据按未变组件复用；尾段切法受本组新验。
未以CPU通过替代真实HTTP、TLS/服务/恢复或完整交付。

下一组固定1启动/12创建/600秒（含清理），独占GPU验证实际多段
音频及双SDK、故障隔离/同PID恢复、静音/阈值、段数/共享输出预算、
取消与核心Chat。初始输出预算24、最大3段；私有链接故障，不修改
原始模型资产或默认rev276。必要源/资产/SDK身份在prepare中冻结。
全部master剩余任务继续，长稳不在本组执行。

[冻结CPU清单](plan.json)、[逐项结果](cpu.log)、[结果](result.json)、
[源码身份](identity.json)、[构建记录](build-receipt.json)、
[验收器正负控](driver-cpu.log)、[候选身份](candidate.json)。
完整构建日志留原工作目录，不为保存文档重跑验证。
