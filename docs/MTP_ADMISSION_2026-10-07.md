# 单流文本 MTP 准入（2026-10-07）

状态：资源合同通过，数值对照存在未解释差异，尚未准入。用户授权
Goal 持续推进；唯一工作分支为
`codex/mtp-admission-20261007`，起点 main `8cd297b`。原研究目录、两处
未提交修改、已验收主线工作区和二进制不变。

## 目标与边界

判断现有 MTP 能否可靠使用，以及是否值得显式启用。单 Thor、指定
checkpoint、文本 greedy、max_seq=1、k=3、max_prefill=8192、
max_len=208896；默认继续关闭。上下文尾部不足完整验证步时允许明确
记录的普通 decode 收尾，不通过缩减正式五档容量取得通过。

仅修明确的资源、生命周期、生成与验证入口缺陷。不引入 draft 量化、
动态 k 优化、offload、媒体、多流或新的 kernel 性能候选。若正确性或
收益不达标，以具体证据和 NO_GO 结束，不自动追加其他优化方向。

## 冻结任务

1. 资源：按实际 LoadMtp、DraftExtend、ReserveScratch 与 verify
   checkpoint 分配补齐独立权重、持久缓冲、临时峰值及加载 host staging
   预算；预算仍是分配估算。修复部分加载/扩容失败的清理及容量发布。
2. 生成：验证实际 `MtpSpeculativeStepMulti(B=1)` 的数值及接受/拒绝
   回滚；正式 plain decode 作为对照，不以重新 prefill 冒充。修复
   scheduler 不可用时的回退、上下文尾部、停止、取消等具体失败。
3. 证据：扩展现有 evalscope runner 显式 MTP 模式，逐 HTTP ID 绑定
   实际路径，拒绝把静默回退当作 MTP 结果；记录 TTFT、decode、整请求
   耗时和内存。不修改既有参考、精度或接受门槛。

## 验收顺序与出口

- 不变主线二进制 `0f52e926` 的 HTTP 质量 11/11 按身份复用。先补其
  五档性能基线（各 3 次、输出 256），旧部署和 offload 数据不替代。
  该独立基线可与候选代码编写并行，禁止并行 GPU 测试或服务。
- 修复按上述固定范围成组完成后，统一零警告构建、预算/资源故障
  合同、MTP 直接数值/回滚测试和 runner 合成反例。直接合同只用于
  缺陷修复，不证明完整模型质量；跨路径数值差异必须保留和解释。
- 候选先做实际 HTTP 质量与输出边界（MTP off/on 分开）、上下文尾部
  和取消恢复。只对具体失败定向修复、重验受影响项。
- 获得性能接受结论仍需候选 off/on 各完整五档 HTTP E2E，同输入、
  同容量、同规则。分别给出质量、性能与内存结论；单次或短上下文
  收益不抵消其他档位回退。未运行、失败、模式回退均明确标记。
- 内存采样以进程及整机可观察字段为界，分配估算或 RSS 不能证明
  整机 RAM 上限；不进行大体积 tensor capture。现有约 10 GiB 磁盘
  余量用于独立构建和有界 HTTP 证据。

必要修改分批提交在同一工作分支，统一验收后给出可启用范围或明确
NO_GO；本 Goal 不改变默认部署，不自动合并未经接受的 MTP 能力。

## 第一组实现与直接检查

候选 q4t SHA-256 为
`b839a6745deeb3712005c0cf8334f3019bbff6c69623ba9f0316bec3e603d416`。
本机 Release/C++23/CUDA 13.3/SM110a 构建零警告。工作区
`build/runtime-source-identity-01.json` 绑定 124 个运行时/构建源文件，
包含新文件；不能只用未包含 untracked 文件的 Git diff 绑定身份。

通过：22 项预算合同、2 项上下文边界合同、18 项 runner 模式反例；
真实 CUDA 生命周期检查覆盖 22 个 scratch 分配失败点、3 个 checkpoint
失败点、部分加载的两个 stream、真实第 8/39 分配点、成功/重试及借用
权重不被释放。旧 multi 夹具的 accepted_count 越界已修复，原测试通过；
它仍使用旧的受限参考，不能代替新准入测试。

新四层直接对照首次失败，记录于 `build/numerical-tests-01.log`：
`numeric_equal=0, rollback_exact=1, greedy_equal=0`。固定后续 token 的
首行 logits 最大绝对差 0.240234375、相对 L2 0.0466552478；自然 k=3
首步 bonus=287 相同，下一 token 为 MTP 359、plain 220。输入、层数和
路径见测试代码，属于短模型诊断，不推导完整模型任务质量。接受/拒绝
同路径回滚及下一步固定 token 检查逐位相同；跨路径差异待解释，不
预设新的容差或直接归因为回滚错误。

