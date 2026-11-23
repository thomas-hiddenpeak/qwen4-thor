# Live SIP 呼叫控制输入

SIP 专用接听配置要求 `session.type` 为 `live`。验证专用合同之后才去除这个字段，将会话交给原生启动与资源所有者。初版正例遗漏了这个必填字段，结果保留为失败。修正后的用例验证缺失或错误的类型、WebRTC 数据通道权限、客户端指定媒体采样格式均在构造所有者前被拒绝，同一所有者只能启动一次。带私有 MCP 配置的 Responses 仍沿用既有验证。[官方接听接口](https://developers.openai.com/api/reference/resources/live/subresources/sessions/methods/accept)。

运行时校验拒接码为 300 至 699 的整数，转接目标能作为绝对 URI 写入 Refer-To，旁路键音为单个 0–9、A–D、星号或井号。检查覆盖边界、无法精确表示为整数的 JSON 数字、百分号转义、换行、畸形目标和全部 16 个键。未经协商的 WebRTC 旁路发送键音仍会被拒绝。[官方电话控制](https://developers.openai.com/api/docs/guides/voice-sip?voice-api=live) 和[拒接接口](https://developers.openai.com/api/reference/resources/live/subresources/sessions/methods/reject)。

正式宿主与引擎构建零警告，15 项针对性 CPU 用例通过，未启动 GPU。HTTP 电话接口、传入通知、信令交付、SRTP 与真实通话仍待实现和验收。下一步应使中继身份映射到具名客户端，以真实信令提交结果驱动接听、拒接、转接和挂断的 HTTP 回执。[构建与用例记录](cpu.json)。
