# SSD offload后续策略筛选

状态：机制筛选候选，等待当前五档完整基线核验；未实现或接受优化。
本目标只选一个后续研究方向，不自动启动策略优化或追加GPU采样。

## 优先候选：同L2 payload预算的跨层共享CPU专家缓存

当前L2是每层独立pinned缓存。C256基线固定48层各16槽，worker=16；
主模型按层前向，prefill内分块可重复访问同层专家。候选以(layer, expert)
为身份共享CPU缓存，保留源claim/event完成约束，优先研究当前层工作集
复用。先固定GPU C256、mirror8、执行映射与输入，不把加预算混入收益。

当前源码MoEResidencyStagingBytes与SfBufferSize给出hs=2560、moe_is=640：

| payload项 | 字节 |
|---|---:|
| 每个L2 staging槽（含raw SF/merged SF/转换后数据） | 3,276,816 |
| 当前48×16个L2槽 | 2,516,594,688 |
| 单层全部512专家的L2 staging | 1,677,729,792 |
| 当前48×8个mirror槽 | 1,061,689,344 |

这是源码推导的payload配置量，不是物理RAM峰。不能把2,764,824字节的
checkpoint逻辑专家读取量、2,764,816字节的mirror payload和L2 staging
混用。共享池还需要键、淘汰、claim和错误状态元数据；未来必须单列这些
实际开销，不能只保持payload常数就宣称整个资源预算完全相同。

相同768个staging槽的总量足以容纳一层512专家，为集中利用原本分散的
缓存提供机制空间；它不证明实际访问序列、并发claim、淘汰和mirror交互
下的命中率。主要目标是减少软件读取、CPU scale处理与拷贝。CPU缓存命中
仍需要H2D，不能从命中率直接预测速度，更不能将逻辑读取减少称为SSD读取
减少。主机缓存变化也可能改变worker时序，需检查实际GPU/mirror行为。

## 为什么优先于重复旧试验

- [旧工作集回放](OFFLOAD_REPLAY_2026-10-02.md)记录主45K首发prefill约
  1.122–1.134TB逻辑读，其中约0.802–0.814TB来自同层同forward再读。
  这是旧身份、明确排程模型下的机制证据，不是当前版本物理SSD测量。
- [非均匀GPU容量试验](MOE_RESIDENCY_R3_DECISION_2026-10-01.md)已研究
  增C及按层分配；不能将其当成尚未尝试的新方向或直接移植旧性能门。
- 历史mirror recycle候选未通过其性能/history要求；decode supply
  观察也表明同plan竞争不足以解释主要缺口，暂不优先继续小幅mirror微调。
  来源身份见[基础审计](OFFLOAD_INFRASTRUCTURE_AUDIT_2026-10-20.md)。

本次机制字节核对及来源SHA保存在
.q4t-work/offload-foundation-20261020/strategy-screen/mechanism-screen.json。
当前基线已封存并复核：15条offload请求L2 hit为0；prefill miss占
全部软件加载98.6748%（该矩阵长度加权）。约22.99TB逻辑读取与
约112.77GB进程存储计数属于不同范围，不能换算专家SSD命中率。
这些观察支持优先减少CPU专家供给重复工作，尚未证明共享缓存命中率
或提速。最终保留本候选；详见[基线结果](OFFLOAD_FOUNDATION_BASELINE_2026-10-20.md)。

## 下一阶段边界

先固定校准/留出数据与同字节预算，离线回放只筛选共享缓存的局部性机会，
保留跨请求历史与不同合法worker排程；不把模拟当实际Linux页缓存预测。
只有可重复的机制收益支持实现时，再设计共享claim、错误保留与资源准入
合同。真实优化先HTTP E2E，再按适用五档与目标收益/保护项评估；精度、
数值、生命周期及实际预算都必须通过。当前不引入CUDA Graph、跨流预取、
MTP draft offload或任意策略自动搜索。