## 当前主线五档基线

未修改的主线二进制 `0f52e926` 完成 15 条同输入 HTTP，输出与冻结
参考一致、服务退出 0。TTFT 为三次算术均值，decode 为调和均值：

| 输入 token | TTFT（秒） | decode（token/s） |
|---:|---:|---:|
| 1024 | 0.8202 | 18.5175 |
| 4096 | 2.6258 | 17.8748 |
| 8192 | 5.1614 | 18.1131 |
| 45056 | 30.4047 | 17.8453 |
| 204800 | 159.1450 | 17.1249 |

原始证据位于旧主线工作区 `build/mtp-admission-baseline-off-20261007/`；
本阶段外层目录的 `baseline-summary.json` 保存实际绝对路径和摘要。
内存采样从运行中途开始，仅覆盖部分后续请求，不覆盖完整启动峰值。
这些是本轮新测基线，尚无候选加速/持平接受结论。

## 首次失败后的有界补修

候选 b839a674 的普通模式 HTTP 质量 11/11、真实路径审计和退出通过，
证据 `.q4t-work/evidence/quality-off-01/`。这不能排除计算合同缺陷。

代码审查定位两项具体问题，限定本轮补修范围：

- 实际 scheduler 使用的 `ModelDecodeBatchMulti` 未更新当前 token 的
  三行 RoPE 坐标。prefill 只填输入范围，后续位置仍为 0；其他 decode
  及 verify 入口写 logical position + rope delta。先保存真实调用反例，
  再补齐写入，检查槽位和邻位隔离。旧普通模式只作兼容对照。
- checkpoint 写入以当前 `num_ckpt` 排布，恢复以分配容量定位。预留
  3 后使用 1 时步长不一致；原 same-path rollback 对照共享恢复逻辑，
  不能证明状态正确。增加独立容量布局反例再修，不用原通过掩盖缺陷。

唯一一轮现有 hook 采集保留于外层 `linear-capture-01/`：492 文件、
31,343,616 字节；重复 prefill 逐位相同，固定 token 首个观测差异在
layer 0 的 qkv_raw（首行 19/10240 元素不同），自然首 bonus 则更早在
输入 x（4/2560）。这支持投影算术路径也存在差异，不构成完整归因或
新的误差门槛；hook 改变同步，数据不作性能依据。不追加大规模抓取。

两项旧版反例已实际失败并保存于外层 `repro-old/`（含测试源码和库）：
RoPE 两步三行均为 0，期望分别为 13 和 19；checkpoint 两种容量的
prefill/verify 状态和 logits 逐字节相同，但恢复后 layer 1 SSM 有
3,136,146/3,145,728 字节不同，后续 logits 有 343,074/496,640 字节不同。

补丁按当前 verify 的实际行数定位，记录有效槽位，在新 forward、
reset、增长/失败与销毁时失效；拒绝未写行、无效槽和不足容量。
旧 `ModelDecodeBatch(save_checkpoints=true)` 的单序列 PLE kernel
没有保存 PLE checkpoint，本轮在提交 GPU 工作前明确拒绝并指向
`ModelVerifyMulti`。不宣称旧 standalone MTP 已获准入。

修复前后分开保存身份；统一重验这两项、受影响生命周期与原数值对照。
跨路径 `numeric_equal=0` 只表示逐位不同，不单独证明算法错误；同输入
同算术容量变化必须逐位相同。修复后的 HTTP 质量、生成边界和收益仍
分别判定，旧错误 plain 输出仅作兼容/修复代价对照。

修复候选 `3073656388802257502946503f413ae0a83fdb6bb00f08f32da0e066184d2cf3`
已零警告构建；两项独立反例均由 FAIL 转 PASS，容量变化后的恢复状态
与后续 logits 逐字节相同，所有有效性边界及加载/扩容失败清理通过。
原四层诊断仍为 `numeric_equal=0, rollback_exact=1, greedy_equal=0`；
固定后续 token 首行 logits 最大绝对差 0.203125、相对 L2 0.0400214922，
自然首步 correction 仍为 MTP 359 / plain 220。保留在
`build/*-03.log` 与 `build/direct-results-03.json`。这些有限观察尚未闭合
跨路径生成/数值接入；完成冻结的 HTTP 合同后若仍未闭合，按 NO_GO
出口停止 MTP 五档收益验收，不以未准入能力的局部速度声称加速。

兼容范围：非 serve 的旧 CLI 仍使用 `MtpSpeculativeStep`，会遇到
上述 legacy PLE checkpoint 错误；本轮未将它迁移到 Multi，也不将
它列入可用入口。当前分支是阶段候选，并非默认部署或全入口发布。
