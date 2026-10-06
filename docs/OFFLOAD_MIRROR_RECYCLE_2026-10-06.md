# GPU-covered mirror 回收评估：NO_GO（2026-10-07）

本轮唯一候选完成冻结的 **17 个服务、131 个 HTTP 请求**，逐组输出、usage、
身份与清理合同通过，独立结果核算通过；**性能结论为 NO_GO**。六档矩阵只有
8192、45056 通过全部门，1024、4096、204800、261887 各有失败；history 两个
方向块的 14 个位置全部至少失败一项，其中 11 项最低 decode 下降。停止继续
推进这个候选，`Q4T_MOE_MIRROR_GPU_RECYCLE` 保持默认 `0`，本轮不加入第二策略。
这是可交付的否定结果，不具备默认启用资格，也不把小幅观测差异描述为统计显著。

## 候选与冻结身份

承接 `3a838473`，交付分支 `codex/offload-mirror-recycle-20261006`。
本机 Thor Release 构建通过，编译零警告。

候选仅在显式 single-decode、T=1、无 request 上下文时，优先复用仍被 GPU
覆盖的可用 mirror 槽；PlanResolve 完成保留后构建不可变覆盖集合，排除本 plan
全部 victims/missing。无合格槽时走原 cursor 回退，既有 claim/event/in-flight
约束保留。开关只接受 0/1；OFF 不建立覆盖集合或增加 ring/event 扫描；模式分支和计数存储仍存在，
不宣称绝对零开销。没有 GEMM 精度、prefill 算法、
MTP 或其他 offload 策略变更。

| 冻结项 | 值 |
|---|---|
| 运行源码 commit | `1efc524fe379f1eca9ceefed690327ae77236c7f` |
| q4t 二进制 SHA256 | `6a1e3321499a45359068610ee5865a8b7c97930bb6eed6f0065acfa0c6178762` |
| execution-plan SHA256 | `5e9f1627c85cd21a7467caed538fb80bc88cedb534f59fed6c094a922be8537e` |
| 主配置 | GPU expert C=256，L2=16，mirror K=8，max-open=200，单流 |
| 容量配置 | max-prefill=8192，max-len=262144；MTP关闭 |
| 其他轴 | partition/request-partition/quiet/chunk-order 均为0；observer、route trace、phase diagnostics关闭 |

## 覆盖与接受规则

首项测试为固定 HTTP 质量 11 题，随后主机合同 24 项（选择器14、协议10）及
单个真实权重数值合同通过。性能覆盖 history 4×21=84 请求与六档矩阵
6×2×3=36 请求，共 131 HTTP；未加有利重采样，没有丢弃轮次、反转或失败。

history 按 h01 OFF、h02 ON、h03 ON、h04 OFF 的 ABBA 顺序执行，每个服务连续
三轮固定七位置 `[16385,8192,8193,1024,45056,4096,8192]`。两块分别比较
h01 OFF/h02 ON 与 h04 OFF/h03 ON。矩阵按输入档交替 OFF→ON、ON→OFF，
始终以 OFF 为基线；每臂保留全部三轮。对每个位置/档分别要求：
首轮 ON TTFT ≤ OFF；后两轮最大 ON TTFT ≤ OFF；三轮最低 ON decode ≥ OFF。
没有事后 epsilon。history NO_GO 始终保留否决作用，后续矩阵只完成预冻覆盖。

下表 Δ 均为 `(ON/OFF−1)×100%`：TTFT 正数更慢，decode 正数更快。数值显示到
六位小数，门判断使用原始未舍入值；全部原始精度值见相应 compact decision。
独立服务是重复单位，位置与三轮不是独立服务重复；ABBA/交替顺序没有消除所有漂移。

## 六档 HTTP E2E

