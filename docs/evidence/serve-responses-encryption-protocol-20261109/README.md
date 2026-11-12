# A07.3 加密 Responses 双端针对性验证

2026-11-09；18 个不同 C++ 用例最终通过，28 次执行；22 个实际渲染的响应/SSE 值通过固定官方 SDK schema。host/引擎零警告，候选 687fc18f，0 GPU。

首轮 9 过/9 失败：认证解密成功，但来源前缀为带 ID/status 的 canonical 持久项，误用外部 input shape 拒绝。只改 authenticated payload 校验为 conversation shape，外部输入与生成计划校验保留；9 失败及 1 归属/篡改负控补验全过。首失败、旧源码身份与两轮日志保持。

覆盖 store=false 密文手工回填、完整/后缀/省略来源前缀、冲突/篡改/跨客户端/模型拒绝、summary 与 JSON/SSE/持久回放同密文、重启 previous_response、Conversations 不改原项、后台排队恢复、真实历史工具结果适配、截断状态、恢复后容量裁剪和加密失败无成功终态。CPU token delta 是合成夹具，不能代替真实模型业务。

`response.json`/`response.wire` 是实际 C++ 渲染输出，包含合成上下文密文，不包含私钥；私钥夹具仅留工作区。原加密基础 16 CPU 来源未变，直接复用。

**尚未交付完整 A07.3**：继续原生密钥管理入口、官方 SDK/真实模型短验和可回退发行；当前默认 A07.2/STOPPED 保持，总计划其余全部任务仍必做。
