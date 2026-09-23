# 完整 4K MoE gate/up 输入、权重与记录算法重放

2026-09-24。生产二进制、计算路径和精度未改变。原 4K 检索题期望
`360284`，仍输出 `600440`；本阶段不表示任务正确性或性能验收通过。

## HTTP 顺序与观测范围

只读观测器零警告构建后，第一项测试为 tools/evalscope 原 4K HTTP，
按关闭观测、开启观测、关闭观测各一次。三次均输入4096、输出7、
`stop`、服务退出0；质量驱动退出1，答案都是原错答。只通过这一个
请求的观测不干扰检查；诊断时间不能用于性能比较，没有运行 bench。

采集完整48层21197次活跃专家 gate/up GEMM，及随后 SwiGLUQuant
的输入输出。实际输入与此前全行 gather 快照按 `(token, slot)` 绑定，
不能假定原子 token list 的行顺序相同。1966080个专家行中1888982个
位置与旧排列不同；逐专家重排后的有效 FP4/SF 字节全部一致，所有
flat 索引覆盖完整且恰好一次，expert 归属匹配冻结 router ID。

## 权重与相邻算子交接

为避免复制约39GB权重，预先从 checkpoint 生成190773段来源计划，
记录路径、offset、长度、SHA256、文件大小和mtime。现场直接逐字节
比对 GPU gate/up packed 权重及 swizzled SF 与 checkpoint；确认
input_scale、weight_scale_2、实际 alpha 及下游 down input_scale。
三侧 HTTP 后重新读取全部39070734340字节，摘要匹配。

同时检查 FP4/BF16 类型、矩阵形状、转置、FP32 compute/scale、host
pointer mode、两侧 VEC16_UE4M3 模式与尺度指针、默认 epilogue、
beta=0、32MiB workspace，并保存实际 cuBLASLt 算法。

保存2516582400个完整 GU BF16 输出；现场核对它们正是 SwiGLUQuant
读取的输入。保存后续1258291200个 FP4 值及物理/逻辑 SF，独立
reshape/transpose 确认78643200个有效 SF 字节布局相同。padding
原样保存但不作有效值解释；尚未验证 SwiGLU 与中间量化算术。

## 记录算法重放

离线重建实际行序的 packed 输入和逻辑 SF，独立按 tile/lane/group
生成物理布局，未使用 padding 行清零。由 checkpoint 重建完整
GU 权重，使用实际 alpha 和记录算法；每个算法先检查兼容性。
cuBLASLt版本130501，21197次 GEMM 的2516582400个 BF16 输出
全部逐位一致。完整重放输出、全部差异索引文件和逐层结果均保存，
没有抽样、阈值放宽或选择较优重放。

这是同一 cuBLASLt 算法的可复现性核对，**不是独立算术证明**。
重放使用实际 SF，包含已经确认的 E4M3 高区间编码错误；不能借由
输出一致宣布输入量化正确，也未传播修正后的尺度。

补充对照此前末行480份 GU 输出文件。旧阶段 manifest 只列摘要和
工具，未列这些输出文件；本阶段首次记录其 SHA256。因此末行相等
只说明与当前保留文件一致，不能声称获得了旧文件的历史摘要保证。
完整本次观测和重放均另有逐文件摘要，不依赖该补充对照成立。

## 证据与下一步

证据目录 `.q4t-work/e2e/moe-full-gu-20260924/`：

- `raw-http-audit.json`、`exit.json`、三份原始 HTTP 结果；
- `checkpoint-plan.json`、两份 prior binding、`boundary-reference.json`；
- `observed-captures/`、`reference/`、逐层 replay JSON；
- `previous-tail-binding.json`、`summary.json`、`artifact-binding.json`。

可复现模板见 `tools/verify/moe_full_gu/`。已接受二进制仍为
`2392f6b1fb33f28744d0e63812e47be98b4c6f224e22f087bd627d050f8bbe9e`。

下一步复用冻结证据做完整 GU 独立高精度参考，再核对 SwiGLU/
中间量化、down/shared/combine 的完整计算链。原错答因果、完整模型
正确性和其他上下文档位的扩展验证仍未完成；不开始新的性能优化。

## 后续：完整 GU 高精度参考（2026-09-24）

复用上述冻结 HTTP 证据，生产及观测器均未改，不重新启动服务。
参考程序零警告构建后，重读绑定全部输入/行序/实际 GU 输出及
checkpoint 段。独立以 E2M1 数值表和 E4M3 指数公式解码权重与
输入，不调用生产转换函数；保留实际 SF 及现场 FP32 alpha。

完整21197次矩阵乘采用 FP64 Dgemm，结果经 FP32 再做 BF16 最近
偶数舍入。**2516582400个输出全部同实际值，无差异。** 每层最后
一个输入 token 的十个专家行另以 CPU 顺序 FP64 乘法、加法计算，
关闭乘加收缩；480行614400个未舍入 FP64 值与 Dgemm 逐位一致，
舍入结果也同实际 GU 输出。CPU只交叉覆盖这些末行，不扩张为完整
输出均做过独立 CPU 计算；完整高精度参考仍使用 cuBLAS FP64。

完整舍入 BF16 已落盘，所有差异索引及对应 FP64 文件保留（本轮
均为空）。末 token 保存完整 CPU/GPU FP64；其他一致位置的未
舍入 FP64 不落盘。最终审计重新读取输入摘要、完整舍入输出及
差异文件，验证统计和末行交叉结果，没有引入容忍阈值。

这缩小了本请求的排查范围：实际量化输入上的 gate/up 矩阵乘未
发现该参考能揭示的输出偏差。但实际 SF 中仍包含前序已确认的
编码错误，尚未传播修正，不能把此结果解释成整个专家量化链或
整模型正确。原4K错答未修复；没有新增性能结论。

证据 `.q4t-work/e2e/moe-full-gu-fp64-20260924/`，包括输入绑定、
checkpoint计划、48份逐层结果、完整参考与最终manifest；工具
`tools/verify/moe_full_gu_fp64/`。下一步复用已采集边界核对完整
SwiGLU/中间量化，然后继续 down/shared/combine。