| 输入 token | 首轮 TTFT 秒：OFF → ON（Δ） | 后两轮最大 TTFT 秒：OFF → ON（Δ） | 三轮最低 decode tok/s：OFF → ON（Δ） | 结果 |
|---:|---|---|---|---|
| 1024 | 11.821605 → 11.834804（+0.111650%） | 9.963748 → 10.003836（+0.402338%） | 6.955698 → 6.899380（-0.809679%） | FAIL：首轮TTFT、后两轮最大TTFT、三轮最低decode |
| 4096 | 25.457492 → 25.634225（+0.694226%） | 25.990408 → 26.104875（+0.440420%） | 6.478986 → 6.495125（+0.249107%） | FAIL：首轮TTFT、后两轮最大TTFT |
| 8192 | 37.518605 → 37.308789（-0.559232%） | 38.347353 → 38.203108（-0.376152%） | 6.243914 → 6.246118（+0.035299%） | PASS |
| 45056 | 219.842121 → 219.121571（-0.327758%） | 222.201061 → 222.104277（-0.043557%） | 7.147919 → 7.149498（+0.022094%） | PASS |
| 204800 | 1107.496585 → 1112.231783（+0.427559%） | 1114.562322 → 1112.553941（-0.180195%） | 7.559318 → 7.544574（-0.195044%） | FAIL：首轮TTFT、三轮最低decode |
| 261887 | 1439.829971 → 1436.356378（-0.241250%） | 1436.446957 → 1439.509002（+0.213168%） | 6.109843 → 6.119806（+0.163050%） | FAIL：后两轮最大TTFT |

8192、45056 的最低 decode 分别只增加 0.035299%、0.022094%，这里仅记录其
通过原定观测门，不据此宣称稳定加速。204800 的首轮 TTFT 与最低 decode 失败；
261887 虽首轮 TTFT 和最低 decode 通过，后两轮最大 TTFT 仍失败。不得用挑选
通过档或只看均值替换全范围接受要求。HTTP TTFT 不是独立 prefill 内核计时，
这些变化不能用来归因 prefill 提升或纯 mirror 策略开销。

## history 两方向块

块一：h01 OFF → h02 ON，七个位置均 FAIL。

| 位置 | 输入 token | 首轮 TTFT Δ | 后两轮最大 TTFT Δ | 三轮最低 decode Δ | 失败门 |
|---:|---:|---:|---:|---:|---|
| 1 | 16385 | -0.023763% | +0.467486% | -0.901573% | 后两轮最大TTFT、三轮最低decode |
| 2 | 8192 | +0.117991% | -0.089041% | -0.786466% | 首轮TTFT、三轮最低decode |
| 3 | 8193 | +0.168262% | +0.241515% | -1.609069% | 首轮TTFT、后两轮最大TTFT、三轮最低decode |
| 4 | 1024 | -0.734424% | +0.477412% | -1.340955% | 后两轮最大TTFT、三轮最低decode |
| 5 | 45056 | -0.035472% | -1.618772% | -0.521908% | 三轮最低decode |
| 6 | 4096 | -0.742159% | +2.886937% | -0.580162% | 后两轮最大TTFT、三轮最低decode |
| 7 | 8192 | -0.776874% | +0.460816% | -0.712849% | 后两轮最大TTFT、三轮最低decode |

块二：服务顺序 h03 ON → h04 OFF；计算仍为 ON 相对 OFF，七个位置均 FAIL。

| 位置 | 输入 token | 首轮 TTFT Δ | 后两轮最大 TTFT Δ | 三轮最低 decode Δ | 失败门 |
|---:|---:|---:|---:|---:|---|
| 1 | 16385 | -0.100480% | +0.555729% | -0.663008% | 后两轮最大TTFT、三轮最低decode |
| 2 | 8192 | +0.326514% | -0.041886% | -0.475597% | 首轮TTFT、三轮最低decode |
| 3 | 8193 | +0.300626% | +0.218073% | +0.580355% | 首轮TTFT、后两轮最大TTFT |
| 4 | 1024 | +0.468782% | -0.287734% | -0.126234% | 首轮TTFT、三轮最低decode |
| 5 | 45056 | +0.034220% | -4.929482% | +0.356704% | 首轮TTFT |
| 6 | 4096 | +0.003215% | -0.121916% | +0.410846% | 首轮TTFT |
| 7 | 8192 | -0.342883% | +0.124039% | -0.427861% | 后两轮最大TTFT、三轮最低decode |

