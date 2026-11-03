# SRV-02e 启动可用内存与Thor计量边界

这是业务可靠性推进，不是性能优化。前置默认c597adc3/a350c978；
工业化总目标保持完整。现有容量估算按mem_fraction × MemTotal选择
容量，运行期MemAvailable准入在模型已加载后生效；二者都不能证明
启动时还有足够余量。只使用物理总量作估算上限不能替代可用量检查。

## 开发范围

新增--startup-memory-headroom-mib（默认2048MiB，范围0..1048576）：
元数据/词表/缓存打开之前按余量检查；已有预算完成、进入startup
reservation或legacy模型加载之前，重新要求MemAvailable不少于
estimated_total+headroom；发布ready/启动HTTP监听之前再次按余量检查。
三次严格新读数，未知/不一致/缺估算/加法溢出拒绝并保留固定原因。
不因可用量变化默默另选容量，不把拒绝变成另一条自动重试加载路径。

0显式关闭该检查。--no-budget继续绕过容量预算，并明确绕过依赖它的
启动检查；配置以budget_disabled显示，运行期内存准入仍独立。
启动检查是快照，不阻止竞争进程在读数后分配，也不保证未计量项、
CUDA分配、文件页缓存或媒体解码峰值。它没有新增CPU容量offload路线。

GET /v1/config公开策略、CLI/default来源和三个实际快照，离线配置
序列化尚未执行检查时为null，未知读数不得伪装成可用0。capabilities
声明检查点和明确绕过边界。修正ServerOptions中“reserves”误导注释。
固定protocol身份、完整源码清单与输入防遗漏清单同步更新。

## 有界验收计划（随源码提交冻结）

- 一次成组CPU：严格读数复用，新增精确边界、总量不替代可用量、
  未知/不一致、真实0、溢出/缺估算、禁用不读、三检查点新读数、CLI
  事务性与配置合同；必需host清单纳入新增8项。零警告构建。
- 锁内原生HTTP/准入复用24项；无模型的启动拒绝2次：极高余量在
  metadata前拒绝；64GiB余量在完整估算后、任何大模型分配之前拒绝。
  核对实际日志、退出、无HTTP监听/owned资源遗留，不制造整机OOM。
- 一次正常候选完整head/MTP、seq1/8192：三个实际读数/配置/能力与
  预算一致；普通JSON/SSE、strict及真实MODEL工具读取/引用回填。
  资源/worker/slot回收及实际SDK质量分别核对。
- 独立四种256MiB分配观测：匿名host、cudaMalloc、cudaMallocHost、
  cudaMallocManaged；每个独立scope/进程，提交真实页/设备写入，
  记录baseline、实际持有、释放后的进程/cgroup/全局/CUDA口径。
  所有测量在同一完整GPU lease内，模型已停止后依次执行。
- 通过后保留旧pair、交付默认和有界安装健康，所有原始失败保留。
  具体次数/时间/观察上限在驱动和plan中冻结，禁止自动重试或扩样。

计量不是极限分配或性能bench；固定小样本不能支持精确归因的全机
字节和物理硬限额结论。若memcg未覆盖某类CUDA分配，明确留下缺口，
不因已有MemoryMax就宣称工业化资源边界完成。观测失败/竞争记ENV_ABORT，
只停止自有进程；不修改模型、reference、其他服务或机器全局缓存。

## 外部依据与本机证据的区分

[NVIDIA CUDA 13.3 Tegra说明](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-for-tegra-appnote/index.html#memory-management)
说明host/device/unified分配共用SoC DRAM，cudaMemGetInfo是可变化的
分配视图；本项目不将它与RSS或memcg简单相加。
[Linux cgroup v2文档](https://docs.kernel.org/admin-guide/cgroup-v2.html)
定义memory.current/stat/max的控制器口径；本机CUDA分配如何被计费
仍由实际四种分配观测确认，不从文档存在推导驱动覆盖。

## 执行约束与CPU门禁

一次完整lease最多1800秒（清理预留90秒，1500秒之后不得开始安装），
固定3次启动尝试，其中2次启动拒绝、1次正常模型；恰好5次生成：
普通JSON/SSE、strict JSON、SDK真实资料工具调用及引用答案。另4次
独立256MiB探针，最多同时1份分配；128次HTTP控制/发现观察上限，
阶段内原生HTTP24项另列。计量各检查点固定2次观测、间隔250ms，
managed另含CPU触页后、GPU触页前的检查点；无极限压力或自动扩样。
安装健康使用实际默认ELF的CLI/隔离worker握手，HTTP复用同SHA候选；
不另起模型，也不称安装路径的独立HTTP重验。所有产物按身份保留。

冻结前CPU已通过：OFF234/ON243、必需host200、后端9、驱动6、原生
配置15及启动配置10；引擎和256MiB探针零警告。输入防遗漏清单10个
源码锚点已按实际语义复核，字段/路由/嵌套白名单保持。运行时200个
源码身份固定为构建时清单；数值/模板/分词器/grammar保持原证据。
真实GPU/HTTP/计量结果尚待执行，不把CPU通过作为业务接受。
