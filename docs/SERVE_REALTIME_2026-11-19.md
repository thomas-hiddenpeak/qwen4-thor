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
可复用；入口WebSocket升级及双向转发、实际原生ASR手动提交会话现已通过
[真实官方SDK/WSS业务](evidence/serve-realtime-transcription-business-20261119/README.md)。
自动分段/可选处理器、对话与Live仍继续实现，实际Guardian组合和
可回退交付尚待完成；默认rev326保持。不能把手动转录当作完整
Realtime/Live，也不能把HTTP上传后的SSE当作持续音频输入。

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

[后端会话治理23项CPU](evidence/serve-realtime-session-cpu-20261119/README.md)
已通过：独立客户/全局会话槽、永久HTTP移交、真实socket背压与反向
取消、关闭和异常；模型事件调度、实际逐轮配额及ASR仍须接通。

[实际PCM/G.711接线7项CPU](evidence/serve-realtime-pcm-cpu-20261119/README.md)
已通过：直接读取队列两个片段、精确解码/重采样、尾长与事务性取消；
后续将该实际波形送入既有ASR，不使用伪造转录作为业务证据。

[实际手动转录执行接线21项CPU](evidence/serve-realtime-transcription-cpu-20261119/README.md)
已通过：GA/旧版设置、实际原生ASR回调与当前客户逐轮准入/结算、
有界输入/输出、取消与完整终态。尚未完成真实模型/SDK业务，VAD/
降噪/延迟、对话/Live和TLS/恢复组合继续实施，默认rev326保持。

[手动转录真实业务](evidence/serve-realtime-transcription-business-20261119/README.md)
已通过：中英PCM/G.711、官方JS/WSS、实际逐轮配额与取消后业务。
21项原生CPU+3项SDK/TLS CPU、1模型/1入口启动、3会话5有效提交/
1Chat，44.378秒，44次NVML正常。固定准入控制夹具不能代替实际
Guardian恢复组合，默认rev326仍保持；继续下列未完成项目。

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
