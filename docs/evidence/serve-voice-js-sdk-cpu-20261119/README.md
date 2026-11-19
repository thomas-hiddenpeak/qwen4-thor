# 官方JavaScript音色SDK：9项针对性CPU通过

复用已固定OpenAI JS7.47.0，使用其真实voiceConsents/voices/Speech/
Chat/transcriptions方法。新增单请求客户端保留SDK multipart和SSE
字节、禁止重试/自动分页/重定向，限制请求与响应大小；凭据不进请求
日志，私有证据仅写.q4t-work。新增代码不是运行时依赖。

9项CPU全部通过，1.015秒、0GPU：实际上传序列化、metadata方法/ID、
错误状态、二进制与SSE Speech、Chat音频/历史/扩展字段、无原文ASR、
输入哈希与读写上限、端点/符号链接/TLS校验保护；另外用自有临时
HTTPS服务和真实子进程验证受信证书可用、未受信证书在HTTP前拒绝。
其余响应为CPU夹具，不能替代真实模型、生产入口或注册业务验收。

同一9项已接入现有host CI，不重复完整矩阵。默认rev326与生产引擎
没有变化。下一步把本客户端用于实际音色/ICL业务；原encoder数值
口径待决保持，继续独立Realtime协议实现与全部master任务。
