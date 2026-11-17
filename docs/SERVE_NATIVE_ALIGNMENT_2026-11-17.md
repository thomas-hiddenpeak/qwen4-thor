# 原生音频时间戳与字幕实施

本项补齐A10转录的真实时间戳和字幕，以及A09/A12的相应服务组合。
rev286已可回退交付`verbose_json`、SRT/VTT、实际对齐/完整segment
和VAD切片，见[交付记录](evidence/serve-audio-auxiliary-delivery-20261118/README.md)。
说话人、Chat音频、语音输出及Realtime继续保留在音频总计划；
日/韩真实业务和人工时间精度尚未验收，不因本次交付取消剩余项。

## 固定实现依据

- 官方模型`Qwen/Qwen3-ForcedAligner-0.6B`，固定revision
  `c7cbfc2048c462b0d63a45797104fc9db3ad62b7`，Apache-2.0。
  权重1,835,544,544字节，SHA256
  `47831d0e82f96b20e9034dba01a075ee06436654719f6a68289e49f1b65ce0e7`。
- [官方配置](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B/blob/c7cbfc2048c462b0d63a45797104fc9db3ad62b7/config.json)
  与[官方推理实现](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/qwen_asr/inference/qwen3_forced_aligner.py)
  于2026-11-17重新核对。源码固定
  `7c6daf77a2421100f5fb066495372c00129d39ff`，不是浮动main。
- 时间槽token为151705，5000类、每类80ms；从实际模型分类输出
  获取边界。参考的单调修复算法属于预测后处理，须保留原预测与
  修复信息，不能把修复后的结果描述成原始分类逐项正确。

| 项目 | 已交付ASR | 强制对齐模型 |
|---|---|---|
| 音频encoder | 18层、896维、14头 | 24层、1024维、16头 |
| encoder FFN | 3584 | 4096 |
| 文本decoder | 28层、1024维 | 28层、1024维 |
| 输入词表 | 151936 | 152064 |
| 输出head | 151936词表 | 5000时间类别 |
| 执行方式 | 自回归转录 | 已给文本的时间槽分类 |

官方文本decoder仍构造因果mask。非自回归分类不意味着可以改成
双向注意力；每个时间槽使用其实际位置hidden state分类。不能套用
ASR encoder形状、共享输出head，或把普通生成logits当时间戳。

## 子目标及完成条件

1. **AL01资产/配置/tokenizer合同**：严格固定架构、实际tensor形状、
   BF16和文件范围；时间槽模板及文字单元来自同版参考。中文/混合
   空格语言、日语/韩语分词须逐项实现和验证，参考Python工具不进入
   发行运行依赖。模型资产按部署启用，缺失不得影响健康文字转录
   或核心Chat。复用既有Unicode/隔离读取能力，新增依赖先说明范围。
2. **AL02原生实际计算**：适配真实24层音频encoder和5000类head，
   保留ASR原数值合同。选择局部矩阵/操作数与真实中英文音频验证，
   验证时间槽、因果位置及尾块。GPU之前单独冻结请求/启动/时限；
   不以整体语义成功掩盖错误计算，也不重复完整主模型质量/性能。
3. **AL03服务输出**：从实际对齐边界生成word/segment时间戳、
   verbose_json和SRT/VTT，保持完整转录文本。处理空结果、超界、
   非单调预测、Unicode文字单元、取消、资源预算和失败隔离；
   不伪造speaker或无音频证据的字段。语音活动/说话人仍须后续
   真实后端补齐，不能把相应参数无声忽略。
4. **AL04业务/交付**：实际音频、双SDK与入口、主轻及恢复身份接入；
   对新增行为做必要短验，复用未变的核心证据。通过后可回退发布、
   实际合main；总任务继续，用户负责长稳实跑。

## 首组冻结边界

资产只写`.q4t-work/serve-audio-aligner-20261117/`，上限
2,000,000,000字节、1800秒，持续保留8GiB磁盘空间；每文件校验
固定Git blob或LFS SHA256，失败及未完成文件保留。本组不初始化
CUDA、不运行模型，默认rev276不变。元数据和官方源码已留存摘要。

