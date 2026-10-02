# Offload 基础修复 Goal（2026-10-02）

本阶段修复与有界验证完成。入口问题见
[全面诊断](MOE_OFFLOAD_DIAGNOSIS_2026-10-02.md)。加载合同、验收工具和
分项内存账已落地；完整去重物理峰值仍为 **INDETERMINATE**，没有新
性能或整模型发布接受结论。GEMM 冻结，Phase D off。

## 结果与边界

| 范围 | 结果 |
|---|---|
| 3b 关闭路径 | singleton索引修复；16组合真实设备payload合同通过；1K/8K HTTP回退通过 |
| ReadRangev | 部分iovec短读、EINTR、EOF、边界及IOV_MAX合同通过 |
| 分片读期所有权 | 共享读句柄跨驱逐存活；FindTensor元数据在loader生命周期内稳定 |
| 验收工具 | 30项host合同通过；实际binary透传、失败退出、证据保留及两段监控接线 |
| 内存工具 | 9项host合同；真实三组逐文件账审核通过；完整物理总量仍未知 |
| 工作区运行时 | 构建零警告，相关集成11/11，HTTP质量11/11，故障/取消恢复全过 |
| 隔离的提交候选 | 独立host编译零警告、residency合同8/8；未另做该身份的整模型HTTP |
| 性能 | 本轮未运行五档/目标档矩阵，不宣称性能保持、60% decode或TTFT降低30%达标 |

## 身份、实现与提交边界

- 入口HEAD：`1ad87bba0d126e81e83741a8f9e3f78e274bbba2`，分支
  `codex/moe-residency-20260930`。入口diff与二进制封存于证据根的start/。
- 入口binary：`e5b7c7827219db98b255f3c1b63d2593829d873f36741ccf1f3fbe5df8d169a3`。
- 本轮HTTP binary：`dc845dd3eaf5d8cc87929f456b829e4f5d7569e207b7f7105a58f330dba84212`。
  三组均C=256、hot-final-12288、L2=16、K=8、max-open-shards=200、
  MTP off、Phase D=0；merge-off组显式设PREAD_MERGE=0。
- ReadRangev正确推进文件位置与目标iovec，并限制每次iovec数量。
  WeightLoader每个读取持有共享句柄，缓存锁不跨文件读取；缓存驱逐
  不关闭在途读取。稳定元数据副本有额外host空间，真实账包含此成本；
  open_shards只表示缓存句柄数，在途读句柄可使实际fd数暂时高于上限。
- 修复依赖入口尚未提交的3b API/管线；HEAD没有这些实现。因此阶段
  提交明确包含必要依赖，不能描述为只改一行。Phase D及moe.cu接线、
  model/server新增统计与原阶段日志保留在工作区，不顺带提交。
- staging/保存两份quant候选、HEAD/入口/工作区三方向补丁及摘要。
  候选PlanResolve与入口HEAD逐字相同，排除了Phase D；已独立编译并
  通过8项residency合同。**dc845dd3工作区HTTP身份含关闭状态的Phase D，
  与隔离提交树不同**；不把该HTTP记录冒充另一洁净二进制的发布资格。

## 直接缺陷证据

除明确列出的其他根目录，以下均相对
`.q4t-work/offload-goal-20261002/`。

- build.log/test-build.log：q4t与q4t_tests完整/增量构建零警告。
  integrated-tests.log + required-tests.txt：11/11，无跳过。
- dispatch/test-before.log：仅恢复旧任务构造，首个单miss即失败；
  dispatch/test.log：修复后merge 0/1、inline 0/1、1/2/4/9专家共16组合
  全过，包含非零批次偏移。实际GPU权重/SF/scalar与独立checkpoint读取
  逐字节一致，重复Resolve命中且槽位稳定。
- `.q4t-work/io-contracts-20261002/`：contracts.log与asan.log各7/7；
  before-short-read.log、before-shard-lifetime.log保留旧实现失败。
  keep-page-cache.log真实验证缓存保留/逐出；旧测试把mincore成功返回0
  误判为不可用，现已修复，本机支持该查询。
- tools-host-v3/host-tests-final.log：验收工具最终30/30；identity.json
  保存源码与日志摘要。memory/host-tests-v2.log：内存工具9/9。
- staging/build.log、staging/tests.log：隔离3b候选独立编译零警告，
  8/8 residency合同，无跳过；不是另一个完整q4t的HTTP验收。

## 真实 HTTP 与生命周期

证据根：`http/run-20261002-repaired/`。17:38–18:00:59完成。
verification-exit.json确认全部请求检查通过，binary前后摘要一致；
三个服务退出码均0。没有清缓存或新性能矩阵。

| 检查 | 证据与结果 |
|---|---|
| 固定质量 | quality-11/results.json：11题manifest-exact，输入1024至204800，与旧参考逐字一致 |
| merge关闭 | merge-off/results.json：1024/8192各256输出，输入/输出/finish及文本同旧C=0 |
| 单次加载故障 | lifecycle/results.json：首发500且保留fault injection链；同题重发恢复并同参考 |
| Prefill取消 | 实际drain位置8192/45107，原请求409，槽位释放；同题重发同参考 |
| Decode取消 | 已观察真实content后取消，有取消终态；重发输出/计数/finish同参考 |

