# 修复实际打断请求的默认对话归属

2026-11-20。实际SDK从同一请求构造函数创建conversation:auto音频回答，
覆盖旁路base；服务端VAD保护规则不变。1 Node真实构造检查和1 Python
归属反例通过，20来源反例及SDK语法通过。原19项其他检查保持有效。
汇总器只匹配tests而漏掉单数test，原FAIL保留，按原stdout/退出码
离线复核为通过，没有重新执行CPU或GPU来覆盖失败。

后续只补轻量1模型/1WSS/1TLS/2原生工作/420秒的实际语音打断。
当前模型、数值和API默认值不变；已通过语音/工具及主档不重跑。
原全部业务失败保留，资格/恢复/交付和master继续。

[审查](review.json)、[索引](index.json)、[原始记录](records.tar.gz)。
