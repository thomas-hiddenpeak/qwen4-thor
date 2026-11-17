# 原生VAD真实多段转写HTTP（2026-11-17）

**通过**，1启动/12创建/57.048312秒，独占租约COMPLETED；程序正常
退出、剩余自有进程为空、端口释放，默认rev276保持。
实际候选`f6f3370f481431cf2d0d32bc1f677ab800653edd6a787d5efd3c916b1f3f7c71`。

- Python SDK完整两段JSON：每段原中文精确、26个真实词边界，
  segment全局ID0/1；seek72/635，实际start0.884/6.596秒，
  end4.164/9.876秒。实际token字节、统计与zlib压缩率均核对。
- JS SDK跨段流式15事件，最终真实input123/output22；delta文字和
  概率逐项重组成终态。VTT全局cue和单一头，完整重复实际原文。
- 阈值1对非零真实音频与自然零PCM各输出空结果/0模型token，
  对应实际prompt/generation计数无增加；不是伪造无语音概率。
- 四段超过最大3段原子拒绝；三段在整请求累计24输出时明确失败，
  无部分成功。首delta后取消被计为aborted，资源释放后核心42。
- 初始VAD资产缺失503仅降级VAD；普通ASR和Chat正常，显式冷却后
  同PID恢复。最终VAD/ASR/对齐ready、compute_failures均0；
  VAD两次加载/一次故障，0设备权重字节。

音频来自已有固定官方中文PCM，重复并插入明确零PCM；原始资产
和默认程序未修改。本组是转录/分段/接口业务接受，非人工时间精度、
全部语言或整模型质量/性能接受。候选未发布，继续服务包/原生TLS/
主轻及恢复交付，再推进全部master任务；长稳由用户执行。

[结果](result.json)、[业务结果](quality.json)、[来源绑定](binding.json)、
[清理](cleanup.json)、[独占租约](lease/exit.json)。SDK完整记录、
原始来源清单和模型日志保存在绑定的raw目录。
证据整理曾误用旧监控文件名observations.jsonl；实际events.jsonl和
nvml-queries.jsonl完整归档。该整理错误没有重新运行GPU或改写原件；
HTTP/SSE原始CRLF通过明确Git binary属性保留。