这些结果覆盖所测请求和前置加载故障，不证明所有中间浮点量逐位相同，
也不覆盖部分H2D入队后的失败恢复。质量、缺陷修复、资源、性能分别判定。
旧正式ed68六档数据经新compare只读复核仍通过旧50%门槛，未改旧证据；
不能因此认为本轮binary通过新性能目标。

## 完整分项账与不可测边界

新monitor在服务启动前ready，绑定准确PID/starttime，记录阶段、采样起止、
RSS/PSS分项、NVIDIA计数、全机上下文及逐文件cache。cachestat不读取payload，
模型文件按device/inode去重。文件集合归属不等于本次进程独占。

三组CSV、逐文件JSONL、manifest、summary与assessment已逐行核对，
每行226文件查询成功、页数与CSV一致。源摘要及检查结果在
memory/final-monitor-audit.json。**下表为独立分项峰值，不可相加。**

| 组 | CSV行数 | RSS GB | NVIDIA GB | 模型文件cache窗口峰GB | 观测缺口 |
|---|---:|---:|---:|---:|---|
| quality-11 | 876 | 4.695 | 56.237 | 73.739 | GPU查询空值1次；无after_exit |
| merge-off | 100 | 4.696 | 56.237 | 73.952 | GPU查询空值1次；完整到after_exit |
| lifecycle | 386 | 4.698 | 56.237 | 73.579 | GPU查询空值2次；无after_exit |

quality/lifecycle的退出端点缺失是controller停止与单次采样的竞争；
服务退出正确性另有exit=0证据。保留原sampling_complete=false，未为
补尾部采样重跑模型。canonical wrapper现改为等待monitor自然观察退出
最多15s，超时才停止并记录原因；host合同通过，未另跑正式C3长矩阵。
quality账本在目录搬移前生成，旧sources路径由只读审计sidecar映射到
quality-11/memory，原文件及hash保留。

| 账目 | 来源 | 交叠或覆盖边界 |
|---|---|---|
| 模型文件驻留 | 逐文件cachestat | 包含既有缓存；多文件扫描不是原子快照 |
| CPU匿名/共享页 | Pss_Anon/Pss_Shmem | 比例归属；pinned可能出现在Shmem |
| 进程file页 | RssFile/Pss_File | 模型映射可能已包含在文件cache中 |
| device/driver | NVIDIA进程计数 | 分配计数与实际物理归属不能直接等同 |
| pinned/Locked/VmPin | API校准、smaps | VmPin/Locked不覆盖全部pinned |
| kernel/其他进程 | meminfo、共享cgroup | 全机变化不能直接分配给q4t |
| 总物理峰值 | 尚缺完整分配归属与去重 | INDETERMINATE，不签PASS或已测超支 |

校准证据memory-calibration/及memory/calibration-analysis.json：
64MiB device触碰使driver增加64MiB而RSS仅增加128KiB；64MiB pinned
触碰使RssShmem/PssShmem增加64MiB、driver不变，VmPin/Locked仍0。
上下文初始化的driver/RSS变化不能判断交叠，不推广为整个引擎的公式。
全机KReclaimable随device分配/释放约减少/增加64MiB，提示既有可回收池
影响MemFree变化；该全局辅助现象不构成独占归属。

sudo只读debugfs核查权限可用，但nvmap统计采集关闭、q4t条目0，
dma_buf主要是显示对象，NVIDIA fdinfo/UVM没有可对应约55.8GB计数的
逐分配物理账。未改变统计开关、权限或运行配置。证据在
memory/debugfs-readonly-20261002/；不能把接口的0当CUDA占用为0。

闲时226文件窗口64.521GB中，192个MoE分片贡献64.417GB，约占其
67.987GB文件大小的94.7%。这支持“暖缓存下几乎整份MoE分片可驻留RAM”
的判断；逻辑pread字节不能写成SSD实际读流量。逐文件证据在
memory/model-cache-ledger-snapshot-analysis.json。

cache+匿名/共享PSS只作为窗口估计，不能当严格瞬时下界。原68.34GB
门禁不能认定为完整峰值；超支授权保留，但未知量仍不能签成已测超支。
canonical C3现在独立采样预热probe与矩阵进程，分别产出
memory-gate-probe.json / memory-gate-matrix.json；任一缺证据或非零
均拒绝通过，不跨进程相加峰值。

## 复用证据与下一阶段

原d0于17:00完成，measured.json仍在
`.q4t-work/moe-residency-20260930/targeted-phased-d0-1534/`。
204800三次TTFT694.477/694.343/694.502s，decode9.8989/9.9679/9.9488；
261887三次TTFT909.046/908.888/909.221s，decode9.0286/9.1158/9.1859。
两档均输出256，正式目标档输出257，预热生命周期也不同，故只保留
定向观察，不改写成完整性能接受。

未纳入本阶段的有界问题仍见诊断：InitHot批内去重、部分H2D失败状态、
离线留出泄漏与真实整组保护/跨阶段回放。下一步应先冻结实际RAM预算
与缓存协议，区分暖缓存和受限内存SSD场景，再建立可实现的分区/驱逐
上界；之后只选一项在线优化，并按规定完成性能矩阵。当前不推进新策略。
