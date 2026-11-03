# SRV-02j1 CUDA申请额度核心与短验合同

本项是资源可靠性开发，不是性能优化。先实现可复用的额度账本与
分配适配器，再在02j2接入serve现有link wrapper、启动预留、CLI、
config/metrics和错误分类。**本批不替换默认ELF、不声称serve已启用
额度保护**；既有day计划保持，用户专门执行长验。

## 缺口与范围

SRV-02e真实计量已显示cudaMalloc的256MiB没有相当的memcg增量；
SRV-15全局MemAvailable轮询退场不等于物理限额。当前生产startup
reservation在物理预留后按计划交付，但运行期CUDA/pinned申请没有
统一的执行额度。现有GNU --wrap不覆盖外部DSO内部调用。

本批额度单位是**经适配器申请、尚未确认释放的请求字节**，统一计入
device、两类pinned、managed和async device；它不等于实际物理页。
驱动/库内部申请、普通host堆、文件缓存、池保留、外部进程均不在
这个账本中，不能把该上限称为整机RAM硬保护或容量offload。
CUDA池已释放的地址仍可能留在reserved池中，见[NVIDIA官方说明](https://developer.nvidia.com/blog/using-cuda-stream-ordered-memory-allocator-part-1/)。

核心要求：

- 有界固定表，不在申请/释放/完成路径动态分配元数据；并发申请先
  原子占额度和表项，拒绝发生在调用后端之前；减法比较避免加法溢出。
- 后端失败且未返回指针才能退还申请额度。未知所有权、重复live
  指针、成功却无指针及释放失败保留账目并锁存故障；不盲目重试释放。
- generation ticket防止旧票据修改复用表项；额度耗尽、表满、错误
  指针、错误释放类型等有独立原因，保留原后端错误。
- async free提交成功仅进入pending；只凭成功stream/device同步
  及同步前已发布的完成边界退还额度。后发提交不搭便车；stream
  token必须区分代次，event完成/stream销毁不自行当成全局屏障。
- 同地址的旧pending和新live可共存、分别计额；重复live为故障。
  CUDA async分配的同步cudaFree无隐式完成保证，本核心要求用
  async free加同步收尾，见[NVIDIA官方说明](https://developer.nvidia.com/blog/using-cuda-stream-ordered-memory-allocator-part-2/)。
- 核心/适配器都不拥有模型、不自动释放遗留未知资源、不新增后台
  线程/自动重启；其生命周期最终报告尚未闭合项。

## 执行前冻结的验收边界

先成组完成实现和CPU测试，再统一验证。CPU覆盖额度/表容量、边界
溢出、并发预占、失败回滚/未知所有权、代次票据、异步完成边界、
跨流、地址复用、同步失败、startup owner交付/回滚。真实CUDA仅
4个独立短探针：混合分配与超额恢复、跨流异步释放、计划预留失败
回滚、并发额度。每个最多15秒，总驱动最多90秒；失败即停，不
扩样。正常请求字节峰值不超过32MiB；不进行机器OOM或memcg击穿。

4探针在同一生产gpu_exclusive租约内运行，不加载模型、不发送HTTP，
不改系统服务或缓存。每个真实读写/结果/退出、来源及二进制身份、
guard观察和最终GPU/原锁释放均留存。若竞争/监控失败记ENV_ABORT。
轮询不排除短时干扰。CPU失败保留，具体修复只重验受影响项。

接受出口：零警告构建、全部直接合同和4探针通过、原件可核对、
默认pair及day未变、GPU释放；只接受核心限定范围。02j2另冻结
真实serve业务/工具闭环、发布/回退和更新长稳交接，不以本批小探针
替代HTTP质量或完整物理RAM保护。

## GPU执行前的释放发布时序修正

初轮25 CPU和4驱动检查通过、零警告。代码复核新增一个合法并发
时序：CUDA已接受释放，但host尚未提交其返回值，新分配已拿到
同一地址。原实现把releasing当作live重复，定向CPU复现为FAILED；
`publication-repro/`保留，不能把最初25项通过宣称覆盖这个时序。

修正允许releasing/pending地址代次共存、分别计额；原释放后续
失败则同地址全部代次锁存unknown，禁止再次释放。新增26号合同
分别覆盖成功发布和失败发布；`cpu-v2/`独立构建/验证新身份，初轮
源码和结果保持。4真实探针、32MiB/15秒/90秒、无模型/HTTP预算
均不扩展，GPU尚未执行。
