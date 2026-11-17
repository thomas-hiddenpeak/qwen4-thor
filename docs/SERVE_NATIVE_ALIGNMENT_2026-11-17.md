# 原生音频时间戳与字幕实施

本项补齐A10转录的真实时间戳和字幕，以及A09/A12的相应服务组合。
rev276已交付文字转录，`verbose_json`、SRT/VTT、实际对齐仍是
待实现能力，不能以统一拒绝或均分时间替代。说话人/VAD、Chat音频、
语音输出及Realtime继续保留在音频总计划，不因本子项关闭。

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

当前PR44固定在转录交付的最终head，等待检查及合并；本项为后续
工作，暂不混入PR44源码和验收。全部Serve总目标仍active。

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
