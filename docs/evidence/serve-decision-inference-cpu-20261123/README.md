# Decisions 原生推理入口

POST /v1/decisions 进入原生 TLS 网关和模型服务，使用调用客户端的请求槽、截止时间和同一 RPM/TPM 租约。每题从 target prefill 最后一行 BF16 logits 对单 token 选项代码取分，合计带前导空格和不带空格的拼写。选择概率在给定选项内归一化；置信度记录获选代码在完整词表中的概率质量。拒答有独立代码。每题由普通模型路径实际接纳一个分类 token。共享资料与题目位于不同的缓存边界。

25 项 CPU 检查通过，实际引擎与入口零警告构建。覆盖数值、拒答、输入、缓存边界、HTTP 连接，以及共用准入路径的 Moderations 保护。真实 SDK、模型质量、实际缓存和用量另行核验。

[CPU 日志](cpu.log) · [构建及来源](result.json) · [OpenAI Decisions 参考](https://developers.openai.com/api/docs/guides/decisions)
