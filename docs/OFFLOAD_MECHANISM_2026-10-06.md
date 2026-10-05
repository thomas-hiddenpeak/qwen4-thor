# Offload 缓存状态与阶段等待机制 — Goal 启动

更新：2026-10-06。承接d8482e4前驱诊断，在独立分支
`codex/offload-mechanism-20261006`推进。先复用旧runtime6b51693 /
binary8447d898的现成诊断能力，本阶段不改运行时代码或构建新binary。

## 本轮回答的问题

此前8193前驱后的k1、k3在A/C两模式都有影响，仅端到端时间和整窗口
字节不能定位来源。本轮把请求入口、prefill末态、decode前缀与最终状态
接到真实路由、阶段统计和PID存储读取，核对影响发生的阶段及其延续。
已有计时是累计/嵌套或并行，不把它们相加成独占关键路径。

## 已冻结的有限范围

[范围计划](evidence/offload-mechanism-20261006/scope-plan.json) SHA256：
`2fdd504c5bef648e882371b9be3fc50e2b98f2089576d5dc983d9997b367990a`。
8个新服务、32HTTP，每服务 `[P,1024,1024,1024]`，各输出256。
A/C为global/request/quiet=0/0/0与1/1/1，S前驱1024，L前驱8193。

| 服务顺序 | 条件 | 观测bundle |
|---|---|---|
| 1 | AS | off |
| 2 | AS | on |
| 3 | CS | on |
| 4 | CS | off |
| 5 | CL | off |
| 6 | CL | on |
| 7 | AL | on |
| 8 | AL | off |

on启用现有phase diagnostics、residency timing和router collector；
off全部关闭。每条件一对on/off，仅一个服务/cell，不估计稳定噪声、尾部
或统计显著性。配对顺序交替不等于所有组序/温度/机器状态已经平衡。
k1主端点；k2/k3分别次端点；conditioning不得混入匹配probe比较。
新旧样本不拼接为同一重复实验，不重复旧64常规采集。

固定16GiB host/cache、swap0、C256/L2配置16/mirror配置8、max_open200，
max_seq1/max_prefill8192/max_len262144，MTP、Phase D、chunk_order关闭，
精度/GEMM不动。各服务启动前定向冷准备，组内不清缓存。

trace额度128MiB为整服务预算，四个on服务总上限512MiB；运行前磁盘
可用空间至少2GiB。collector额外分配15MiB device及4×15MiB pinned，
还有D2D/D2H、写盘、采样和计时成本。on−off只能描述整个bundle与时序
差异；capture_ns也不是总观测成本，不从on结果直接扣除它来预测off速度。

## 合同与分析

逐组只审核身份、SSE、输出/长度/容量、路径、冷态和清理；on强制phase
及路由完整，无drop/overflow，不能接受collector静默关闭后的HTTP成功。
任一合同失败停止后续，保留失败，只定点修复；正常小差异仍完成固定32。
全部服务及子步骤终态后统一分析，不中途根据速度追加或删样本。

旧phase schema实际事件为prefill_begin、prefill_end_decode_begin、
decode_prefix(count=1/8/32)、inference_end。范围计划中的decode_forward_*
为概念端点，执行冻结须显式绑定该映射，不能改旧runtime事件。
三处完整快照含48层GPU slot/LRU/protected、L2 ID/tick、mirror ID/cursor。
核上请求结束到下请求开始的连续性、prefill前后变化、decode前缀和末段；
trace按request/forward/stage/position/48层连接真实top-k需求。

入口集合交集只能描述可能命中；worker的并发claim/commit与in-flight
会影响真实L2/mirror来源。现有全层聚合计数、阶段PID字节和嵌套计时
不承诺逐ID实际供给、独占SSD/H2D/host等待；未知项必须明确记录。
内存计数分列，54GB整体物理目标未证实仍未知。

## 任务与出口

1. 完成严格的新mechanism序列入口、phase/trace闭合审查与分析器；旧
   causality及完整性能接受合同不放宽。成组验证受影响工具，未改证据复用。
2. 绑定干净runner、旧runtime/build、工具依赖与精确命令后执行8服务32HTTP。
   新观测配置的输出合同由这批请求检查，旧质量11/C++15/数值3仅按原
   覆盖范围引用，不冒称其验证了新的观测配置或全部中间值。
3. 交付新的缓存/路由/阶段机制证据及观测限制；若仍缺具体动作依据，
   据这些新证据收束。只有明确缺口指向可操作假设时，才另冻一次有界
   观测扩展；至多一个优化候选，实施前另冻HTTP首测/数值/五档加容量/
   history/质量/资源验收。没有自动追加4K/8K/长矩阵或bench/profile。
4. 更新状态/日志并阶段提交推送，核MAIN七项修改和原binary保护、模型
   元数据与限域reference/服务清理。旧NO_GO和默认关闭保持。

当前已建立保护快照和独立工作树，工具接线/审计器实现中；未执行新
模型请求或测试，尚无本阶段性能或机制结论。原始材料存放于
`.q4t-work/offload-mechanism-20261006/`，不会覆盖旧失败和已交付证据。