保留方向反转：8193 的最低 decode Δ 从 −1.609069% 变为 +0.580355%；45056
从 −0.521908% 变为 +0.356704%；4096 从 −0.580162% 变为 +0.410846%。4096
后两轮最大 TTFT 从 +2.886937% 变为 −0.121916%。这些反转不能删去或合并成
候选稳定改善的结论。两个方向块共 14 个位置均至少一门失败，11 个 decode 门失败。

## 输出、数值与策略是否实际生效

固定 HTTP 质量 11 题及全部逐组合同通过；四个 history 服务的 21 项完整输出
序列、prompt SHA、实际输入/输出 token 与 finish 比较一致。矩阵 OFF/ON 逐档
逐轮的对应输出合同通过。固定题集通过不等于任意任务语义质量改善。

真实权重合同限定 layer2、T1、GPU槽16、L2槽8、mirror槽8、单worker、inline关闭。
受控 router 下，完整 BF16 输出及 router IDs/weights 对全驻留参考 bit-exact，
OFF/ON routed FP32 输出 bit-exact；对全驻留 FP32 的差异仍按原合同报告。
夹具覆盖 changed=1 的真实选槽、写回后再读、保留 sole mirror、排除 plan victim，
以及 unknown/prefill singleton 不触发。比较的是所选路由的完整输出及槽身份/recency，
不是每个驻留槽全部 raw 字节，也不是所有层、任意多worker调度或 CUDA 故障证明。

ON 累计 `changed=16149`，`preferred=19446`，
`published=19446`，`unavailable=6`；所有 OFF
七项计数为零。`changed` 是真实改变回收目标的次数，不能用 `preferred` 替代。
实际干预门通过仅证明选择发生变化，不证明少读多少 NVMe 字节或获得速度收益。
日志声明的 phase 不是逐事件 phase 证明；适用范围还依赖源码门与直接合同。

## 资源审计与保留的缺失

资源审计完成，身份与 summary 对照合同通过，覆盖 **28,932 个样本、121 个
client I/O 包络**：质量11请求共享1包络，history84及matrix36各每请求一包络。
17个服务的 PSI 均为 UNKNOWN，GPU scheduled-skip 共26,259次，不能补作零压力
或零GPU占用。资源审计完成不等于资源接受或物理54GB证明。

质量服务无16GiB限制；16个性能服务的实际 memory.max 为17,179,869,184字节。
10个服务的采样 memory.current 峰值超出该值，最大超 **262,144字节**；12个服务
的 memory.peak charge峰值超出，最大超 **860,160字节**。两类原始超额分别保留，
没有裁零、混合峰值或用容差改写。OOM及swap的已观测计数/峰值为0，不替代缺失PSI。
这些是服务生命周期观测，不是每请求内存峰值。

PID `read_bytes`、逻辑 `rchar`、cgroup I/O 与 partition/device计数分开，不把
设备后台流量归为专家读盘，也不相加父盘与分区。NVIDIA、cgroup、进程和文件缓存
口径相互重叠且峰值不同步，**整体物理RAM≤54GB仍为 INDETERMINATE**。不据此
计算纯SSD等待占比、prefill因果或候选默认启用资格。

## 存储前置失败、修复与恢复定位

本轮空间处理记录独立于推理收益：453候选内做428次相同字节硬链接共享，并清理
25个明确可重建文件；首轮压缩1件，只比原1GiB门多364,544字节。随后m062在
`run_stage.py:47`前置磁盘断言失败，controller/service/HTTP均未开始，HTTP=0。
失败后补记的free快照为1,071,853,568字节，断言瞬时字节值未记录；
首次失败原样保留，不算额外HTTP尝试。

