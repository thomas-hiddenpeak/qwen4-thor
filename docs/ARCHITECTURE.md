# ARCHITECTURE.md — 当前实现与设计边界

2026-10-17按代码静态核对，2026-10-21补正SSD卸载与统一RAM边界；
2026-11-08补充依赖最小化原则、前置接入边界及恢复包实际依赖。
本文描述当前模块，不把设计目标或旧实测
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
51,200,245,760字节sidecar不主动整表常驻，读取产生的文件页缓存仍占RAM。
不能将这个文件大小当作运行时
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
moe_resident_slots=0，全部专家驻留；非零显式启用配置的GPU槽位与SSD
后备。**SSD是唯一容量卸载目标**：host/device/managed共用DRAM，
放到CPU内存仍是RAM驻留。既有staging/L2/mirror只是占用该RAM的
中转或缓存，不构成独立容量层，也不代表已满足低RAM目标。该路径
的研究结论不能替代默认全驻留MTP验收。

当前专家miss经buffered pread/preadv读checkpoint，可能命中文件页
缓存；host合并/swizzle scale后H2D到device槽。驱逐不写回原模型。
PLE的io_uring也读host页池，二者均未接入cuFile/GDS。
数据流、容量和I/O术语遵循[SSD offload合同](SSD_OFFLOAD_CONTRACT.md)。

统一物理内存不等于零拷贝：CPU/GPU仍有各自分配、H2D/D2H和同步，
不能把重叠的驱动、进程、cgroup计数简单相加为物理总量。
`src/runtime/memory_budget.cpp`结合权重和状态/workspace容量估计，
以mem_fraction（默认0.90）决定可行配置；未指定max_len时自动推导，
部分请求可被CAPPED，实际容量写入服务启动日志。不承诺所有配置无OOM，
不覆盖其他进程、媒体解码及任意异常输入。

旧“84 GB权重/14.2 GB固定成本/96 GB峰值”等数字依赖当时配置，不是
当前所有模式的内存常量。模型配置上限262144也不是任意并发容量保证；
当前正式单流矩阵最大名义档204800，冻结max_len=208896。

## 启动设备资源计划

当前源码的文本服务在预算器确定有效容量后，从配置和真实张量形状
生成设备分配计划。`StartupReservation`先按字节数稳定降序预留，
再按模型原有逻辑顺序交付；pinned权重加载位于预留之后。ready之前
必须完成全部交付，服务资源先析构，预留所有者最后核对及回收。

`--startup-plan auto`为默认选择，`off`保留旧分配路径，`required`
拒绝不支持组合。auto只在计划开始前对明确不支持的功能选择旧路径；
元数据、分配或交付错误不会静默降级。media、router capture、FP8
shadow和MTP多槽等组合的支持范围由代码中的能力门确定。

正式路径不依赖研究逐API追踪或临时文件锁页；它解决受测启动顺序
的可靠性问题，不代表物理RAM硬上限、驱动页池根因修复或性能收益。
当前二进制交付状态与受测范围见[正式交付记录](STARTUP_DELIVERY_2026-10-21.md)。

## 状态与PD分离边界

ModelSequence维护position/history/stage及请求生命周期；模型的KV、
SSM、conv、PLE等状态按槽位管理。Paged KV使用页表间接寻址，page size
为16；具备页表和prefill/decode分开调用，不等于已经实现跨设备传输、
动态共享页池或跨设备会话迁移。

同进程完整会话前缀缓存已单独实现：`SessionStateRanges`枚举目标SSM/
卷积/PLE、有效KV/索引/RoPE及draft KV/索引/g/d0/pending/最后目标trunk；
`SessionCache`在私有SSD目录用匿名O_DIRECT文件保存，固定8项/24GiB，
单8MiB host staging并额外预留32MiB RAM预算。客户端显式session_id，仍传
完整历史并逐token校验；恢复使用目的槽页表，重建suffix索引，必要时修复
shifted draft最后一行。取消/失败不发布新快照，完整性失败reset后fresh
prefill；GPU错误fail closed。仅最后完整MTP边界，最多4个尾部输出重算。

缓存释放的是执行槽，预分配的GPU槽内存仍保留；不跨进程/重启，不支持
任意token中途抢占。受测配置、SDK序列化限制和收益/成本见
[会话缓存合同](SESSION_CACHE_PLAN_2026-10-27.md)与
[验收交付](SESSION_CACHE_RESULT_2026-10-27.md)。