首组CPU最多8项、180秒：真实配置、encoder/decoder关键结构漂移、
预处理合同、非生成时间槽配置、输出head与词表独立形状、tensor
类型/范围/重复边界、真实checkpoint只读头部核对、原ASR与对齐
身份相互拒绝。必要构建单列。完成后继续tokenizer、实际计算和
所有输出能力，不能把资产合同通过当作时间戳接口已经可用。

PR44最终head 5784729c已通过两组CI并合main（8de16675）；本项为
后续工作，未混入该PR源码和验收。全部Serve总目标仍active。

## 资产合同结果与后续tokenizer组

[首组8项CPU与真实资产通过](evidence/serve-aligner-contract-20261117/README.md)，
0.016秒、0GPU。下一组限6项CPU/180秒：实际时间token/模板ID、
时间token语义漂移、文字注入、预算/原owner保持，以及原ASR官方
16例和marker/padding两项回归。对齐独立profile必须验证第63个
added token `<timestamp>` 为151705且`special=false`；不改变ASR
原62token合同，不引入Python运行时。全部后续任务继续。

## 原生计算开发组

计算复用同版音频卷积/窗口注意力和Qwen3因果decoder；encoder以
固定profile实例化896×18与1024×24，分类head从各时间槽的最终
RMSNorm状态获得5000类logits，不复用末token文本生成结果。
共享算子保持原FP32归约/BF16舍入。GPU动作前另冻结实际短验；
当前开发及6项CPU布局检查上限180秒、0GPU，覆盖真实权重范围、
encoder24分区、时间槽/context/预算/溢出及原ASR两项保护。

首轮实际计算清单已固定在`.q4t-work/serve-audio-aligner-20261117/gpu-plan-01.json`：
2次GPU模型加载（对齐/ASR各一次），中英文420/1505帧；对齐分别
13/36词，共98个真实时间槽。ASR每条只复查前4token，原旧输出仅
作逐位回归，原数值差分失败不追认。1次官方CPU参考、600秒总上限、
0HTTP。encoder输入cos≥0.99995/L2≤0.01，encoder/head cos≥0.9995/
L2≤0.04，原始分类argmax逐项相同；不在结果出现后改门槛。此组
不声称字幕服务或整模型质量已验，后续真实接口工作继续。

## 词单元与时间后处理开发组

本组8项CPU/180秒、0GPU。普通语言沿用官方Unicode L/N/撇号
清洗与CJK逐字规则，并保留原文UTF-8范围。日/韩使用本项目已有
ICU词边界，属于明确的原生分词策略，不宣称与Nagisa/SoyNLP分词
逐项相同；实际日/韩时间戳业务仍需单独验证。没有新增运行库。
时间修复沿用参考最长非降序子序列策略，保留raw/fixed及修改数量；
只对落在音频最后一个80ms量化区间内的越界做显式clamp，其他越界
报相应请求错误。不是均匀生成时间戳，也不隐藏模型原始预测。

## 独立资产与服务预算开发组

本组15项CPU/180秒、0GPU、0完整checkpoint读取：5项新资产
隔离、2项原ASR资产保护、6项新资源/健康/模板合同和2项原ASR
健康保护。资产失败只作用于独立对齐health实例；真实共享GPU故障
继续保留保护。资源估算含统一RAM中的staging/文件页/device，以及
全时间槽logits，不能以权重另一个指针冒充容量卸载。实际完整资产
和服务组合随后有界验证。默认rev276不变，全部master继续。

## 实际请求与词时间戳/字幕开发组

独立对齐加载接入共享GPU/slot/deadline/取消，内部额外输入通过
原RPM下的ExtendPrompt预留并按实际计算计量。词级verbose JSON
和SRT/VTT从真实分类/显式修复得到，字幕只在实际词边界分组。
14项CPU/180秒、0GPU，覆盖分类/CLI/能力、输出/Unicode/时间边界、
格式组合；必要隔离构建单列。实际GPU HTTP短验随后另冻结。

审计发现标准segment还有tokens/avg_logprob/no_speech_prob等字段；
词对齐模型不能提供其置信度。相关统计和VAD实际来源继续实现，
默认segment请求当前仍待接入，不能把word-only候选当完整verbose
或整项交付。VAD/说话人/全部master仍必做，默认rev276保持。

