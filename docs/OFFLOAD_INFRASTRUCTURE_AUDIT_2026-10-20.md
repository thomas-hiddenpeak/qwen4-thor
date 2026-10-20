# MoE expert SSD offload 基础设施审计

2026-10-20。审计源码321c0b4；默认3ae53f26，未改运行时或默认程序。
范围：真实SSD专家供给链路、可靠性、资源核算、策略与验收工具。
方法：代码静态审查、当前验收范围核对、固定历史归档复盘；新增GPU
采样0，未跑性能矩阵，未对缺口做故障复现。源码和历史文件摘要见
[身份清单](evidence/offload-infrastructure-audit-20261020/identity.json)。

当前注释（2026-10-21）：下文保留审计当时的实现、缺口和推进建议，
后续修复与交付以[STATUS](STATUS.md)为准，不重复执行旧计划。当前
[SSD offload合同](SSD_OFFLOAD_CONTRACT.md)明确：Thor的host/device
分配共享同一物理RAM，MoE专家容量后备只有SSD；有界主机staging/cache
计入统一RAM预算，不算容量卸载。后来的跨层共享主机缓存方案已搁置，
仅保留历史研究记录，不是正在推进的CPU offload或独立替代路线。

## 结论

**研究基础已存在，但不能认定SSD offload所需基础设施已经完整闭合。**
可以立即做历史复盘和冻结路由离线分析；真实策略实验前应先补专家
payload失败合同及相关验证工具接入。证明严格物理RAM上限、跨流预取、
MTP专家卸载等另有条件，不能由通用可靠性验收推导通过。

此前“Megatron基础已经够用”仅适用于结束通用借鉴、转入专项研究；
本次审计收紧其含义：它不代表现有MoEResidency所有错误出口已经安全，
也不代表旧offload实验工具已统一采用当前NVML监控。

这里的offload指从只读SSD checkpoint按需加载不可变专家权重；驱逐
GPU副本不需要写回SSD。现有mirror writeback是GPU→CPU pinned缓存，
不是SSD写回，也不是移出统一RAM的容量卸载。PLE的SSD读取与专家
权重读取是两条独立链路。

## 逐项完备性

| 项目 | 当前实现与证据 | 判定/边界 |
|---|---|---|
| 权重寻址与布局 | WeightLoader、SafetensorsFile；专家weights/SF/scalar描述符，邻接范围判定，scale merge/swizzle | 已有；可直接读取原模型，无需先重排或写模型目录 |
| SSD专家读取 | ReadExpert/ReadRun、pread/preadv、持久worker；短读/EINTR处理 | 已有buffered按需读取；不等于每次访问物理SSD，不是可取消异步I/O队列 |
| GPU专家缓存 | 每层C槽、LRU、同plan去重/保留、slot ID映射、每层容量/热列表 | 已有可研究基线；部分失败后的槽位有效性有缺口 |
| 统一RAM内主机缓存 | pinned L2、mirror ring、claim/event/in-flight、统计与预算 | 正常路径已有；不增加容量后备，部分复制失败后的保护未闭合 |
| 前向接入 | router IDs/weights D2H、prefill分块、gather/scatter、slot-mode decode | 已接入真实模型；D2H同步和分块成本在关键路径，不是免费接口 |
| 正常异步所有权 | 单model stream顺序，元数据快照、完成边界、loader共享所有权 | 最近已有验收；不能代替专家payload错误出口验收 |
| 路由与策略研究 | trace格式/校验、working set、离线replay、partition/chunk-order | 已有；不是通用任意策略模拟器，不预测实际SSD或延迟 |
| 预取与重叠 | worker读取与H2D、相邻prefill子块供给已有流水 | 不具备通用跨层/跨token预测预取、跨流依赖或统一I/O仲裁 |
| 资源预算 | C槽权重扣减，L2含staging，mirror单列，MTP/workspace预算 | 已有allocation预算；不能认证完整去重物理RAM上限 |
| I/O与内存观测 | residency counters、阶段timing、PID/cgroup/device计数、文件cache扫描 | 多视角已有；逻辑专家读取不能冒充物理专家SSD字节 |
| 故障与取消 | worker barrier、读失败传播、请求取消/槽位归还，错误GPU拒绝后续提交 | 通用基础已有；专家payload部分失败、阻塞I/O取消仍缺专项合同 |
| 数值验证 | expert bytes一致、cache hit/evict/mirror、真实驻留及partition数值工具 | 测试资产已有；当前全驻留HTTP通过不等于当前offload整体通过 |
| HTTP验收/发布 | E2E/质量/lifecycle/矩阵审计、身份绑定、回退包、GPU独占 | 已有；旧专项runner监控接入需更新，不能直接照搬旧协议 |
| MTP/多请求 | 通用模型执行及请求状态基础；MTP draft另走LoadMoeBf16 | 本轮不能认证其offload组合；draft专家未接MoEResidency |