多设备PD部署、独立prefill/decode池与跨设备状态交接仍是设计目标。
历史“PD-ready”“零风险”等措辞不能作为已交付或无状态污染的承诺。

## 外部依赖与可选前置接入

2026-11-08用户明确：项目设计应尽量减少外部依赖，兼容nginx前置，
但不能强制绑定。以下是设计约束与待完成的解耦边界，不表示当前
恢复封装已满足无nginx部署。

- 默认发行应独立提供原生API、鉴权、准入、健康及有界自恢复，
  无需安装、携带或启动nginx；systemd继续作为可选外层管理器。
- 外部网关通过标准HTTP接入q4t，按需承担TLS终止或统一接入策略。
  原生服务合同不依赖nginx特有模块、私有准入子请求或网关进程身份。
  网关退出只影响经过它的流量，不触发健康模型重启。
- 身份、配额、自检完成与否、恢复期准入仍由q4t守住；前置存在
  不应改变JSON/SSE、取消、工具响应及请求不自动重放的合同。
  公共调用者提供的内部身份头和转发头不能自动成为可信身份。
- 新增依赖须说明解决的问题、已有能力为何不足、启用范围及失效
  影响。必要的系统/推理依赖与可选接入、观测及开发工具分别登记；
  辅助依赖失能只降低相关能力，真实设备故障的保护继续保留。
- 复用现有原生HTTP实现，新增入口能力限定于q4t服务合同。可选TLS
  等集成保持明确的启用边界，不为去掉nginx重做通用Web服务器或密码库。

当前事实与解耦缺口：

| 环节 | 当前代码 | 解耦要求 |
|---|---|---|
| 原生推理入口 | `build/q4t serve`使用POSIX socket，不需要nginx；监听与模型在同一进程 | 保持直接接入；恢复入口应在模型重载时仍可明确返回503/Retry-After |
| 客户端鉴权 | `ClientAdmission`识别网关注入的客户端ID及内部凭据；RPM/TPM、公平准入已原生实现 | 增加客户端Bearer API Key鉴权并复用原生配额，拒绝伪造内部身份 |
| 恢复准入 | Controller自检及完整分钟配额等待完成后发布ready，由当前网关执行对外阻断 | 将准入条件落实到q4t原生入口；不能直接暴露已监听但尚未获准的模型端口 |
| 恢复封装 | `tools/serve_recovery/bundle.py`强制复制并校验nginx，`service.py`始终启动网关角色 | 默认包与启动路径移除强制网关依赖，外部前置作为显式可选集成 |

实现入口：[原生HTTP](../include/q4t/server/chat_server.h)、
[鉴权与配额](../src/server/client_policy.cpp)、
[恢复自检与开放入口](../tools/serve_recovery/controller.py)、
[当前封装](../tools/serve_recovery/bundle.py)。仅关闭Guardian的网关角色
不能补齐上述鉴权、准入及恢复期响应，不能据此宣称已经解耦。

后续顺序：先补原生鉴权与恢复准入，再形成独立于模型重载的轻量
原生服务入口并解除包/启动绑定，最后分别验证无nginx运行和可选
前置兼容。CPU覆盖伪造身份、未ready请求、额度保护、SSE中断及停止
边界；受影响的真实JSON/SSE、工具和恢复闭环另作有界验收。已有
Guardian、恢复预算与故障隔离证据按未变身份复用，不把旧封装验收
扩展为新入口已通过。具体实现与验收预算另行冻结。

## 构建与验证工具

CMake要求4.0+，host/device标准均为C++23，目标SM110a；本机工具链
CUDA13.3/g++-14。liburing缺失时PLE读取器被禁用，ICU缺失时tokenizer
不构建，因此完整推理需要相应开发依赖。原生`build/q4t serve`
不依赖Python；当前A/B恢复封装的控制器与网关工作进程使用Python，
并强制携带nginx，不能据原生入口推导整个恢复包没有这些依赖。
评估工具也使用Python；共享GPU验证lease及其局限见
[评估工具说明](../tools/evalscope/README.md)。

源码决定实际行为，模型config与固定上游实现用于核对数学语义，
数值合同与HTTP任务质量分别验收。历史与未接受研究见
[归档索引](ARCHIVED_RESEARCH.md)，不作为当前自动执行计划。