## 首组真实HTTP冻结清单

新增驱动`tools/native_asr/alignment_http_acceptance.py`。固定1次主模型
启动、7条创建、600秒（含清理预留），持同一严格GPU租约：
缺失对齐权重的词时间戳请求、故障中普通转录、故障中Chat、恢复后
Python SDK中文词JSON、JS SDK英文SRT、Python SDK中文VTT、最后
同PID Chat。只改变私有硬链接入口，原资产只读、默认不变。

原真实中文26槽与官方参考逐项相同的已有记录作为接入一致性对照；
中英文文本沿用已冻结质量目标。检查真实音频时长、边界/词序、
字幕及SDK/wire一致、独立health与资源清理。不把这些检查当人工
标注的时间精度或全部语言质量结论。原英文2/72槽差异仍保留。
2项驱动正例和2项时间负控已通过、0GPU；具体资产/源码/SDK/默认
身份在prepare中冻结。本组尚未执行，后续完整segment及交付继续。

## 真实语音活动检测计算组

为VAD/chunking提供真实逐帧概率，采用Silero固定revision
`066409f3d9db45a30d21dfcbf9147826ffdd7052`的16k模型；原生C++
实现右侧reflect STFT/4卷积/LSTM/head，无新增推理运行库。
[MIT来源与许可](../third_party/licenses/silero-vad/README.md)保留。

资产取得上限8MiB/600秒并保留8GiB磁盘，实际3,547,386字节/
28.926秒，包含开发专用JIT参考；每个文件对固定Git blob核对。
原生权重1,239,748字节，15个F32张量/309633参数，512样本一帧、
64样本上下文。每请求独立状态，尾帧补零不扩大实际音频边界。
逐帧speech posterior不能直接冒充整段no_speech_prob；完整segment
的定义与统计来源仍须明确并实现，当前不伪造字段。

本组最多12个CPU用例/180秒：8项纯CPU结构/坏输入/原子所有权/
取消/上下文/尾帧；中英原实际PCM、零PCM与固定噪声4项官方JIT
差分。逐帧prob最大绝对误差≤2e-5、终态h/c≤2e-4，0.5判定
逐项相同，真实语音有正例、零PCM最大prob≤0.1。门槛在运行前
冻结；JIT与原生15张量逐位核对。0GPU，必要构建单列；不构成
完整语音质量或服务发布验收。后续资产隔离、VAD请求组合、完整
segment及全部master继续，默认rev276不变。

## 完整segment统计来源开发组

新增直接采样token/原始logprob/实际温度及可见UTF-8范围记录，
元数据header和trim不计为正文token；跨边界token保留真实ID和
概率，不重分词伪造。无语音概率由实际ASR训练标签`language None`
的分布提供：独立未强制语言prefix，在同一音频encoder上评分
` None`，不能把Silero逐帧概率均值当整段无语音概率。

压缩率复用现有系统zlib，在有界exec子进程里计算真实UTF-8字节
与zlib level 6长度，记录库身份；缺库、取消、超时不影响健康核心。
本组16项CPU/180秒、0GPU：11项新统计/prefix/隔离压缩与5项受影响
原ASR输出保护。实际label推理、段落序列化/配额和HTTP随后完成。
采样温度保留逐token事实，组合输出的segment定义仍须如实处理。

[统计来源16项CPU已通过](evidence/serve-asr-statistics-20261117/README.md)，
0.164秒、0GPU；实际评分、完整格式及HTTP仍继续实现。

## 完整segment执行组

固定16项CPU/180秒、0GPU，随后另冻结真实HTTP短验。单个独立
解码音频窗口形成一个segment；时间取实际对齐首末词边界，
纯标点使用输入clip实际范围，seek为实际输入帧偏移0。token和
平均logprob来自已接受正文，压缩率由隔离zlib实际计算，无语音
概率由共享encoder的独立`language None`完整词表后验评分产生。

segment采用整次固定温度解码，temperature=0时低于-1平均logprob
最多按0/.2/.4/.6/.8/1六次尝试；所有尝试共享原请求输出上限和
deadline，追加输入须重新取得TPM/客户端输入配额，RPM只算一次。
只发布最终完整尝试；预算耗尽报错，不能把半段或混合温度当成功。
普通JSON/SSE保留已有逐token自适应。分类辅助输入也真实计量，
失败压缩库只影响本次segment格式，不污染模型readiness。

