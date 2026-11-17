# 实际词时间戳/字幕及辅助故障隔离

[原始结果](result.json)：1次模型启动、7条创建、57.044秒。
[严格GPU租约](lease/exit.json)完成，55次NVML采集均正常；
[自有清理](cleanup.json)模型正常退出、无剩余进程、端口已释放。
默认rev276及其发布身份未变。固定来源见[计划](plan.json)。

缺失私有对齐权重得到分类错误，独立health降级；同PID普通文字
转录及Chat 42继续。恢复只读权重链接后，请求驱动重新加载通过，
没有重启模型。最终ASR和alignment均ready、compute_failures=0。

Python SDK 2.8.1取得13个中文词/26个边界，逐项等于已保留实际
native与官方参考中文结果；词序/真实时长/usage和wire保持。
JS SDK英文SRT为6个实际字幕片段，Python SDK中文VTT为1个；
文字均与冻结录音转录完全相同（英文WER=0），所有时间为正且在
实际录音内。SDK和wire相同，之后核心Chat 42与清理仍通过。

这证明接口/SDK/边界接入和故障域恢复，不宣称人工标注的时间
精度、日/韩语言质量或完整segment合同。原英文组件2/72槽差异
仍保留在前序证据。完整segment统计/VAD、服务组合/可回退交付
及所有Serve总任务继续，本候选尚未成为默认。
