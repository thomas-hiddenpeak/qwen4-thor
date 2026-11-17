# 原生重叠语音实现与验收

A10继续实现同时说话的真实音频路径。rev296的ECAPA聚类只能为
顺序声音窗口分人，不能从混音中恢复两个人的词，不能据此关闭重叠
问题。以下阶段只是实施、验证和提交边界，总目标保持active。

## OS01：固定实际分离模型与资产合同

采用SpeechBrain SepFormer WSJ0-2Mix固定模型revision
`3a2826343a10e2d2e8a75f79aeab5ff3a2473531`，官方训练来源
`fc2eabb7416b0ae68b3baaadb39972ea1d153985`。模型是8kHz单声道
两源分离；原生后端保持encoder/masknet/decoder完整计算与F32权重，
不把16kHz的ASR特征当作其输入。重采样、尾部对齐单独定义和验证。
RE-SepFormer同时进行了元数据比较；其masknet约185.5MB，当前
SepFormer约113.1MB，不因为名称含“资源高效”就推断更适合本机。
本项不是性能优化，不作速度或精度提升主张。

下载三个固定checkpoint，合计113,142,927字节，分别核对官方LFS
SHA；CPU weights_only离线转换为单一safetensors，保留名称映射、
shape、dtype和逐字节值。原始文件与转换产物合计上限250MiB，
时间600秒，下载/转换前至少4GiB空闲；源码/元数据上限3MiB。
这是本组小资产预算，不改写旧阶段的空间门槛。生产不读pickle，
不引入Torch/SpeechBrain服务或额外分离进程的强制依赖。

首组8项CPU/180秒、0GPU/HTTP：固定配置；实际完整张量合同；
非法名称/重复/缺失；shape/dtype；区间/长度；取消与失败原子性；
encoder/decoder/位置buffer映射；实际转换文件与原始checkpoint字节
核对。必要构建单列，不触碰已发布build/q4t。

## OS02：原生计算与差分

实现stride-8波形卷积、GroupNorm、250帧半重叠分块、两组双路径
Transformer（每条路径8层）、真实mask与转置卷积波形输出。遵守
训练时LayerNorm epsilon、位置编码、残差及重叠相加合同；保留
原始位置buffer，不能用新的近似表替代。工作区所有中间值、attention
及库空间按统一DRAM显式预算，并在真实完成阶段同步进度与取消。

先冻结工作区/绑定CPU边界，再固定官方CPU参考与有界GPU差分；
比对encoder、四个路径阶段、mask和两路真实PCM。冻结同源纯净
声音的混音、单人、静音、非整步长与容量边界，分别判断数值合同和
分离效果；原始失败保留，不能只用ASR语义结果代替数值正确性。

## OS03：重叠检测、分路转录与全局标签

分离波形实际接入现有VAD、ASR、词对齐与ECAPA，检测两路有声
区间的重叠；声纹用于跨块/跨路关联，不能固定把输出通道当作人。
按真实时间输出允许重叠的片段，保持请求内已知参考和未知首次命名。
单人/无声输入不得凭空制造第二人；输出预算、usage、取消、上传与
引用所有权仍共享同一请求的限制，不能为每条分路重复放大额度。

本固定模型只能产出两路。超过已支持同时说话数的输入不能声称已经
完整识别；后续扩展仍按实际多源能力决定和验收，不能以拒绝/披露
替代总计划要求。离线语料上的公开分数不能代替本项目实际业务证据。

## OS04：独立健康、服务与恢复

独立资产身份、启用选项和资源预算贯穿CLI、配置schema、健康、
capabilities、prepare与主轻/回退资格。普通资产/计算/worker故障
只降级分离能力，健康Chat与既有音频继续；真实设备故障保留保护。
按实际完成进度运行，恢复不盲目重启模型，不允许回退悄悄删除已声明
能力。实际混音JSON/SSE、双SDK/TLS及恢复后仍能保留两路词是出口。

## OS05：可回退交付并继续总计划

冻结适用业务短验，复用未变证据，不做完整性能或主模型质量矩阵。
通过后形成可核验包、默认/回退/协议一致的发布和主线合入；随后
继续Chat音频、合成、Realtime和所有剩余master项。长稳工具和
当前后端交接属于Q03，长稳实跑由用户执行。

## 固定官方来源

- [模型与许可](https://huggingface.co/speechbrain/sepformer-wsj02mix/tree/3a2826343a10e2d2e8a75f79aeab5ff3a2473531)
- [双路径实现](https://github.com/speechbrain/speechbrain/blob/fc2eabb7416b0ae68b3baaadb39972ea1d153985/speechbrain/lobes/models/dual_path.py)
- [Transformer实现](https://github.com/speechbrain/speechbrain/blob/fc2eabb7416b0ae68b3baaadb39972ea1d153985/speechbrain/lobes/models/transformer/Transformer.py)

官方声明Apache-2.0；使用/借鉴保留作者和许可。官方WSJ0-2Mix
成绩不保证其他域效果。原始语音夹具不随代码分发，保留来源与散列。
