# Offload RAM预算与缓存协议：计划与首轮校准（2026-10-02）

用户已允许开始本阶段。先建立可验证的资源约束与实验协议，再选择
优化策略。首轮独立小额校准已完成；本轮没有运行模型、改动运行时、
清理模型缓存或启动性能矩阵。GEMM冻结，Phase D off。

## 本阶段出口

1. 明确预算的字节数、计入范围、覆盖阶段与swap规则；旧68.34GB
   不再作为实测物理上限。当前建议先探索C=256的可运行资源范围，
   54GB是否仍为最终目标待用户选择，不自行放宽或替换目标。
2. 验证CPU、pinned、CUDA及文件缓存分别落在哪些计数/约束内；未
   覆盖的部分明确列出，不把一个配置参数当成完整物理RAM上限。
3. 冻结缓存与实际存储IO协议、准确binary/模型/热点/输入身份。
4. 完成一组有界HTTP探索，再选定预算与候选；性能接受仍需五档
   加目标档矩阵。质量、容量、资源和性能分开判定。

## 首轮校准已否定“直接用memory.max限制全部RAM”

环境：Thor、kernel 6.8.12-1021-tegra、systemd255、cgroup v2。
探针出生于独立system级transient服务；采集器在组外，逐阶段核对
MainPID、ControlGroup和/proc/PID/cgroup。隔离限额1GiB、swap=0，
仅是小探针保护条件，不是模型预算或已证明的物理总上限。

14阶段各3次观察，CPU/CUDA/pinned每种64MiB，两份任务文件各32MiB。
编译-Wall/-Wextra/-Werror零警告，运行exit0，约5.8秒。未触发OOM，
探针、unit/cgroup、两份任务文件均已清理；模型与共享session未改。
本轮未主动触限；limits与events只证明配置/无触限，不证明所有路径
都被限制。memory.pressure/io.pressure不可用，保留unknown而非0。
证据根：`.q4t-work/offload-budget-20261002/probe/`，源码、采集脚本、
binary与原始记录可复核，独立审查在run-01/analysis/summary.json。

| 动作（阶段中位差） | cgroup charge | 其他对应量 | 结论范围 |
|---|---:|---|---|
| CPU匿名64MiB实际写页 | 约+64MiB，另有管理开销 | 释放后回落 | 普通匿名页进入计费 |
| 读取外部预热32MiB文件 | file项+0，current仅少量波动 | read_bytes+0 | 外部已有cache不会自动迁入此组 |
| 新文件驱逐后重读32MiB | current/file各+33,554,432B | PID read_bytes与io.stat rbytes各+33,554,432B | 本组新缓存与真实读IO可观察 |
| CUDA device64MiB分配并触碰 | current中位+0，peak不增 | NVIDIA+67,108,864B，KReclaimable−同量 | 当前driver池路径未完整计入调用方memcg |
| 再分配/触碰pinned64MiB | current+67,141,632B | file与shmem各+67,108,864B | file包含shmem，不能相加 |
| 释放device64MiB | current中位+0 | NVIDIA−64MiB、KReclaimable+64MiB | 与分配阶段一致 |

这证明本机当前路径上仅设memory.max不足以限制全部q4t物理RAM。
不能从一次64MiB增量推广所有CUDA分配或推导完整物理总数。后续会
分别记录设备分配、host/pinned与文件cache约束；完整去重总量未
闭合时仍判INDETERMINATE。不得简单把这些分项峰值相加。

共享页的计费归属及进程迁移不迁移旧charge的规则见
[内核memory ownership文档](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-ownership)。
本轮外部预热/内部新文件对照给出了当前机器的直接证据。

## 执行计划

### 1. 固定基线与预算语义

初始测量继续使用已验证工作区binary dc845dd3，完整SHA和来源见
[前阶段修复报告](MOE_OFFLOAD_REPAIR_2026-10-02.md)，封存副本与来源
后再接新runner。C=256、hot-final-12288、L2=16、K=8、maxopen=200、
max_prefill8192、max_len262144、单流、MTP off、Phase D off。
不因整理协议重复已有身份未变的质量证据。

