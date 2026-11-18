# Base真实声纹条件：官方数值对照通过

完整Base源3,857,413,744字节SHA256通过并保留为只读资产，固定
Qwen revision fd4b254389122332181a7c3db7f27e918eec64e3。三次网络
中断/重试耗尽原记录保留，前缀逐段校验复用；最后续传1.538GB，
811.946秒，只有全源SHA通过才发布完整文件并进入GPU阶段。

同一严格租约1次GPU Base talker和1次官方CPU参考，两例各2帧，
134项对照全部通过；共49.855秒，原生6.734秒、CPU参考36.340秒。
官方两数学模块原样SHA绑定，FP32权重/计算、eager attention；
实际调用官方prompt构建，传入独立验收的2048向量，没有builtin
speaker替代。比较text投影、条件序列、talker和15个predictor的
隐藏值/logits；64个teacher-forced greedy音频码全部一致。

最大相对L2 1.328e-5、最低cosine 0.9999999999119，在冻结的
conditioning/dense门内，未改变阈值。48次NVML查询全正常，租约
COMPLETED、驱动退出0，默认五项身份未变，主LLM和HTTP均0。
既有18生产CPU与4验收器负控按身份复用，未扩大性能/整模型矩阵。

本阶段故意止于2帧，没有声称自然EOS、完整音频、风格或API通过。
接着以生产资产worker/upload，从原始音频重新算声纹并合成两句，
再用独立ASR检查完整内容；持久登记/撤销/客户隔离和最终API继续。
正常播放听感与声音相似度也不能由本数值阶段替代。

[结果](result.json)、[逐件摘要](index.json)、[固定源与原始记录](records.tar.gz)。
完整中间矩阵保留在本地冻结目录，逐件SHA在阶段result中；权重
不入Git。当前默认rev316保持STOPPED，总目标仍active。
