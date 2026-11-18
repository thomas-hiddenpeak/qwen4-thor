# 原生Realtime与Live实施计划

属于Serve master的A10/A01/A12/N05/R03/Q01—Q03，不新增独立终点。
注册音色ICL的encoder数值口径仍待决定；此处先推进不依赖该决定的
传输与会话实现。两者最后都须完成真实业务、恢复和可回退交付。

## 固定依据与已有实现

采用2026-11-19保存的官方Realtime WebSocket、转录及会话文档，
并核对已固定OpenAI JS7.47.0的类型/方法；传输遵循RFC6455。
来源身份保存在`.q4t-work/serve-realtime-20261119/reference-docs`。
Realtime的`/v1/realtime`和Live的`/v1/live/sessions`具有不同会话
事件，分别实现，不能拿自定义WebSocket topic冒充官方协议。

现有原生HTTP/TLS、客户准入、ASR/VAD、LLM工具/结构化、TTS与Guardian
可复用；入口WebSocket升级及双向转发已通过CPU对端验证，后端真实
会话仍待接通，因此尚未提供可用的Realtime/Live模型业务。输入HTTP
上传后的SSE也不等于流式音频输入。

Orator只提供设计参考：统一采样时钟、阶段状态和有序排空；实际
缓存上限、截止时间、客户隔离、事件合同由q4t独立实现。不复制
AGPL源码，不增加Orator、nginx、Python网络服务等运行时依赖。

## 已有阶段证据

WebSocket基础16项CPU、音频队列12项CPU及
[HTTP字段/双向TLS25项CPU](evidence/serve-realtime-duplex-cpu-20261119/README.md)
已通过；[原生入口升级16项CPU](evidence/serve-realtime-upgrade-cpu-20261119/README.md)
也已通过，后端真实会话/模型及每轮配额仍按以下顺序继续。两个64 KiB转发缓冲
不包含TLS/内核缓冲成本，正式会话准入须计入对应实际RAM。
[实际波形重采样9项CPU](evidence/serve-realtime-resample-cpu-20261119/README.md)
已通过，四种输入率、整数相位和尾部排空待接实际会话/ASR。

## 连续实施顺序

1. **传输基础**：共享有界RFC6455解析/编码，严格掩码、分片、UTF-8、
   ping/pong/close、长度与背压；握手复用现有系统libcrypto的SHA-1，
   仅用于规范要求的挑战值，密码库失效只影响此接入。CPU反例与真实
   socket/TLS检查；该阶段自身不表示Realtime已可用。
2. **原生入口与治理**：HTTP升级、直接后端和可选TLS入口接线；先鉴权、
   来源与客户/会话容量，再101。单连接双向生命周期有截止时间，排空
   与正常HTTP隔离。会话占用和模型请求准入分开；不能靠一个握手
   绕过后续RPM/TPM、输出/音频预算。旧HTTP/上传/SSE直接受影响项短验。
3. **转录会话**：session.update、append/clear/commit、item关联、增量
   与最终转录、错误、VAD和手动结束；真实ASR消费持续到达的有界音频。
   采样位置使用会话epoch和绝对计数，24k→16k跨块相位保持，不能用
   浮点秒累加代替样本位置。正常结束排空末尾；断开/取消不伪造成功。
4. **对话与输出**：复用真实LLM/TTS与工具执行，conversation/response
   事件、音频/转录增量、工具回填、usage、打断与truncate、取消/错误
   完整接通。背压不得覆盖待发送输出；取消后不继续生成或重复工具。
   客户端播放进度与服务器生成进度分别记录。
5. **Live与音色组合**：独立session.start/started、连续输入输出、close/
   closed与最终usage；原生模型适配和prompt音色继续作为实现任务，
   不用固定拒绝、空输出或沿用Realtime事件结案。
6. **业务与交付**：Python/JS官方客户端、本机TLS、真实录音/图像/工具
   多轮；入口、辅助后端和模型故障分别有界恢复，显式报告中断且不
   自动重放已执行工具。主/轻/回退能力及保留状态核对后发行与合入。

## 每层必须保持的合同

- 输入、解析帧、消息、音频、历史、模型工作区及待发送输出全部有
  实际字节上限，计入Thor统一RAM。只限制单帧不等于限制分片消息。
- 每个item/response有稳定身份和一次终态；临时文本、已完成文本、
  播放截断和实际采样边界分开。新连接不继承未经核验的旧执行租约。
- WebSocket错误不得污染随后HTTP解析；未完成握手不向模型派发。
  关闭后不再处理数据，部分写入不能重发整个事件。
- 正常结束按输入、识别、响应/工具、输出、最终usage顺序排空；各层
  有截止时间和取消。辅助观察失败不得停止健康核心业务。
- 每次验证先冻结范围；非性能改动只跑对应CPU/业务短验。已有身份
  未变证据复用；原失败保留；长期稳定与正常听感仍由用户实跑。

协议来源：[WebSocket指南](https://developers.openai.com/api/docs/guides/realtime-websocket)、
[转录指南](https://developers.openai.com/api/docs/guides/realtime-transcription)、
[会话指南](https://developers.openai.com/api/docs/guides/realtime-conversations)、
[RFC6455](https://www.rfc-editor.org/rfc/rfc6455.html)。
