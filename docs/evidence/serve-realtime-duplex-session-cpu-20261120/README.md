# 同一Realtime会话输入与轮次：针对CPU

日期：2026-11-20。实际RealtimeConversationSession组合原生输入owner，
同一历史接收GA音频事件/隐私转写、检索与精确删除。手动提交不自动
回答；response.create在等ASR时已固定请求ID、期限、历史、Prompt和
设置，只发一次created，失败/删除/取消不执行旧消息或空提示。

VAD在实际commit冻结自动回答；语音开始只打断默认回答，旁路回答
保持。同一会话的ASR与回答按实际线程退出交接，响应可用时先交给
响应，不让后续ASR抢占单客户限额。失败管线在ASR仍借用时取消，
仅在其退出后非阻塞join，随后健康文字回答和文字设置更新仍可用。
四个未发送响应捕获各限96MiB，含历史、Prompt和设置，计入已有预算。

首轮49项41PASS/8FAIL原件保留。问题包括夹具整数构造器把0.5写成0、
VAD未commit前误以队列空代表完成、Start校验计数遗漏；另有真实接口
遗漏：SDK 7.47.0客户端总类型引用旧ResponseCreateParams，单独导出的
WithReferencesParam支持item_reference，却在外层被拒绝。现在完整
响应按精确新参数root校验，再严格校验原事件外层；固定SDK原图保持。
引用不能创建新item，也不能赋予客户端转写原生来源；未知/重复字段
和已删除引用仍被拒绝。

仅补原8失败、2新引用检查和2既有wire保护：12项/0.596秒，20来源反例
通过；41项原PASS按受影响路径与精确源码复用。最终53个不同用例/
61次执行有有效PASS，新增19项。最终host构建35.948秒零警告，SHA256
`e3768696482282454f1df5a10eedb222a6e05ea17fe883eaab842cf4ee0f9901`。
模型输出是明确CPU夹具，不是实际ASR/TTS质量证据。

HTTP ChatServer原生ASR/VAD回调尚未接入，空闲触发、直接混合音频和
同会话输入管线恢复仍继续开发；不能称完整双向业务已验收。0GPU，
默认rev376/STOPPED和原包保持，全部Serve master继续。

[结果](acceptance.json)、[索引](index.json)、[全部原件](records.tar.gz)、
[双向计划](../../SERVE_REALTIME_DUPLEX_2026-11-20.md)。