## 优先缺口

### P0：专家payload部分失败后，所有权和缓存身份未闭合

位置：src/quant/moe_residency.cpp的CommitExpert约1100–1318行，
StageTask约1085–1097行，ReleaseMissClaim/ReleaseRingClaim约550–570行。

1. H2D由多次cudaMemcpyAsync组成，后续copy失败直接返回；in-flight
   标志仅在最后event record成功后设置。StageTask随后释放claim。
   因此早先已入队copy的源可能没有有效event保护；其他worker可能继续
   claim/复用缓冲区。外层等所有worker结束再同步，不能替代这段保护。
2. GPU槽位身份到全部H2D/event成功后才更新。覆盖旧槽位过程中失败，
   旧expert→slot映射可能仍有效，但内容可能已部分改变。需要明确使
   槽位失效、恢复旧内容或使整个residency不可再用，不能直接当旧命中。
3. mirror D2H的copy/event失败分支仅清除ring_claimed，然后继续后续
   H2D；未保证部分D2H完成，也未在该分支清除可能已被改写的旧mirror
   身份。即使最终GPU没有sticky错误，也不能据此认为旧payload有效。

这是代码可见的错误出口合同缺口，**尚未注入复现实际损坏**；不宣称
默认全驻留程序当前已受损。前轮MoE hot-upload修复的是offset和路由
元数据源及邻近D2H目标，未修改此处专家payload提交。
现有Q4T_RESIDENCY_FAIL_EXPERT在修改L2/slot之前返回，不能覆盖上述路径。

最低补齐：晚于第一份成功复制的copy失败、event record失败、mirror
部分写入、同步持续失败；检查源保护/目标有效性、其他worker继续执行、
下一请求复用与销毁。完成不确定时保留存储并拒绝复用，不能只清标志。
具体实现及真实故障测试属于后续修复，本次没有修改运行时。

### P1：offload专项入口未统一接入当前GPU监控合同

- tools/evalscope/run_offload_lifecycle.py:require_idle_device直接调用
  nvidia-smi；run_budget_experiment.py同样自行查询并判定空闲。
- monitor_memory.py:gpu_observation仍仅解析nvidia-smi进程显存。
- 当前gpu_exclusive.py默认NVML，但外层lease不会自动替换这些内部调用。

因此“外层NVML已可靠”不足以保证旧runner可运行或资源观测完整。
应复用同一lease，采用现有查询能力及明确unknown语义；进程枚举可用
也不代表每进程显存字段可用。不能把缺失读数记0，不能用jtop单一总量
替代进程归属或完整物理内存。当前代码事实是未统一，不是本轮已复现
这些命令必然失败。修复工具后先做host/fake-backend合同，再启动采样。

### P1：SSD与RAM实验合同必须先冻结

SafetensorsFile用O_RDONLY和buffered pread/preadv；model.cu对expert
shards保留page cache。Stats.nvme_read_bytes计的是应用读取量，可能
全由RAM页缓存提供，不是物理SSD计数。L2/mirror和GPU也可能重复持有
同一专家。Thor的CUDA与CPU内存来自共享物理资源，减少device allocation
不必然按等量减少系统RAM。

