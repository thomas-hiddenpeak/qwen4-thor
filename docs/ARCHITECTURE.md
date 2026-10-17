# ARCHITECTURE.md — 当前实现与设计边界

2026-10-17按代码静态核对。本文描述当前模块，不把设计目标或旧实测
当作已交付功能；验收范围见[STATUS](STATUS.md)和[EVALUATION](EVALUATION.md)。
旧设计记录见[历史版本](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/7356f235e972b5e1be201923a4ba27b214b31e12/docs/ARCHITECTURE.md)。

## 运行入口与模块

- `src/main.cpp`提供version、probe、models、generate、serve及两类bench。
  generate调用独立单序列MTP路径；serve默认MTP k=3，可用--no-mtp回退。
  bench存在不代表它可用于性能接受，后者使用真实HTTP E2E。
- `src/server/`实现自有HTTP socket服务、请求解析、chat模板、资源预算
  接入和调度；默认数字IPv4 127.0.0.1:8000。只实现规定范围的OpenAI
  风格接口，当前生成采用greedy，不支持完整OpenAI参数语义。
- `chat_scheduler.cpp`提供批处理/多序列调度与MTP批量步骤。实现存在
  不代表所有并发配置已获验收；当前冻结矩阵是纯文本单流k=3。
- `src/model/`实现48层主模型：36层linear attention、12层full attention，
  含QSA、HyperConnection、MoE和第2层PLE（0-indexed layer 1）。
- `src/mtp/`实现一层draft与T>1 verifier路径；旧sequential verifier不在
  当前源码中。N=4 ULP近tie合同不等于所有状态或跨路径逐位相同。
- `src/quant/`实现NVFP4专家权重/激活原语、GEMM/GEMV和可选专家驻留。
  不是整个模型都为W4A4：其余权重/激活与状态包含BF16、FP32。
- `src/vision/`实现ViT与图像/视频预处理；serve默认拒绝媒体，需显式
  --allow-media，当前文本验收不覆盖此实验路径。
- `src/ple/`实现ngram hash、页去重读取和FP8转换；`src/io/`负责权重/
  JSON配置，`src/text/`负责基于ICU的BPE tokenizer。

## PLE实际数据流

每token计算16个row ID，每行160字节，因此FP8查表数据为2560字节/token。
51,200,245,760字节sidecar不整表驻留，不能将这个文件大小当作运行时
工作内存，也不能把全部约47.68 GiB都描述为无额外成本的节省。

```text
主模型到达PLE层
  → host计算row IDs
  → PlePageReader::Gather：4 KiB页拆分/去重，io_uring分波提交并等待完成
  → scatter到pinned host staging
  → 在调用者CUDA stream上H2D到GPU FP8 scratch
  → 同一stream执行FP8→BF16、scale及PLE层融合
  → 后续主模型层
```

`PleEmbedding::GatherRows`返回前完成host读取和scatter；GPU上传/转换可
异步完成，但再次覆盖staging前须等待上一轮upload_complete事件。
错误退出会排空已提交工作，事件失败后embedding不可继续复用。
这些所有权边界是2026-10-16 PLE修复的一部分，不能因同一CUDA stream
就省略CPU覆盖源内存前的等待。

当前实现没有专属ConvertStream对象，也不等同于上游“提前一层发起
prefetch”的执行器。I/O可能与此前已入队的GPU工作重叠，实际程度受
同步/调度影响，不保证SSD延迟完全被掩盖。

主要容量来自`PleEmbedding::Create`与page_reader配置：

| 缓冲 | 容量关系 |
|---|---|
| pinned FP8 staging | capacity_tokens × 16 × 160字节 |
| GPU FP8 scratch | 与staging同大小 |
| pinned row IDs | capacity_tokens × 16 × sizeof(int64_t) |
| page pool | 默认32 MiB；尝试注册io_uring fixed buffer，失败可回退普通buffer读取 |
| 其他 | reader元数据、ring、模型持有的BF16输出等另计 |

## 专家驻留与内存预算

PLE sidecar流式读取和MoE专家按需驻留是两个机制。serve默认
moe_resident_slots=0，全部专家驻留；非零显式启用有限GPU槽位、主机
缓存与按需NVMe读取。该路径的研究结论不能替代默认全驻留MTP验收。

统一物理内存不等于零拷贝：CPU/GPU仍有各自分配、H2D/D2H和同步，
不能把重叠的驱动、进程、cgroup计数简单相加为物理总量。
`src/runtime/memory_budget.cpp`结合权重和状态/workspace容量估计，
以mem_fraction（默认0.90）决定可行配置；未指定max_len时自动推导，
部分请求可被CAPPED，实际容量写入服务启动日志。不承诺所有配置无OOM，
不覆盖其他进程、媒体解码及任意异常输入。

旧“84 GB权重/14.2 GB固定成本/96 GB峰值”等数字依赖当时配置，不是
当前所有模式的内存常量。模型配置上限262144也不是任意并发容量保证；
当前正式单流矩阵最大名义档204800，冻结max_len=208896。

## 状态与PD分离边界

ModelSequence维护position/history/stage及请求生命周期；模型的KV、
SSM、conv、PLE等状态按槽位管理。Paged KV使用页表间接寻址，page size
为16；具备页表和prefill/decode分开调用，不等于已经实现跨设备传输、
动态共享页池或完整会话迁移。

多设备PD部署、独立prefill/decode池与跨设备状态交接仍是设计目标。
历史“PD-ready”“零风险”等措辞不能作为已交付或无状态污染的承诺。

## 构建与验证工具

CMake要求4.0+，host/device标准均为C++23，目标SM110a；本机工具链
CUDA13.3/g++-14。liburing缺失时PLE读取器被禁用，ICU缺失时tokenizer
不构建，因此完整推理需要相应开发依赖。推理服务不依赖Python，
评估工具使用Python；共享GPU验证lease及其局限见
[评估工具说明](../tools/evalscope/README.md)。

源码决定实际行为，模型config与固定上游实现用于核对数学语义，
数值合同与HTTP任务质量分别验收。历史与未接受研究见
[归档索引](ARCHIVED_RESEARCH.md)，不作为当前自动执行计划。
