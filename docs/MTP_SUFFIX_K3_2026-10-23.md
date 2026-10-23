# 固定 k3 suffix 草稿：冻结、验收与决定

2026-10-23；实现及生产/CPU/GPU测试程序已零警告构建，尚未运行测试，
不代表接受。

## 身份与范围

父源码 `a75643c`（main，进入时工作区干净）。父程序
`build/q4t` SHA256
`f356b2c96330101abf1e5fa3fced495a37f4ea5a270ed4457037e0d55735d313`，
sidecar记录实际构建源码 `9a902a822b96b4ea25ae9f47fa5c0a7862f175c0`。
工作分支 `codex/suffix-k3` 为本阶段唯一候选入口；独立构建及原始证据
位于 `.q4t-work/suffix-k3-20261023/`。现有默认程序和发布元数据保留。

子集沿用 `.q4t-work/sha2-startup-20261023/delivery-package/artifact`：
65536行、248320全词表、2560隐藏宽度、BF16。manifest SHA256
`b63b0f251fddc8a75749a54dd4b22477fdb8da875cc26f83595534c71062c3ce`；
head `21522eabb4ab4f816d9f76eefe317a17e7ba668ebe838809eb4be87d84ef75c1`；
IDs `71d89c6d2899c22641ac3d1f7f4957de94eddc5256b3f60bc8242f24becf590b`。
不修改或重建子集。完整head仍默认，子集仍通过`--mtp-subset-head`选择。

独立 `serve --mtp-suffix` 默认关闭。只支持单流、text greedy、MTP k=3；
与 `--no-mtp`、非3的k、max-seq非1、media或max-len超过262144组合
在启动阶段失败，预算推导后的有效容量也检查上限。现有HTTP解析器
继续拒绝非greedy采样。generate不扩展。上下文/剩余输出不足四行时
沿用普通decode，pending token保留。索引资源申请失败记录并回到
现有MTP；不能将这类请求标为成功使用suffix。

## 上游研读与固定算法

只读参考 `.q4t-work/reference-review-20261016/strata`，固定
`296b59d2f73c8f04a805df29c2fa97655adace78`，MIT许可。
`suffix_drafter.cpp`的三元组哈希、最近四位置、逐token精确后向比较、
最长匹配/最近位置平局规则，以及`propose_after`的临时pending语义适用。
生成循环先要求suffix首候选等于已计算的draft[0]；目标verify和接受后
草稿状态追齐均保留。上游动态T、耗时policy与临时insert方式不引入。

冻结匹配长度3–64、65536哈希桶、每桶4个uint32结束位置；桶占1MiB。
提交历史最多262144个int32，占最多1MiB，动态payload合计最多2MiB，
另计固定对象开销。哈希碰撞仅允许挤出候选而漏匹配，绝不免除精确比较。
先选最长/最近匹配，再要求完整三个合法全局ID且首个等于d0；失败回MTP，
不为符合d0改选更短候选。查询只读“已提交历史+pending”视图，周期
续接可读取视图已有token，绝不读取未来。不要求候选属于head子集。

索引归请求所有，初始化prompt、只追加实际提交前缀；拒绝尾部不入库。
取消/失败/终止析构释放，槽位复用创建新实例。超容量停止suffix并记录；
不移动到其他缓存。记录初始化、追加、查询、释放成本及内存峰值。

真实HTTP落点为scheduler的`MtpSpeculativeStepMulti`。suffix只替换
神经draft循环，目标四行verify、checkpoint恢复、accepted-prefix draft
extend及next_d0/next_g更新保持。新增直接测试必须用独立接受前缀oracle
同时核对目标和草稿有效状态，不能用verify存在代替数值证明。

## 事前验收判定卡

这是新增算法的性能优化，不能套用业务推进小幅波动或删除冗余路线。
首项测试必须为tools/evalscope真实HTTP E2E：完整head suffix开启11题。
必要构建不算测试。首门通过后才做直接CPU/GPU合同及细分析；任何明确
质量/合同失败停止整个阶段，保留首次失败，不自动修复后扩样。

- 目标：完整head与子集head分别在重复代码、重复文本两个场景达到
  decode tok/s中位数至少+5%，作为新增维护成本的投入目标。
- 保护：每模式五档TTFT/decode、非重复场景、普通decode及质量逐项
  审查；不得用平均收益抵消具体回退，不设统一可损失百分比。
- 五档：现有snap-records输入1024/4096/8192/45056/204800身份，每档
  3正式请求、输出上限256；保留首请求和EOS实际长度。full OFF→ON，
  subset ON→OFF，各模式独立报告；这不构成跨启动重复设计。
- 普通decode：同期父程序→候选各五档15请求。质量full ON/subset ON/
  candidate plain各11题；OFF模式历史质量仅在身份/依赖不变边界复用，
  本轮输出差异必须检查，不能把历史程序冒充候选。
- 场景：重复代码、重复文本、非重复各3请求×四臂=36；验收材料固定
  后不调参。开发材料只用于人工token/状态夹具，不以验收输出来选参数。
- 生命周期：full/subset取消与重新请求各3条=6。单独计时诊断四臂×
  三场景×1=12，使用Q4T_MTP_TIMING；这些数据不代替未插桩E2E。
- 主预算177 HTTP、19服务启动、墙钟4小时（含直接合同≤20分钟）。
  接受后的部署健康另最多3启动/3HTTP，累计180HTTP。无额外warmup、
  参数扫描或自动扩样。具体顺序/缓存条件/输入摘要由执行前plan固化。
- 原始客户端数据、请求、服务日志、索引统计、接受分布及完整资源
  时间线保留。统一RAM包括host/device/managed与文件缓存，不能相加
  独立峰值；suffix实际payload和系统占用分开，不把未测项写0。

缓存采用已交付auto启动资源预留及自然继承文件缓存；不清页缓存，
不额外预热，逐次记录启动和请求资源时间线，不声称SSD冷态。完整驱动经gpu_exclusive持同一共享锁。
竞争或监控失败ENV_ABORT，只停止自己的进程，保留现场；轮询不证明
没有短暂干扰。预算/超时/质量/状态/资源门失败均停止，不自动重试。

统计重复层级为每臂一次服务启动内的请求，跨启动不确定性未建立；
不声称非劣、等效或任意输入逐位一致。最终分别记录证据有效性、测量
完整性、统计结论和工程决定。目标未满足则保留当前交付版并归档候选；
满足适用门后按已授权流程保留回退包、部署适用程序/元数据、健康检查、
文档及提交推送。PLE提前读取、量化/扩容、动态k、Graph、GPU接受及
SSD专家策略均不在范围内。
