# Chat音频可回退交付 rev306

默认服务包`3c136a5884f87dde06cdd10b8bbc286717361e6bdf397aff4c5f3a37941c06f4`，引擎`bcfa0b56c179426ccbba26fec3463d37bd75265fe22e14a7405dac16051ff3b0`。
19项STOPPED检查/发布、实际整根回退与再前进全部完成，294.806秒，
0新增GPU；outer启用且累计启动0，服务保持STOPPED。

Chat的WAV/MP3语音输入、双choice、JSON/SSE、工具闭环及用量
已验；双SDK/原生TLS、主轻和整包回退后仍实际回答原音频问题，
原A/B/A、ASR/对齐/VAD及核心业务保护保持。
参见[直接HTTP](../serve-chat-audio-http-20261118/README.md)、
[主轻](../serve-chat-audio-profiles-20261118/README.md)和
[原生服务/恢复](../serve-chat-audio-service-20261118/README.md)。

核心/client/outer预算字节保持，profile仅显式扩展已验Chat音频合同
及精确引擎身份；原history/pending/selected/deadline保持，实际整根回退恢复
各根原版本周期。人工根`e9cd734a50c8938b62217df331d1bb04c364d0ef59c49481bb46bea6db72f197`、自动payload
`6171679f81ef9e1e40f013a028830b21794b647d90f406d9ac57c17f2f5d4a7e`均使用当前引擎与保留控制；未宣称独立
旧引擎具备新增能力。前驱rev296及原day入口保留。

5个无引用intent分4+1可恢复归档，0删除、未改generation/version
归档集合。资产/辅助
故障只降级对应能力，真正设备故障保护保留。已验托管配置使用
8个声音窗口，CLI默认256；预算均显式绑定，不将容量拒绝算成功。

下一步完成主线检查和合入；重叠语音原质量问题、非语言声学信息、音频输出/历史/Realtime和其余全部master继续。没有宣称完整OpenAI兼容、
全模型质量、性能或长稳通过，总目标仍active。
