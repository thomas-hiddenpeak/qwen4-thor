# MTP 验证候选一：四 stream 批量 GatherQuant

## 实施前冻结（2026-10-08）

这是当前主验证 Goal 的第一个优化候选，沿用
`codex/mtp-admission-20261007`。诊断服务 `dd2e79c7` 的 41 条 HTTP
及独立 raw 审计已完成；成本见 [MoE 诊断](MTP_VERIFY_MOE_2026-10-08.md)。
56,304 次验证 MoE 平均 28.77 个活跃专家，固定样本专家 host 提交
451–522 微秒，parallel stream 窗口 514–580 微秒。这支持减少提交
次数的实验，不能证明 CPU 瓶颈或预报 decode 收益。

只尝试跨专家批量 gather/量化，第二候选尚未选择。不改变路由、
实际 M_e、GEMM 形状/算法/workspace、FP4 舍入/scale 布局、alpha、
GU 与 DN 的 BF16 输出、SwiGLU、最终 slot 累加顺序或 MTP 状态。
既有跨模式数值缺口、所有性能 NO_GO 和默认 MTP 关闭保持。

## 实现合同与回退边界

- 新 `Q4T_MOE_BATCH_GATHER=1` 显式 opt-in，其他取值关闭。仅支持
  M4/k10/E512/hs2560/moe_is640/四 stream，原始 counts 非负、
  每项 <=4、总和 40。其他形状或异常 counts 沿用旧路径并计数；
  不借本候选修改历史异常路由语义。
- Host 从原 counts 构造按值传给 kernel 的固定 row descriptors，
  每项含 expert、原 local row、active ordinal。每个原 stream
  一次 batch gather；仍读原 token-list，不排序其 atomic 行序，
  不增加 descriptor H2D。之后原升序 expert 循环及 round-robin
  stream 分配不变，各 stream 保持 GU → SwiGLUQuant → DN。
- 40 个独立 GU 输入 slots，每个 packed 5,120 B、SF 20,480 B，
  共 1,024,000 B。所有基址 256 对齐；这里只保留最多四行的
  容量，GEMM 仍使用真实 M_e，不能补成 M4 或拆成 M1。
  Arena 附在原 routing allocation 后，与原 metadata 同寿命；
  不新增正常路径分配/释放次数、event、wait 或同步。原 counts
  同步已保证 allocation、x、token-list 对 extra streams 就绪。
  GU 输出和后续 DN 输入继续复用原 per-stream scratch。
- 已提交 extra stream 工作后的失败出口，先 join 再释放 arena；
  清理失败显式报告，不能释放仍被使用的内存。测试 capture 只在
  专用入口使用；服务不分配或复制观察缓冲。
- 预算显式增加 1,024,000 B main forward 临时保守费用，不减少
  原 GEMM workspace 或其他预算。它是 allocation estimate，
  不证明整机物理 RAM 峰值。
- 实際 applied/legacy/bad-counts/launch 数采用 opt-in 时的轻量
  进程计数，在 S1 请求前后取差并绑定 response_id，明确范围为
  单流请求窗口，不作为一般并发请求统计。正式五档无 T4 prefill
  余块，要求 applied_calls=48*mtp_steps、batch_launches=4*applied、
  bad_counts=0；保存减少前后的 gather launch 数。
- 低层形状门禁也影响 plain T4 及 8196 prompt 主 prefill 的末
  四行；必须在数值检查中覆盖，不能称为仅验证分支变化。
- 旧 MoE 诊断 v1 与候选显式互斥。Batch 中不存在逐 expert gather
  叶段，不复制/平分整批时间，也不为诊断静默换回旧路径。所有新
  HTTP 关闭 cycle/init/verify-MoE 诊断；不增加计时请求。

## 有限数值合同

必要构建后第一测仍为 HTTP 质量。通过后才执行以下固定检查；
没有具体失败不增加样本或改阈值。