[完整segment16项CPU及3项验收器检查通过](evidence/serve-asr-segments-cpu-20261117/README.md)，
必要CUDA构建零警告。真实HTTP组1启动/7创建/600秒，双SDK默认
segment、word+segment/强制中文、显式温度英文、静音、原word-only
和核心Chat；中文token字节/平均logprob与普通真实转录一致，实际
无语音后验在speech<.5/silence>.5，实际zlib独立计算复核。
全部动作同一严格租约，默认不变；随后继续VAD与服务交付。

首轮实际5条成功，词表读取触发验收器2MiB上限，原整组FAILED
保留。仅修固定SHA/长度后的3MiB读取，离线补完实际token/logprob
及posterior校验；[原证据](evidence/serve-asr-segment-http-original-20261117/README.md)。
只补尚未执行word-only/核心两条，另1启动/2创建/300秒；本组累计
上限2启动/7创建/600秒，原5条不重复。候选ELF和默认均未变。

[完整segment实际HTTP分项接受](evidence/serve-asr-segment-http-20261117/README.md)：
累计2启动/7创建/100.611秒，原5条复用、2条补验，默认未变。
继续VAD实际分段与服务/恢复交付，AL03/AL04及全部master尚未完成。

## VAD资产与分段组件组

固定14项CPU/180秒、0GPU：7项VAD资产/原模型I/O保护与7项
采样边界/阈值/静音/前缀填充/长段/取消/预算。1.24MB固定CPU
权重经现有隔离worker按目录/文件类型/长度/SHA核验，没有新增
推理库；加载失败保留既有owner。后续需独立健康/请求驱动重试，
不能成为普通ASR或核心readiness依赖。

分段使用真实16k/512帧posterior，进入>=threshold、离开低于
max(threshold-.15,.01)，短静音保留到silence_duration满足。
不以最短时长过滤短词；prefix_padding只取实际音频且与前段
不重叠。长段按配置真实样本边界有界切分并标记forced_split，
不伪装为自然停顿；超过段数预算必须失败，不能丢弃尾段。
最大600秒与ASR配置上界一致，数值计算未变。组件验证后继续
真实chunking执行、累计配额/输出、服务交付及全部master任务。

[VAD资产/分段14项CPU通过](evidence/serve-vad-assets-chunks-20261117/README.md)，
0.071秒、0GPU；继续独立健康、多段ASR和所有服务/交付任务。

## VAD运行时与跨段输出开发组

16项CPU/180秒、0GPU；清单固定在
`.q4t-work/serve-audio-aligner-20261117/vad-runtime-01/plan.json`。
独立CPU健康与资产恢复，阈值/静音/前缀真实切片；共享整请求的
RPM、TPM、输出预算和取消截止。全局word/segment时间从实际切片
样本偏移得到，seek为其10ms特征帧索引；段落统计/IDs保持原观察，
仅总显示文本跨非空片段插入空格，不伪造对应token概率。
字幕只出现一个VTT头且cue全局递增。短末帧可回看紧邻真实样本至
至少10ms，不重叠已有片段；无法满足时原子失败，不补造PCM。
VAD判定全静音输出空结果/零模型token并仍计一次RPM；原生ASR
整clip无语音后验属于不同观测，不能相互替代。业务短验随后冻结，
AL04完整服务/恢复/交付与全部master继续。

[运行时CPU组通过](evidence/serve-vad-runtime-cpu-20261117/README.md)：
16C++与3验收器用例，零警告候选engine-03。下一组固定1次启动、
12创建、600秒，严格连续GPU租约。普通转录与两条核心Chat保护、
VAD坏资产/恢复、两段中文JSON/流式/VTT、自然静音/阈值1、四段
超过最大3段、三段共享24输出上限、首delta后取消；源码和原PCM
固定。新音频仅重复真实官方PCM并插入明确零PCM间隔，不生成假
转录；目标每段原中文精确、全局时间合法、无部分成功和清理完整。
不是人工时间精度或全部语言质量验收，通过后继续AL04和全部master。
