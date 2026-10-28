# 多槽单请求草稿 prefill 评分：有界候选

状态：实现/构建准备，尚未运行测试。用户授权2026-10-29。
父程序6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965，
主线起点dab8321。工作分支codex/mtp-prefill-scoring用于保留未交付候选，
不替换build/q4t。本轮是性能优化的定位阶段，不构成正式交付或五档验收。

## 唯一运行时改动

MtpDraftExtend已有host seq_id；当多槽且本块T>4时，仅为草稿attention
索引评分传入该槽压缩索引切片，选择既有BF16 GEMM/归约。KV、raw/comp
写入、RoPE、页表仍用原pooled地址和d_seq_id；混合多槽generation、T<=4
pending修补及接受后extend保持原分派。均匀布局及不等长槽偏移都来自
既有SequenceOffset。Q4T_MTP_PREFILL_LEGACY_SCORING=1为单候选消融，
仅撤销评分切片；不改变其它工作。不同时优化head行数、分配、同步或调度。

这是执行映射及舍入变化：旧SIMT逐元素FP32累加，既有单槽GEMM先存BF16
点积再归约。不能走删除冗余的逐位等价豁免。独立单槽GEMM参考与新pooled
评分输出要求exact；旧SIMT差异单列。两侧持久KV/raw/comp和只读元数据
要求exact，其他槽必须不写。实际两head HTTP输出要求与冻结同输入原值一致，
保留接受直方图，不因目标verify忽略草稿状态/质量。

## 预算与冻结顺序

必要构建不算测试。验证从完整阶段驱动开始至退出，上限5400秒；该驱动
全程持tools/evalscope/gpu_exclusive.py共享独占锁。冻结绝对截止在启动前
绑定；计划、源码、父候选、子集产物和输入SHA绑定，运行中不得变更。
总上限16生成HTTP、8服务启动、2直接GPU fixture；不重试、不扩样、不扫描。
监控故障/环境竞争ENV_ABORT，仅清理自己的进程。当前交付保持不变。

1. 真实tools/evalscope HTTP优先：full候选→full父版→subset父版→subset候选，
   每服务按8K、45056、204800三档各1条，共12条。复用原固定prompt，
   256输出，greedy，seed20260920。关闭全部诊断，无warmup，不清OS缓存，
   每服务的私有/公共SSD缓存从空开始，四槽208896+3×65536、prefill4096，
   suffix和两个缓存池启用，保护请求不带session/scope，因此无缓存读写。
   每条核对prompt SHA、实际输入、256输出、length及完整输出SHA；失败即停。
2. 12条完成后核对两head200K TTFT均至少下降20%，同时E2E latency下降。
   8K/45K及decode原值全部公开，不能以长档收益抵消已确认退化。
   目标未达即结束，不启动fixture和诊断。样本为描述性单对，不建立非劣统计。
3. 两head各一次直接GPU合同（每次上限600秒，两次合计上限1200秒）：真实
   四代表目标层与真实draft，槽位16384/12288/10240/8192。独立attention
   评分覆盖slot3短块、slot2跨8192边界、slot1长上下文、slot0完整4096行末块；
   新pooled与独立sliced GEMM输出exact，旧SIMT输出差异记录，三者完整池
   KV/index写入exact、只读元数据/其他槽及未写区间exact，有限值检查。
   复用独立架构状态枚举，1024/8193两边界，源槽覆盖、异槽恢复、私有/共享
   查找、改变pending、四个分支与17新增token、最终g/d0及target logits exact。
   不把4层参考称48层逐层证明；完整48层由HTTP覆盖。
4. 合同通过才做4条诊断HTTP：同候选full旧评分→新评分、subset新→旧，
   每次新服务、仅200K一条。Q4T_PREFILL_SCORING_TIMING=1记录每块target、
   draft、draft attention和index score的host wall/thread CPU与CUDA stream
   elapsed。最多每块8条延迟采集事件，既有完成边界后读取，不新增stream同步。
   事件区间包含提交空隙，不称纯kernel活动；父子区间不相加。诊断不是接受样本。

fixture直接覆盖缓存状态，HTTP实际缓存命中/重复场景净收益本阶段不重测，
不得把旧净收益自动归给新候选；旧未变存储/队列/取消逻辑证据复用。
本轮通过仅表示主因和候选成立，后续正式验收仍须冻结适用五档、两head
重复收益和受影响缓存HTTP；不在本预算内自动展开。

## 出口

任何必要HTTP/状态合同失败、任一head200K不足20%、ENV_ABORT或时间预算耗尽
即停止，记录证据有效性、测量完整性、统计结论和工程决定，不自动补跑。
通过记RESEARCH_GO_REQUIRES_RELEASE_VALIDATION；不通过记具体NO_GO或ENV_ABORT。
保存候选、父程序及发布元数据、原始日志和失败现场，文档及阶段提交推送。