1. 真实 layer 2 权重，M4/k10 下三套固定路由，旧/新各一次，共
   六次 routed forward：40 个 M1；10 个 M4；混合
   h1=18/h2=4/h3=2/h4=2。包含 expert 0/511，打乱 expert ID
   与 token slots；BF16 x 包含零、正负有限值，非二进制 router
   weights，初始 y 非零。复用同一工作区，验证覆盖与 guards。
2. 专用 capture 入口调用实际旧/新实现。在原 stream、原 scratch
   覆写前 D2D 捕获 GU packed/SF/GU BF16、DN packed/SF/DN BF16；
   原 joins 后一次统一读回。保存 counts、token-list、offset、
   row-of-flat、每 expert GEMM key/算法/alpha/workspace。
   按每次实际 token-list 转成 canonical(token,slot)，有效元素
   exact 比较；不要求 atomic 分组物理行顺序一致，不将 padding
   未初始化字节当数学值。新 arena 未用行、padding 及外侧 guards
   在测试入口初始化 sentinel 后核对；捕获缓冲也设 guards。
3. 开启候选，运行既有完整模型短 k3：16 自然+1 强制步骤，接受
   0/1/2/3 draft 全覆盖；123 个冻结 raw 文件逐字节对照。该旧
   raw 没有完整跨版本 recurrent/KV，不能扩为全状态 oracle。
4. 开启候选，运行既有 8196 长初始化 Full/Skip：两次独立主
   prefill(8192+4)，共 10 actual k3 steps 和两个固定 probe。
   新两分支内部 exact 之外，再对 C2 封存的 90 个 raw 文件
   （42,733,425 B）逐字节比较，含完整已保存 recurrent 与 MTP
   KV/indexer/位置状态。Full/Skip 是初始化策略，并非 batch 的
   off/on 对照。中间 draft head logits 未保存仍属观察限制。
5. 有限 host 合同验证开关/shape/counts门禁、固定布局/descriptor
   容量、预算新增费用（普通与 MTP）及饱和边界；专用失败注入
   固定一次，在 mixed 新路径正常 forward 之前、首个 extra batch
   已提交后人为返回失败并清理；随后原计划的 mixed 新 forward
   验证资源再用，共六次正常加一次失败尝试，不注入 CUDA fatal
   error。Host 门禁/布局至多十组，预算另三组。
   `batch+v1` 的拒绝只验证一次启动配置，必须在模型加载前失败。

新增数值容差为零；有限轨迹不能补足普通/MTP跨形状等价。
测试源码、固定路由及 runner 在第一测之前绑定，不重建旧模型
数据，不写模型或 reference，不使用 bench。

## HTTP 顺序与接受条件

封顶 26 条新 HTTP：候选开启质量 11 → 上述有限直接检查 → 候选
开启五档各三次共 15。无 warmup、探针、k 扫描或有利重采样。
单 Thor 串行，测量期间不构建或做重 CPU 分析。固定输入/reference
复用旧组，S1/k3/greedy seed20260920/FP8关闭，容量保持
max_len208896/max_prefill8192，输入1024/4096/8192/45056/204800，
输出256。保存完整HTTP/SQLite、actual path、资源1Hz与退出记录。

接受仍对原 `21d85a17`：各档候选后两次 TTFT/decode_seconds/
latency 分别不超过原后两次最大值，共15格，无新容忍、无档位
抵扣；相对 C2 `962b5632` 和最近 `dd2e79c7` off 组另列描述。
质量或数值失败先保存首次证据，只针对具体缺陷修复并重验影响项。
未通过严格性能仍可保存研究快照，不升级基线或默认启用。

新的 build 与证据分别限于 `build/mtp-batch-gather-20261008/` 与
`.q4t-work/mtp-batch-gather-20261008/`。本阶段原130运行来源及工具
共170文件已封存到诊断目录 `source-snapshot.json`，旧证据通过其
原始摘要解析到快照；main、dirty工作区及所有历史构建保持。

最终报告沿用已完成诊断中的 draft/verify 成本；本候选五档诊断
关闭，不伪造其新的绝对内部计时。整体decode/TTFT/latency由新
E2E直接观察，阶段提交推送同一工作分支，不自动合并或部署。
