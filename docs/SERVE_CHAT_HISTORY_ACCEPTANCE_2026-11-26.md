# A02 聊天历史验收对照

2026-11-26。A02 覆盖客户端输入、模型实际看到的提示与 token、工具回填和已存消息的公共读回。表中设备记录保留各自的发行身份。

| 合同 | 实现入口 | 可核对的证据 |
|---|---|---|
| system、developer、user 和参与者 name | `ValidateChatMessageProfile`、`RenderChatPrompt`。指令按原顺序使用 Qwen 的 system 边界，参与者名进入转义的消息头，存储返回客户端角色。 | Node SDK 通过多指令、按名字区分参与者、无 user 对话及真实项目查询。[原始请求与模型结果](evidence/serve-chat-input-20261108/README.md) |
| assistant 文本、空轮次、refusal、推理内容和引用 | `ResolveMessageReasoning`、`RenderChatPrompt`、`StoredChats::Messages`。原文 Unicode 字符偏移与引用一起保存。 | [努力级别与 SSE](evidence/serve-chat-style-business-20261116/README.md)、[引用与重启](evidence/serve-chat-annotations-business-20261117/README.md)、[拒答旧对象回读](evidence/serve-stored-message-projection-20261125/business.json)、[空轮次、工具调用及回填](evidence/serve-null-assistant-history-20261126/business.json) |
| function、tool、并行回填和自定义工具 | `NormalizeLegacyTools`、`PlanToolResultOrder`、`AppendToolCalls`。保留外部 ID 和参数原文，按调用关联结果。 | [旧 function 回填与迁移](evidence/serve-legacy-tools-20261108/README.md)、[混合自定义工具](evidence/serve-custom-tools-20261108/README.md)、[类型与精确参数](evidence/serve-history-argument-types-20261125/business.json)、[字典参数引用](evidence/serve-tool-map-references-20261126/business.json) |
| 多模态输入、顺序和历史 | `ChatFileInputs::Prepare` 处理图像与 PDF，音频历史按客户解析；视频实际使用有序帧和时间信息。 | [PDF 文字与页色](evidence/serve-file-integration-business-20261110/README.md)、[音频输入、输出与历史](evidence/serve-tts-delivery-20261118/README.md)、[有序帧业务](evidence/serve-visual-frame-parts-20261126/business.json) |
| 模板与 tokenizer | 生产预检返回提示字节、token ID 和扩展视觉 token。 | 冻结模板 21 例各检查原始和解析模式，另有 12 个组合。原始模式的 16 例与检查点直接一致，5 例使用服务的无损工具参数包装。[完整构建对照](evidence/serve-null-assistant-history-20261126/full-build.json) |
| 存储消息的内容和标准客户端 | `StoredChats::Messages` 为六种角色保留原 ID。简写文本和图像在读取时补齐标准字段；视频映射为按序图像，原分组和时间保存在 `original_content_parts`。 | 旧对象逐字节保留，6 个旧页和 6 个新页均通过官方和随包合同。[原错误与修复对照](evidence/serve-stored-content-parts-20261126/cpu.json) |

当前存储候选与空助手候选之间，`src/`、`include/` 和构建定义的唯一差异在 `src/server/chat_storage.cpp`。两者共用前述 54 组模板验证，新增读取行为单独验收。Python SDK 3.55.1 默认模式可以读取混合角色页，其可选严格模式对整页施加了仅助手角色的限制。应用可通过 SDK 的原始响应入口，按官方 HTTP schema 校验完整历史。[固定 SDK 结果](evidence/serve-null-assistant-history-20261126/business.json) · [官方消息列表](https://developers.openai.com/api/reference/resources/chat/subresources/completions/subresources/messages/methods/list)

实际视觉 HTTPS 已通过：绿色静态图、蓝色视频、原助手文本、两帧的时间与顺序均已核对。[模型结果与实际历史](evidence/serve-stored-content-parts-20261126/business.json)

待完成的是存储候选的可回退发行及主线集成。原始视听素材的模型质量和独立语音听感记录继续随对应任务追踪。