现有resource_metrics已收PID read_bytes、cgroup io.stat、块设备计数；
这些包含PLE/元数据等读取，不能直接标成专家SSD字节。memory_accounting
明确保留cuda/pinned/RSS重叠、外部页缓存charge归属未知，完整物理峰值
为INDETERMINATE。不能简单相加独立峰值，也不能以memory.max认证总RAM。

最低要求：记录GPU/L2/mirror配置及实际bytes、同时间窗口系统内存和
cache变化、服务与设备I/O、冷热状态与前驱请求历史；明确哪个是实测、
哪个是归属未知。若目标是严格总RAM上限，须先闭合可验证的核算/上界；
若仅做相对策略比较，可以保留未知项，但不得宣称满足严格容量目标。
不把O_DIRECT、io_uring或GDS列为无条件前置，是否更换I/O后端取决于
证据和对齐/错误合同，不能把机制名称当作收益证明。

### P2：预测预取、取消和组合能力属于后续专项

当前PlanResolve依据已经算出的路由，LoadPhase1阻塞等待worker完成；
prefill已有下一子块供给，不代表预测下一层专家。新增跨流或跨token
预取需要slot generation/有效期、在途pin、completion依赖、限深背压、
过期预测丢弃及miss回退；当前单流安全合同不能直接外推。

专家pread和worker等待没有请求取消token/截止时间，当前取消要等待
在途工作完成；HTTP超时不等于能中止内核阻塞I/O。若业务要求有界取消
延迟或SSD故障下快速停机，须增加相应机制和故障验证。PLE的io_uring
是独立page reader，不能据此认定专家读取已经使用同一异步队列。

MTP draft专家目前BF16常驻，不能由主模型NVFP4槽位卸载推导一并卸载。
首轮建议明确单请求、普通decode、主模型routed专家、单流、hot_protect
关闭的范围。当前hot-protect分块的D=C-H但fresh集合计全部ID，离线
replay也明确拒绝hot_protect；这是需另验证的限制，别作为首轮默认轴。
多请求/视觉/MTP组合不据通用HTTP成功自动获得offload资格。

## 历史证据与当前有效性

归档commit 7672b591644add0bbc05b8782da3127c80edc04d已只读提取到
.q4t-work/offload-infrastructure-audit-20261020，未恢复旧代码到main。

- RAM协议2026-10-02已记录pagecache/charge差异及54GB完整物理RAM
  目标未认证；这说明核算问题早已发现，不应再从零假设其不存在。
- mirror recycle报告记录17服务131 HTTP、六档只有两档全部过门，
  history也失败，结论NO_GO。否定的是该候选，不是所有SSD策略。
- decode supply报告有实际reserve/claim/publication观测，证明仅看
  缓存命中总量不足以归因。该observer在归档；当前main不据历史工具
  名称自动具备它。恢复前须检查依赖、成本和当前源码身份。
- 旧数据可用于机制假设、夹具和工具合同，不是当前3ae53f26的offload
  性能或质量验收。旧逐项零差值门不自动成为新任务协议；新性能任务
  按当前EVALUATION/PERFORMANCE_DECISION_PROTOCOL预冻结收益和保护项。

## 推荐的最短推进顺序与完成条件

1. **先补可靠性和工具接入**：只处理上述payload部分失败合同与旧runner
   监控接入，加入针对性故障检查；按bug修复有界验收，不顺带优化策略。
2. **冻结资源/缓存协议并建立当前offload基线**：固定配置和目标范围，
   HTTP E2E优先；复用同身份工具证据，记录质量、内存、I/O与冷热历史。
   与当前全驻留对照；基线可运行不等于性能或容量目标通过。
3. **再选一个策略**：已有trace/working-set/replay筛选，明确它降低的
   是物理SSD读取、CPU供给、H2D还是等待成本；实际性能主张须按规范
   适用五档评估及目标收益/保护项决定。额外长上下文与history按目标
   事前冻结，不事后挑选样本；不引入CUDA Graph前置或无界通用加固。

本审计完成，不启动修复、GPU采样或默认替换。下一步有明确且有限的
两项基础缺口，不需要继续漫无边际吸收Megatron特性。
