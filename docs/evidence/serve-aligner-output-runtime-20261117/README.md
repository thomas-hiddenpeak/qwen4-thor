# 原生对齐运行时与词时间戳/字幕输出

14项针对性CPU通过，0.007777秒、0GPU；两个隔离构建零警告。
[来源及二进制回执](receipt.json)、[精确用例](required.txt)、
[原始结果](run.log)可复核。候选引擎
`88dc8bf7ebc1d4142d05a224a43274c3eaa3bc9c2abcd7151a446fb15f1e72ab`
保存在`.q4t-work/serve-audio-aligner-20261117/engine-01/q4t`。

实际运行时已接入独立对齐加载、共享GPU/slot/取消/进度和清理；
同一请求只计一个RPM，额外对齐输入通过ExtendPrompt预留，并累计
实际计算usage。词级verbose JSON和SRT/VTT使用真实分类/显式修复
的边界，保留完整转录；空值、Unicode、超界和字幕注入有直接测试。
原ASR无对齐请求保持独立，只有真实设备错误影响共享核心健康。

CPU通过不证明实际HTTP可用。下一组固定真实中英文、双SDK、辅助
资产失能及同PID恢复，随后继续完整segment/VAD和服务交付。
标准segment所需tokens/avg_logprob/no_speech_prob仍须真实来源；
不能用固定值填充，也不把word-only候选作为完整verbose交付。
日/韩实际语言业务、所有后续Serve任务继续。默认rev276未变。
