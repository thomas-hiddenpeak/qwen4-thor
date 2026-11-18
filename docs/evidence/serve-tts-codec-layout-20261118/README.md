# TTS codec执行入口4CPU通过

271项F32 decoder权重及完整vocoder/dilation绑定、统一RAM的
三组波形缓冲/固定im2col与转置权重tile预算、300帧/25上下文
及尾块采样裁切、非法code在GPU前拒绝，共4CPU/0.0035秒通过，
构建零警告、874来源/11负控通过。0GPU/0模型加载。

这里只验证执行入口，真实RVQ、卷积/Transformer、PCM与语音
质量尚待实现/验收，不能当成已发声或接口交付。继续TTS03—07
及全部master；已过FP32 talker不重复，默认rev306保持。
