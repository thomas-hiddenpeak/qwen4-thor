# Speech四种压缩格式的实际中英文内容通过

复用已验中英文原始TTS PCM，每句分别MP3、AAC、Ogg Opus、
FLAC编码并由已验隔离解码器还原，8条完整音频经独立原生ASR
全部匹配原文（只忽略标点/大小写）。没有裁剪codec priming/tail、
添加目标词或语言提示，也没有重新合成挑选样本。

16个CPU编解码worker均回收；3 ASR加载、0 TTS/codec GPU/
主LLM/HTTP，10.644秒，同严格租约完成并释放，NVML正常、
默认rev306保持停止及原身份。程序、原件和每格式真实音频见
`.q4t-work/serve-native-tts-20261118/encode-content-01`。
10项CPU的生产源码身份未变，直接复用。通用CPU tokenizer的
既有regex提示保留，使用固定无文本提示结构，token ID已核对。

为维持至少1GiB磁盘余量，只将已完成TTS/ASR构建中CUDA编译器
探测的临时文件无损归档，逐文件校验后释放临时副本；模型、
实际程序、库和验收原件均未删除，归档摘要及文件清单随附。
本组产物限制24MiB，实际记录见reconciliation。

这是压缩音频内容资格；HTTP/SDK、生成中流式、资产/资源/恢复
与正式发行仍继续，4倍变速内容问题仍未解决。全部master保持
active，不能以本组通过宣称Speech接口或总目标完成。
