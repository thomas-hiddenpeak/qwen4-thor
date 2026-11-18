# 原生TTS完整EOS与实际发声通过

2项比较门CPU/0.114秒通过，构建零警告；复用已验FP32 talker
与codec数值，完整中英文各一次，分别40/39帧、3.20/3.12秒，
均自然EOS，未达到160帧上限。完整波形由独立已验原生ASR在
空系统提示、无语言/文本强制下转写，忽略标点/大小写后均与
输入完全一致。没有裁静音或用截断波形通过。

同严格租约1 talker/1 codec/1 ASR加载，总17.659秒，
19次NVML正常、末尾无GPU进程/锁释放；0主LLM/HTTP，默认
rev306保持。原始WAV/PCM/codes与ASR结果保留于
`.q4t-work/serve-native-tts-20261118/utterance-01/run`，SHA见记录。
CPU参考依赖的泛化tokenizer警告保留；夹具只编码固定ASR模板，
结构token序列与原已验模板一致，没有替换tokenizer或重新跑模型。

本次证明两句实际内容，不宣称全部voice/表达风格已验。下一步
speech/Chat音频输出、格式/语速/指令、SDK/TLS与恢复交付及
全部master，总目标保持active。
