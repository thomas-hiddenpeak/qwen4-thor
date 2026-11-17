# 原生音频后端与标准接口实施

A01/A02/A07/A09/A10/A12的音频缺口继续属于总任务。当前主模型
checkpoint只有文本/视觉，现有音频字段占位不能执行转录或发声。
新增真实语音模型和原生执行路径；不能用空结果、文本回显、统一拒绝
或强制外部推理服务替代。rev266保持已验默认，候选通过后再发行。

## 子目标与依赖

1. **ASR01资产及数学合同**：固定Qwen3-ASR-0.6B的官方revision、
   配置、tokenizer、权重形状与dtype；原生严格解析，拒绝语义漂移。
   保持原主模型目录和reference只读。模型资产按部署显式启用，
   不随核心包强制下载，失效只使相应音频能力不可用。
2. **ASR02原生计算**：有界音频解码/重采样、128维log-mel、音频
   encoder、Qwen3 decoder和实际生成。复用现有CUDA/线性层/隔离
   worker能力；可参考qwen35-thor的MIT实现，移植时保留许可并在
   README注明。不得直接带入其1.7B双分片假设或固定中文提示。
3. **ASR03服务合同**：原生`/v1/audio/transcriptions`的multipart、
   language/prompt、实际usage、JSON/text/SSE、错误/取消/资源预算；
   原生入口、模型登记、Python/JS SDK和服务包一致接入。标准的其余
   参数与输出格式逐项实现，时间戳需实际对齐，不能均分伪造。
4. **ASR04业务与恢复组合**：中/英文真实音频、流式、客户端与
   故障隔离，主/轻profile、原生TLS和可回退交付。复用身份未变的
   文本/视觉证据，不重跑完整模型正确性或五档性能。
5. **后续音频能力**：真实对齐/说话人、Chat音频输入与非空历史、
   实际语音输出、Responses/Batch组合及Realtime。转录交付仅关闭
   对应子项，不关闭整个A10；图像/视频生成、其余媒体及其他master
   任务保持待实现。

## 固定依据与依赖

- [OpenAI转录参考](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
  2026-11-17抓取；本地已有Python/JS固定SDK与新增网页字段分别记录。
  SDK缺字段不代表服务可以丢弃。Docs工具的OpenAPI片段有未展开ref，
  不能据此声称请求schema审计完整；结合正式页面和SDK原定义核对。
- [Qwen3-ASR官方实现](https://github.com/QwenLM/Qwen3-ASR)作为计算
  和模板依据；代码来源需固定commit。官方字/词时间戳使用独立
  ForcedAligner，当前0.6B转录权重不冒充该后端。
- [Qwen3-ASR-0.6B固定资产](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/tree/5eb144179a02acc5e5ba31e748d22b0cf3e303b0)，
  Apache-2.0；权重1,876,091,704字节，SHA256
  `79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea`。
  官方提供vocab/merges/tokenizer_config，没有tokenizer.json，须按
  真实BPE/特殊token/normalizer合同接入，不借用主模型tokenizer。
- 参考qwen35-thor commit `57e29777c2aff8a97f42df6e3d9487b1327f014f`；
  本轮先审查，不把参考服务或其Python依赖加入默认发行。
- 不新增必需nginx、vLLM、Transformers或FFmpeg可执行程序。解码
  优先复用项目的隔离codec；必要系统库仅在启用的后端内使用，
  精确依赖及失效范围随实现登记。

## 当前冻结边界

ASR01先获取固定资产并实现配置/权重合同；下载只写
`.q4t-work/serve-audio-native-20261117/`，总资产不超过2,000,000,000
字节，预留至少8GiB磁盘空间，1800秒上限；每个文件核对固定来源
的长度与Git blob或LFS SHA256，保留失败和未完成文件。不开GPU。

首组CPU最多8项、180秒：官方配置通过及架构/token/数值漂移拒绝，
encoder分块长度的边界，完整权重名字/shape/dtype/区间合同以及
实际checkpoint只读核对。必要构建单列，不运行模型。之后继续
tokenizer、预处理和真实后端；各组实施前固定直接用例与GPU短验
预算，不在本阶段结束时停止总任务。

ASR01完成不等于音频接口可用，也不触发默认发布。PR43在既有
最终head独立完成检查与实际合main；本组使用同一工作分支的后续
提交，不漂移已验包或用户独立长稳快照。

配置/布局及完整checkpoint共8项CPU已通过。tokenizer固定官方
Qwen2Tokenizer的实际输出后，新增
原生vocab/merges/tokenizer_config入口，保持主模型和embedding
profile的原有合同。预算8项CPU/300秒：ASR官方中英文/Unicode/
特殊token及空文本，无隐含EOS，加载污染与限额/取消，原两profile
直接回归；参考tokenizer只在离线CPU验证使用，不成为运行依赖。

tokenizer的8项已通过（2.262秒），16组官方编码/解码一致，原两
profile直接回归通过。完整资产1,880,618,159字节、1532.417秒，
Git/LFS校验全部通过，仍有12.657GB空余。参考环境为Transformers
5.16.1/tokenizers0.23.2，其来源摘要保留；首次驱动把Torch导入当作
CUDA初始化而拒绝，原失败保留。修正后显式无可见GPU并核对CUDA
未初始化，参考仅用于CPU编码，没有加载模型或运行CUDA。

## ASR02特征计算冻结

先实现16kHz单声道PCM至128维log-mel的原生数学路径：400点周期
Hann、中心reflect填充、160采样hop、Slaney三角滤波与面积归一化，
功率谱/log10、8档动态范围裁剪和最终缩放。序列长度按实际STFT
帧计算，不假造尾帧或截断输入；解码/重采样和encoder另接后续路径。
沿用现有C++标准库，先不增加FFT运行依赖，不作性能优化主张。

CPU上限8项/180秒：6个固定合成PCM的完整特征矩阵与官方NumPy
参考差分（零值、冲激、单频/多频、噪声及非整hop），最大绝对误差
1e-5；另2项覆盖长度/输出预算/非有限输入与取消/失败原输出保持。
参考只在CPU生成固定夹具；运行时不依赖Python/NumPy，0模型动作。
通过后继续实际音频解码、encoder/decoder和HTTP，不能据此宣称ASR
服务已完成。所有其他master任务保持原范围。

特征8项已通过（0.289秒），六组矩阵最大绝对误差均为0；累计
[24项基础CPU证据](evidence/serve-asr-foundation-cpu-20261117/README.md)
2.571秒、0模型动作。固定资产、原失败和三份已验CPU程序保留。
下一步真实音频解码及原生encoder/decoder，A10保持进行中，默认
rev266与用户独立day身份不变。
