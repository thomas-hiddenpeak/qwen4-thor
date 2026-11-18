# checkpoint采样后的内置完整音频见证

复用原固定请求：Speech为ryan、Speak calmly、MP3；Chat为仅音频WAV。
两者文本均为“Hello, this is a native speech test.”，没有改变采样seed
或搜索候选。当前C05完整Speech 58368字节，Chat 111404字节；身份见
witness.json。旧贪心见证及旧发行均保持，不用新输出重签旧证据。

首轮1主模型、4 POST、83.326秒：Speech独立无提示ASR通过，Chat
200完整音频、stop终态。继承的注册驱动只允许TTS两次加载，未计入
Speech→ASR→Chat的第三次加载，在事后观察处失败。原结果FAILED、
原wire与加载限制不改写；实际TTS ready、零失败，核心readiness200。

仅对已生成的完整Chat做补验：0主模型/0HTTP/1ASR、6.338秒，复用
已验证解码和独立ASR，不重复合成。两段均完整识别原词，仅句号与
逗号差异；按事前规范化规则通过。未使用目标文字或语言提示。

两阶段严格租约NVML均正常，末次无GPU进程，原模型正常退出、自有
清理/端口和锁释放确认。此见证证明固定请求的完整内容与字节身份；
主/轻profile、恢复切换和完整音色质量仍须分别验收，正常听感待验。
无性能收益或长稳主张，默认rev316未替换。