独立余量修复只取原压缩清单剩余245项；全部完成解压SHA/长度复验与恢复映射后
移除新原件。结束free=1,103,863,808，高出原门30,121,984字节（28.73MiB），
但距1GiB+32MiB存储目标仍差3,432,448字节（3.27MiB）。明确保留
`startup_space_gate_met=true`、`storage_target_met=false`；清单耗尽后未扩范围，
runner门和HTTP计划未改。随后原门通过，最后m062三请求完成，合计仍131 HTTP。

两个durable map分别为 `storage-compression/compression.json`（1项，SHA
`967bf70cb1ed901180a0119a5b722197e3835815e1e5a89602e35cd6fa80aa21`）与
`storage-reserve-repair/compression.json`（245项，SHA
`e7d2cfd61c3c2532ad8a5b41543069f1481477b8cbf608a8132c0ade3af4cd10`）。它们记录原路径、
原SHA/长度与gzip位置/SHA；gzip只在本地各自payload目录保存。原件已从原路径移除，
恢复需流式解压到独立临时文件，核对原SHA/长度、fsync后原子替换，并准备额外空间。
不能保证原inode/mtime/mode；后续依赖的日志、responses、边界与资源流没有压缩。

本轮保留数值夹具RAII生命周期、资源completed将case误当HTTP、交付工具等执行前
静态发现；gzip helper另修复恢复根目录父项未fsync的问题。补充失败上下文的写入
曾错误假设32MiB目标已达，记录为 `independent-review-context-first-failure.json`，
修正为准确携带两个布尔值，未改原独审或重复测试。不能把最终合同通过写成全部首过。
完整失败上下文见 `independent-review-failure-context.json`。归档前另修复两份
storage extension记录被过滤的遗漏，发现与增量独审一并保留。

三步记录的净分配回收累计105,537,536字节（100.65MiB），与全局free时点差值
分开，不作为推理内存改善。存储原记录、首失败、压缩与硬链接元数据均留档。

## 交付结论与证据

逐组/直接合同及独立结果审查通过；性能 **NO_GO**，history否决保留，
默认启用不允许，物理54GB未知。冻结的全部范围已走完，停止该mirror回收候选，
保留默认关闭的可回退实验分支与否定证据，本阶段不追加策略或有利复跑。
最终保护通过：MAIN原七项修改、完整diff与主binary保持；旧三工作树HEAD/status
保持，52个owned退出记录及17个服务unit已闭合。模型只核228项元数据和
config/index摘要，未审计权重payload；元数据一致不证明权重字节相同或排除
瞬时写入。reference范围为tracked Git状态。

主清单所列compact归档完成235文件、5,754,211字节；归档只证明交付完整性，不改变
NO_GO。归档后的文档提交与远端分支身份另做交付复核，不重跑模型。

证据以 `docs/evidence/offload-mirror-recycle-20261006/` 为最终compact归档目标：
`performance-decision.json`、`history-decision.json`、质量group摘要、host/numerical
记录、`resource-summary.json`、`independent-result-review.json`、失败上下文及
storage映射。完整本地R为 `/home/rm01/models/dev/qwen4-thor/.q4t-work/offload-mirror-recycle-20261006`。本报告只从compact记录提炼；没有为报告重读
payload、raw日志、raw资源或旧证据，也没有新增模型、HTTP或测试。

直接证据：[性能结论](evidence/offload-mirror-recycle-20261006/performance-decision.json)、[独立结果复核](evidence/offload-mirror-recycle-20261006/independent-result-review.json)、[资源摘要](evidence/offload-mirror-recycle-20261006/resource-summary.json)、[最终保护](evidence/offload-mirror-recycle-20261006/final-protection.json)、[归档清单](evidence/offload-mirror-recycle-20261006/archive-manifest.json)。
