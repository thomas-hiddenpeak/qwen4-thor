# SSD expert offload 前提与近期工作对齐审计

2026-10-21。按用户要求，核对近期工作是否把SSD卸载变成主机缓存
方案。审计入口main 3fb1c3e，运行时比较166a535→3f2c704/当前入口；
默认二进制5a456351保持。方法为代码/git历史和已封存原始记录复核，
没有新模型加载、GPU测试、缓存操作或性能采样。

## 结论与更正

**没有发现近期实现把SSD expert offload改成全量CPU驻留，但后续
优先级需要纠正。** 启动可靠性交付成立；受限整机内存下的持续SSD
专家供给、物理读取归属与性能仍未验收。此前将跨层共享CPU缓存列为
直接后续优先方向，超出了现有证据能支持的判断，应重新排序；旧
机制研究决定不等于已经批准运行时部署。

已有报告明确保留了物理RAM/SSD未知项，没有发现将影子命中率、
64GiB memcg或热缓存启动直接签成严格SSD卸载通过；问题是后续
路线仍将共享主机缓存作为唯一优先候选，容易使研究目标偏移。
共享缓存只能保留为受总资源预算约束的供给复用候选，不能代替
SSD卸载目标，也没有被部署到默认程序。

## 代码与近期变更

| 项目 | 核对结果 |
|---|---|
| SSD权重来源 | `SafetensorsFile::Open`为O_RDONLY，ReadExpert通过buffered pread/preadv读取不可变checkpoint；GPU淘汰不写回SSD |
| GPU驻留 | C>0按各层实际C建槽；C=0全512驻留默认未变，默认程序健康通过不自动等于offload通过 |
| 主机L2/mirror | 已有每层pinned L2及淘汰mirror；166a535之后src/quant、src/io、model/decoder_layer及memory_budget无改动 |
| 共享CPU缓存 | 仅有离线路由/影子LRU筛选工具和提案，未改运行时每层独立缓存 |
| 启动资源计划 | 只按实际配置/张量形状提前预留device块，C128/C256不提升到512；host分配包装校验顺序后透传原bytes |
| MTP资源 | scratch/checkpoint提前到ready之前，不是全量主机专家缓存 |
| 新页缓存行为 | main/MTP形状预检以及auto/required的MTP预算预检保留已有文件页缓存，不在关闭metadata loader时建议整shard淘汰；off保留旧预检行为 |

最后一项是真实缓存行为变化，不能说本轮完全未改缓存政策。它没有
主动预读或锁住全部payload；元数据读取仍会访问header并可能发生
readahead。保留已有页缓存不等于保证其驻留，OS仍可回收。正式程序
不调用研究锁页helper，不要求完整热门准备。

主机L2始于35b9f7d（2026-10-01），mirror始于887a3fe（2026-10-01），
专家page-cache保留始于b9a973c（2026-10-02），均早于本轮启动修复。
它们与SSD后备并不天然冲突，但都必须计入Thor共享物理内存的约束。
不能因为是旧代码，就把占用忽略。

代码入口：

