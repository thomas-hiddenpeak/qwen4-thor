# 实际语音打断成功暴露取消用量分类遗漏

2026-11-20，候选485a38a3。一次轻量模型、一个WSS、两项原生工作。
实际PCM首块后输入录音，speech_started触发同一默认回答cancelled，
原生ASR完整识别中文；没有自动新回答。取消回答617 tokens、ASR76，
693 tokens全部结算，预留/会话归零，严格监控与所属资源清理通过。

但response.done.usage.output_token_details为空，缺少音频/文字分类；
检查器因此失败，原阶段FAILED保留。代码核对发现音频分类只从完成
EOF后的delivery.Usage读取，取消已经计入总量的TTS工作没有传到
分类。不能用总费用正确掩盖明细缺失，也不能从PCM长度猜token数。

接下来传递实际TTS阶段计数，包括取消/异常；不改变推理、采样或
配额扣费。先做直接CPU与构建，再只补该终态的实际语音短验。先前
成功语音/工具和未改变的后端证据复用，恢复/交付及全部master继续。

[审查](review.json)、[索引](index.json)、[原始记录](records.tar.gz)。
