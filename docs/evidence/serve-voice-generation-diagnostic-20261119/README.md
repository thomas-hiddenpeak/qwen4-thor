# 原失败样本完整生成轨迹与官方数值对照

复用原SAMPLE文本、auto语言、ryan、nonstreaming、空instruction及
seed=0，1GPU talker原样重现已保留的47帧完整音频码和自然EOS。
0codec/ASR/主LLM/HTTP，不改变生产或注册保护，不搜索seed/文本。

官方1CPU talker从实际文本构造prefill，空instruction按官方wrapper
传None，随后以同一组实际码逐帧推进cache。比较完整48次talker
hidden/logits（包括EOS决策），并在预先固定0/1/16/32/46帧比较
predictor首末组，实际执行75次predictor；共118项全部通过。
最大relativeL2 1.1773348e-05、最小cosine
0.999999999931，原门分别1e-4和0.99999999。

总64.246秒，原生9.880秒、参考进程51.538秒，62次NVML正常，严格
租约完成。CUDA诊断构建2.380秒零警告，既有数学对象源码SHA保持。
CPU参考tokenizer原警告保留；本例实际token/prefill/后续逐帧输出已
独立对照，未以忽略警告代替结果。原采样后的声纹一致性失败保持。

这些结果排除本例已检查的生成轨迹数值偏差；不证明所有帧/所有音色
质量，不证明机器声纹失败一定等于听感变化。已发起可选正常播放
听感反馈，声音一致性、注册完整HTTP及真人样本仍待解决；先继续
不依赖此结论的包与恢复接入，默认rev316保持，总目标active。

另外核对了固定上游[SpeakerRecognition实现](https://raw.githubusercontent.com/speechbrain/speechbrain/c0241278f93ef02cd5191f03caf4e5f8e1292bf6/speechbrain/inference/speaker.py)：
其录音对验证默认使用0.25余弦和未做global embedding归一化的向量。
本地0.65原为已验diarization参考匹配门，不能将两种使用场景的
门槛等同于通用身份保证。即使上游0.25也不能解释所有窗口对0.049；
当前未据此改门或追认通过，后续须用独立正反例检验完整核验策略。