- [按需驻留分支](../src/model/decoder_layer.cu#L472)、
  [只读文件](../src/io/safetensors.cpp#L113)、
  [专家读取](../src/quant/moe_residency.cpp#L719)、
  [GPU淘汰到主机mirror](../src/quant/moe_residency.cpp#L1163)。
- [L2与mirror默认配置](../src/quant/moe_residency.cpp#L65)、
  [既有预算](../src/server/chat_server.cpp#L152)、
  [按C推导device计划](../src/runtime/startup_model_plan.cpp#L108)、
  [host分配透传](../src/runtime/startup_reservation.cpp#L252)。
- [main元数据页缓存](../src/runtime/startup_model_plan.cpp#L244)、
  [MTP元数据页缓存](../src/runtime/startup_mtp_plan.cpp#L108)、
  [MTP预算预检](../src/server/chat_server.cpp#L203)。

## 不能忽略的主机缓存成本

以下为当前形状、48层全部使用offload的源码payload计算，不是实测
RSS、物理RAM总量或峰值；尚未包含其他状态、元数据与文件页缓存。
原始staging包括权重、raw/merged/swizzled scale等，不能只按checkpoint
逻辑专家大小计算。实际默认C=0不创建这些offload缓存池。

| 配置 | L2 pinned bytes | mirror pinned bytes | 两者payload合计 |
|---|---:|---:|---:|
| 受测C256：16槽/层、mirror8 | 2,516,594,688 | 1,061,689,344 | 3,578,284,032（约3.58GB） |
| 启用offload但未覆盖缓存参数：128槽/层、mirror8 | 20,132,757,504 | 1,061,689,344 | 21,194,446,848（约21.19GB） |

因此“只是中转”不足以解释整个主机池：现有L2既是加载staging，也是
可复用权重缓存，mirror还可能持有重复副本。共享方案虽拟保持约2.52GB
的L2 payload，仍须核对metadata、mirror、文件缓存及系统余量，不能把
同L2 payload直接称为同整机内存预算。本审计不凭这些数字直接删除
缓存或改变默认槽数，修改策略仍需明确预算和相应验证。

## 已有验收依赖什么缓存条件

正式交付完整热门指192个主模型专家shard的67,987,791,872-byte向量，
不是整个模型的全部文件。锁页helper确已在模型创建前释放全部映射、
VmLck归零并退出；reaped=708687.487334242，server_before_start=
708687.852164607。**解锁不等于清除页缓存**，buffered读取仍可命中RAM。
helper在session cgroup，模型在独立scope，不能把外部热页自动视为
模型scope计费。模型运行时热页也会被回收，没有全部一直锁住的证据。

| 既有阶段 | 本次原始记录复核 | 能证明的范围 |
|---|---|---|
| C256完整热 | 起点67,987,791,872B；ready窗口36份完整扫描，专家缓存观察范围12,932,775,936–54,875,619,328B | 热缓存压力下启动/请求/退出可靠，不是冷SSD性能 |
| C256自然继承 | 起点payload 51,630,784,512B，无清理/补热/锁页 | 该自然缓存状态可运行，不是严格低RAM供给 |
| boundary-v2 C128 | 起点payload精确0；ready窗口95份完整扫描，专家缓存最高观测58,876,686,336B | 冷起点的短请求和回收通过，不能保证运行中缓存持续受限 |
| 默认冷/off冷 | 起点payload精确0 | 程序不以完整预热为启动前提；它们是全专家驻留配置 |

C128的专家缓存最大扫描位于`boundary-v2/resources/cache/000098.json`；
全部模型文件缓存最高观测63,918,297,088B在`000072.json`，两者不是
同一时刻，不能相加。scope memory.current最大观测65,105,383,424B
在`resources/cgroup.jsonl`第73行。每次逐文件扫描有时间窗口，也不是
原子物理峰；已消失scope的最后两条记录保留removed，不替换成零。

同一记录的MemTotal为131,920,637,952B（约122.86GiB），而scope的
memory.max=68,719,476,736B（64GiB）。后者不是把Thor变成64GiB设备，
CUDA驱动分配与外部页缓存的归属/重叠仍未完整闭合。不能把device、
pinned、RSS、页缓存各自峰值相加，或者用memory.max宣称整机上限。

旧五档C256基线同样没有host/cache限额；文件缓存端点约0.20GB→65.43GB。
软件读取约22.99TB，已知相邻样本的进程存储读取约112.77GB，后者还
包含启动、PLE等范围，不能据比值计算专家SSD命中率。详见
[原基线范围](OFFLOAD_FOUNDATION_BASELINE_2026-10-20.md)。

## 后续优先级修正

先明确并核验**受限内存下的SSD专家供给条件**，再选择优化策略：

1. 冻结目标内存预算、GPU驻留、pinned staging/mirror、文件缓存和系统
   余量的口径与可执行约束；不能自行把64GiB scope当作用户整机预算。
   若严格总量尚不可验证，明确保留并解决具体约束缺口，不以未知签PASS。
2. 定义冷起点与持续运行的缓存条件，在允许RAM不能容纳全部所需专家
   工作集的场景核验SSD供给。无需强制每次miss都物理读盘，但不能让
   完整热门夹具替代受限缓存条件。先冻结有界次数与停止条件。
3. 区分应用逻辑读取、主机处理/复制、实际存储I/O；专家、PLE、启动和
   请求分开归属。`nvme_mb`及L2 miss的旧名称仍表示逻辑checkpoint
   读取，不能直接当物理NVMe读取。不能归属的部分继续标unknown。
4. 在上述条件明确的范围内确定瓶颈，再选择共享主机缓存、读取合并、
   预取等候选。
   98.67%是软件加载次数占比而非耗时；68.6%–72.3%是影子局部性机会，
   均不能单独证明优先实现共享CPU缓存。性能优化按HTTP E2E优先、
   适用五档与保护项评估；不以启动可靠性验收替代性能结论。

资源核验采用一次事前冻结的有界清单，形成可执行约束和明确结论。
不要求无限逐页物理归属证明；未知项限制容量/收益主张，不否定当前
交付，也不阻止范围明确的离线研究。不能闭合的项在预算结束时保留，
不自动反复补测。

这是对后续路线的修正，不重做已通过的启动诊断，不自动追加采样，
不部署未实现的缓存候选。保留当前5a默认与19ea回退包。先前“可以
直接进入共享CPU缓存优化”的建议改为“可以进入SSD约束核验阶段”。

## 证据与本次交付

[本次只读复核记录](evidence/ssd-offload-alignment-20261021/audit.json)
绑定代码、原始采样及原部署审计身份；150个实际构建源文件与当前源码
一致。窗口事件与扫描输入离线核对原最终封存清单，两个阶段的局部
seal也绑定原清单，不仅计算当前文件SHA。原始启动证据及失败记录未改。
新审计脚本位于
`.q4t-work/ssd-offload-alignment-20261021/audit.py`；首版读取器未处理
scope退出后的removed记录而报错，修正后显式保留两条removed，未改变
原数据或重跑采样。文档当前入口已同步，运行时与默认二进制不变。