若后续改为clean2b0100c或新运行时代码，另建完整binary身份并进行
受影响验收，不能把dc845dd3证据改记到新身份。预算必须明确：
确切bytes、设备/host/pinned/可回收文件cache是否计入、加载至请求
切换是否覆盖、是否允许swap、缺测与超限如何处理。

### 2. 补齐约束与工具合同，再启动模型

- 服务出生前进入独立实验组；monitor和client在组外。C3若包含
  probe和矩阵两进程，应放入同一实验父组，保持文件cache计费连续。
- 内存端同时记录实际分配配置、memcg charge/peak/stat/events/PSI、
  NVIDIA/进程分项和逐文件cache。设备分配未受memcg完整覆盖，须
  保留独立覆盖边界；不能将host/cache额度称为整体RAM额度。
- IO端新增准确PID read_bytes、组io.stat及设备背景统计；现有
  nvme_mb/pread仍只称逻辑文件读取。包含PLE/metadata的服务IO也
  不能全叫作专家IO。父盘与分区统计不得相加。
- runner增加显式选档/重复数的有界入口，部分结果永久标记非完整
  矩阵。每请求前后记录边界、实际容量、成功/输出、时间及IO计数。
- 冷态必须逐文件确认残留；不把“调用过清理”当作冷态建立。目标
  文件缓存清理只在独占实验边界执行，按预设规则记录失败；请求间
  不反复清缓存来冒充持续受限场景。

存储计数语义见[proc文档](https://docs.kernel.org/filesystems/proc.html)
和[设备统计](https://docs.kernel.org/block/stat.html)。本机模型位于
nvme0n1p1，io.stat观察到父盘259:0；共享session缺io控制器，不能
拿它作为独立实验组或给它设置限额。

### 3. 先处理会破坏预算实验的具体代码边界

只读审查发现下列问题，尚未修复，按有界清单处理：

- 预算无可行容量返回max_len=0时，服务回退使用原请求容量；auto
  路径无余量仍强制最低容量。应明确不可行并拒绝，记录requested/
  effective容量；任何缩容都不能算原容量的成功。
- 空热点层在预算端按0槽计费，加载端仍分配全局C；应共用解析容量。
- MTP off仍使用默认has_mtp=true及无条件draft预算；workspace/PLE
  也存在旧尺寸估计。先与实际分配函数核对，避免把估算高计或低计
  当作真实占用，或靠改估计值制造“省内存”。

这些是预算/工具合同工作，直接host或最小缺陷测试可先行；改运行时
后再做相应HTTP。新性能策略本阶段不实施，遵守EVALUATION的顺序。

### 4. 区分缓存状态，有界探索后统一验收

| 状态 | 固定流程 | 可支持的结论 |
|---|---|---|
| 冷启动 | 验证初始cache→加载→首发；无生成预热 | 加载与首发成本；加载本身也会暖cache |
| 同服务转暖 | 同输入连续第2/3发，不重启 | 该请求序列的转暖变化 |
| 历史C3 | 45K×8 token probe→退出→新服务矩阵 | 暖文件cache、冷服务，保留历史对照口径 |
| 持续受限 | 约束贯穿加载与全部请求，不中途清cache/改额度 | 指定资源与时间窗口内是否持续读存储 |

首组建议45056输入×3、每次256输出，保存完整HTTP/缓存/IO时间线；
三次分别观察，不能把其中两次暖请求说成暖态三重复验收。随后只选
一个事先冻结的host/cache约束配置，再做同样45056×3。当前不擅定
该额度，更不把它当全部物理RAM上限。固定超时/失败出口，失败保留，
不自动加额度反复试到成功。工具合同先成组验证，性能实验以HTTP开始。

身份、约束和缓存协议都明确且选定单个候选后，才做五档+目标档，
每档至少3次。C=0若不能放入同预算，只作资源不同的全驻留参考；
受限优化的公平基线应为同预算、同协议的上一候选。三次观察不能
证明长期稳态或稳定尾延迟。
