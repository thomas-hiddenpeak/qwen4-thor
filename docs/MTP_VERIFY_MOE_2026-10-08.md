# MTP 主验证 MoE 成本与优化（2026-10-08）

## 当前阶段与目标

从 `f194347` / 服务 `962b5632` 继续，沿用唯一工作分支
`codex/mtp-admission-20261007`。新 Goal 已启动，先测 T4 主验证
MoE 的真实分组与成本，之后最多实施两个有独立回退边界的候选。
本页是首次运行前冻结的诊断协议，尚无新性能或数值通过结论。

上一阶段主验证占循环约 80%–81%，但不能据此认为这些时间全部
属于 MoE；counts 等待还包含前序 GPU 工作。`962b5632` 仅作开发
对照，原严格性能及跨模式数值 NO_GO 保留。正式性能接受仍对
`21d85a17` 封存控制组，不能将未接受版本升级为新基线。

原两个 dirty 工作区、main、模型/reference 和全部旧构建受保护。
新构建仅进 `build/mtp-verify-moe-20261008/`，证据仅进
`.q4t-work/mtp-verify-moe-20261008/`。默认 MTP 关闭，分阶段提交
推送同一工作分支，不自动合并或部署。

## 代码审查决定的范围

当前 `GatherQuantKernel` 和 `SwiGLUQuantKernel` 已各自融合。
下一步可以研究跨专家批量提交，但不能将现有融合重复作为新收益。
默认 routed MoE 使用四个 stream，专家链互有重叠；目前各 stream
仅一套复用 scratch，批量处理多个专家必须另设计不重叠的所有权。

每 expert 必须保留真实 `M_e`、原 token-list 行映射、独立 FP4
scale atom、原 alpha 和两次 BF16 GEMM 输出。不得将 `M_e=2..4`
拆成 M1，也不得统一 padding 到 M4。现有 GEMM 算法缓存的键包含
M/N/K/workspace_bytes/out_bits；改变 workspace 预算也可能换算法。
每个专家 GU scale 区至少 20,480 字节，不能按总 40 行的一个 scale
区简单切片。具体候选要等实测后另冻，不在诊断中改这些计算。

## 诊断范围与固定采样

新增默认关闭 `Q4T_MTP_VERIFY_MOE_TIMING=1`，只支持 S1 文本、k3。
请求持有独立 collector，仅在 `ModelVerifyMulti` 作用域激活；不
采集 prefill、初始化、草稿和 extend 内部 MoE。旧 cycle/init
collector 及解析器保持原合同，本轮三个 HTTP 组均关闭它们。
尤其旧 cycle parser 的 positions 回读 2/1 合同不能冒用于当前
known-position 的 0/0 路径。

- 全部步骤 × 48 层复用原 counts D2H 后的 host 数据，记录 clamp
  前的 `h[0..4]`、routes、active experts、异常与实际 stream 数。
  正常 T4/k10/E512 应满足 `sum(h)=512`、`sum(m*h[m])=40`，
  active 在 10..40。不加 D2H，不开启 RouterCollector 或 dump。
- GPU 仅采 step `{0,1,32,63}` × layer `{0,15,32,47}`，共最多
  16 个调用。每调用采 active ordinal `{0,5,10,15}` 的专家链，
  ordinal 按现有升序 expert 遍历；不存在时明确缺失，不替换补样。
  这些调用另存稀疏 `(expert_id,M_e)`，核实 ordinal 与 stream。
- 每调用 11 个主 stream 边界：MoE 开始、router 后、topk/清零后、
  token-list 后、counts copy 后、原 extra-stream joins 后、routed
  返回、shared GU 后、shared activation 后、shared DN 后、最终
  combine 后。每个采样专家另有五边界，分出 gather/quant、GU、
  SwiGLU/quant、DN。事件预分配，最多 `16*(11+4*5)=496`。
- counts-copy-end event 放原 `cudaStreamSynchronize` 之前，作为
  后续并行汇合窗起点；终点放原 extra-stream waits 后。这个区间
  包含 host 提交间隙、offset/row-map 和并行执行，只称 stream
  elapsed，不称纯 GPU 活跃计算。不得新增同步、wait 或跨 stream
  因果边，也不能将重叠专家叶段相加冒充整个 MoE 延迟。
