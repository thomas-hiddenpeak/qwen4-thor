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