- 固定调用另记录 host 的 MoE/routed 首尾、counts 同步首尾、
  offsets 提交末尾、expert loop 首尾，解释等待与提交，不把
  counts wait 全部当作可删除同步成本。
- 请求内每步最多六个 host 边界：engine 开始、draft 结束、verify
  既有读回结束、accept 结束、extend 既有读回结束、engine 结束。
  单列 draft、verify、accept、extend 与收尾，均含该段原有准备。
  保存 position、accepted draft、returned、delivered，末步截断
  单独处理，不拿内部 engine 时间强行重建客户端 decode。

最多 1024 步；超过容量、缺层、重复、异常 counts、未就绪 event
或记录失败使诊断无效，但不改变原模型分支、数学和状态结果。
结束时仅 Query/Elapsed，不增加同步；异常退出输出无效记录并
清理自己持有的资源。关闭时不分配事件、不读时钟，仍须观察新增
host 分支的实际影响，不能声称零开销。

计时只代表固定 16 个调用，路由 census 才是全请求统计。报告首步
和后续样本、M_e/stream/ordinal 的覆盖及与 census 的差异。未覆盖
的桶保持未知，不外推全 48 层成本。GPU 分段和总窗独立闭合，按
各 float32 区间 ULP 累加加 float64 求和舍入预算检查；host 使用
整数纳秒差值，不能临时扩大容差去接纳失败记录。

## 冻结执行顺序：41 条新 HTTP

单 Thor 串行，不用 bench，不加 warmup、推理探针、k 扫描、
并行构建或重分析负载。固定输入和质量 reference 沿用旧阶段。

1. 必要构建后，第一项测试：新二进制关闭全部诊断，质量 11 条。
2. 通过后做有限 collector/parser 合同及来源/设备代码身份检查。
   若 21 份完整 CUDA ELF 和数学、形状、参数、状态、stream 依赖
   不变，有限复用 C2 直接/长 k3 数值证据；不重复整模型推理。
3. 同二进制全部诊断关闭，五档各三次，共 15 条。
4. 同二进制仅新诊断开启，五档各三次，共 15 条。
5. 测量退出后统一离线分析。具体失败才修复并重验受影响项；保留
   首次失败，不因不好看的计时追加样本。

固定 S1/k3、greedy seed 20260920、FP8 关闭、max_len 208896、
max_prefill 8192，prompt 1024/4096/8192/45056/204800，输出 256。
绑定 response_id、真实 MTP/no fallback、输入/文字/usage/步骤、
容量、二进制和全部运行/工具来源。按 1 Hz 采 RSS/HWM、Swap 与
MemAvailable，记录自然退出；不据此声称已证明物理 RAM 峰值。

有限合同覆盖：正常短/完整样本、缺层、重复、坏 counts、错误
ordinal/stream、未就绪/缺失 events、容量、错误 shape/k、步骤
及输出计数错配、阶段不闭合、策略/来源错配。完整覆盖清单和
测试源码在首测前绑定，工具失败只修其具体合同。

保留所有三次客户端指标及首条/后两条范围；decode 汇总采用
765 / 三次 decode 秒数之和。同 binary off/on 用于观察诊断影响，
不证明零开销或因果加速。旧 C2 仅辅助对照，primary 不重置。

## 候选阶段与出口

按实测成本最多选两个候选；若观察不足或原算法约束下没有可信
空间，可以有证据地 NO_GO，不强行扩大任务。每个候选先另冻结
范围、旧新同形 exact 合同、资源所有权和 HTTP 门禁，再实施。
对纯分组/搬运/提交变化不接受新的经验数值容差；历史粗参考测试
不足以替代 packed/scale/GU/DN/最终输出及后续 k3 状态对照。

每候选仍先质量 HTTP 11，再受影响 exact 数值，最后正式五档
15 条。严格规则是 TTFT/decode_seconds/latency 的候选后两次
各自不超过原 `21d85a17` 后两次最大值，共 15 格；不同档、指标
不能抵扣。相对最近开发对照另列，NO_GO 不因平均收益被覆盖。

交付完整成本账、候选接受或 NO_GO、draft/verify/整体 decode/
TTFT 的结果与限制、保护核对和阶段提交。此前跨模式数值问题
继续单列，不能由本轮质量或同形 exact 自动补足。
