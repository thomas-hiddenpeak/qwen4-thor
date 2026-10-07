# MTP 初始化末行 logits 优化（2026-10-08）

用户授权继续 Goal。沿用 `codex/mtp-admission-20261007`，起点
`7c929cd`。上一阶段已证实固定负载有 decode 收益，但 TTFT 全档
增加、200K 整请求回退；本阶段只处理初始化中未使用的 logits。
默认 MTP 关闭，候选尚未验收，不合并、部署或改默认入口。

## 冻结实现与范围

唯一候选：MtpForward 追加默认 `LogitsRows::kAllRows`，保留现有
`compute_logits`；显式 `kLastRow` 仅对 sample_hidden 最后一行
执行 lm_head，输出紧凑 `[1,vocab]`。sample/multi hidden、KV、
indexer 及之前的全部计算仍使用原行数。MtpDraftExtend 同样追加
默认全行参数；只有 HTTP 服务初始调用明确选择末行，legacy、CLI
和 Multi 步进调用保留默认行为，避免把临时分配误当作初始调用。

无 scratch 的初始调用把临时 logits 缩为一行，同步读取偏移；长
prompt 保持原分块，仅末块投影末行。使用持久 scratch 的小输入
仍保持原全行路径。预算保留现有保守估计，正式容量 208896/8192/S1
和 k=3 不变；不缩减主验证或多序列 extend 的多行缓冲。

不新增 KV-only 草稿路径、提前发送 token、prefix cache、offload、
精度开关、调度变化或第二种优化候选。旧 `build/runtime/` 保留，
新构建位于 `build/mtp-init-last-row-20261008/`，产物位于
`.q4t-work/mtp-init-last-row-20261008/`。模型与 reference 只读，
原两个 dirty 工作区、main 和默认二进制单独绑定并保护。

## 验证顺序与有界清单

本轮属于性能优化，**必要构建后的第一项测试为 tools/evalscope
HTTP E2E**，不使用 bench。上一阶段的数值诊断顺序不转借给本轮。

1. 候选 `quality --mtp` 固定 11 条，复用 quality-on-04 的输入及
   既有质量 reference；保存容量、实际路径、全部响应和退出信息。
2. HTTP 通过后，执行一次固定末行结构/数值组和一次完整 k3 回放。
   只因具体失败定向修复重验，不先后切换 prompt 或容差凑通过。
3. 合同组通过后，候选 `performance --mtp` 五档
   1024/4096/8192/45056/204800，各三次同输入输出 256。主比较为
   上一阶段 MTP-on 历史组，普通 off-04 仅作辅助；不传要求完全
   相同文本的 performance reference，逐档单列兼容性差异。
4. 固定采样完成后统一判定；不以不理想速度追加试验。HTTP 或
   数值具体失败保留原记录，若有界内不能解决则 NO_GO 收尾。

旧 MTP 基线为上一阶段 `http-performance-on-01`（二进制
`7fe34d6`），普通基线为 `performance-off-04`。候选另保存 SHA、
构建参数、运行源码与环境。端口 8132，启动等待 600 秒，每组由
runner 管理一个服务，单 Thor 串行，清除 Q4T_* 与 LD_PRELOAD，
不增加预热或探测生成。每组另启原 1 Hz 只读 /proc 采样器。

## 数值合同和覆盖边界

最后 lm_head 的 M 从多行变成 1，可能从 GEMM 切到 GEMV；BF16
输入/输出、FP32 累加与 BF16 RNE 不变，但不能预言逐位相同或 seed
不变。不得以短质量通过、near-tie 或事后放宽容差代替验证。

固定结构组使用两层主模型一次 16-token prefill 的真实 trunk，
MTP 分块 C=8、scratch 容量 4，T={1,4,8,9,10,16}，固定前缀。
在同一候选二进制中比较保留的 all-rows、显式 last-row 和实际
wrapper，始终保持相同分块，不冒称旧机器码对照或 one-shot 等价。
全 sample/multi hidden、KV/indexer 与返回 trunk 须精确一致；
compact 输出设置前后 canary，wrapper seed 由其实际分派 logits
的独立 host argmax 核对。默认 wrapper 与 scratch 原路径同样检查。

T=1 与 T=9 的末块本来为一行，logits 须逐位相同。T=4/8/10/16
的四个实际末行，用全词表 FP64 dot/sum_abs、事先固定的
gamma_(K+2)(2^-24) 累加包络、FP64 自身误差及向外 BF16 RNE
端点核对。只分块读取已加载 lm_head，不保存或哈希权重载荷。
finite/normal-or-zero/product-grid/overflow 前提不满足就报合同
域外，不倒推阈值。此组是有限输入和小分块结构验证，不外推正式
8192 行 GEMM 或整模型误差上界。

完整 k3 回放复用上轮固定 1K prompt、16 自然加 1 强制步骤，唯一
调整是初始 DraftExtend 显式末行模式；保持全 logits/trunk/状态、
选择与 raw checkpoint 检查，不自适应追加步骤。原完整模型跨模式
分叉与旧 admission 失败继续保留，不以本轮候选验证改写历史。

## 收益、资源和出口

TTFT/整请求取三次算术均值，decode 用
`765 / sum(latency - ttft)`，全程吞吐另列；保留首条与后两条范围。
主比较旧/新 MTP，每档候选后两条若在慢方向超出旧后两条较慢边界，
不能声明严格无回退；不临时设置任意容忍百分比，也不把此筛查当
显著性检验。普通基线与旧/新 MTP 分列，不混算。

RSS、VmHWM、MemAvailable 和采样范围单列；历史极值之差不是受控
物理 RAM 增量，静态分配字节不等于整机实测峰值。旧新输出不同则
生成计算也可能不同，不能把时延变化全部归因于单一 head 优化。

仅在实际数值合同、HTTP 质量与各档性能均满足冻结条件时接受这个
候选；候选接受也不等于解决之前 MTP 对普通模式的全部启用缺口。
否则保留具体失败/限制，明确实验分支状态，以 NO_GO 收尾。每个
清晰阶段提交推送同一工作分支，不创建重复工作分支。

## 实现与构建快照

三个运行文件已完成上述唯一候选，独立 Release 构建零警告；候选
服务 SHA `73c39bab73e729adfe288db0fdb09c9e9a642bd61afdc753f758013c80ff6a8c`，
模型库 `454a7cd4`。`candidate-identity.json` 绑定 123 个运行源码，
构建日志、配置和精确 runtime patch 保留。当前仅构建完成，所有
HTTP/数值/性能结果尚待执行；这个提交不表示优化已被接受。

## 首次 HTTP 与直接合同结果

按冻结顺序，第一测 quality-on-01 的 11/11 HTTP 固定题通过，
实际 MTP 路径、正式容量和退出检查通过；runner/server/sampler
均正常退出 0。随后测试二进制 `ae37f1bc` 零警告，服务/模型库/
构建配置身份保持上述候选，未重跑质量。

`mtp_init_last_row` 首次通过，六形状的 12 manual 加 12 wrapper
路径、完整上游 hidden/cache、输出 canary、scratch 全行写出及
借用 trunk 不变均精确通过。四个末行的 248,320 词表输出/路径均
在预定义包络内，两个本来 M=1 的对照逐位一致：

| T | 跨形状不同元素 | 最大绝对差 | All/Last 包络外 | All/Last seed |
|---:|---:|---:|---:|---:|
| 4 | 360 | 0.03125 | 0 / 0 | 96365 / 96365 |
| 8 | 312 | 0.03125 | 0 / 0 | 96756 / 96756 |
| 10 | 276 | 0.0625 | 0 / 0 | 21 / 21 |
| 16 | 384 | 0.03125 | 0 / 0 | 220 / 220 |

这是实际有限输入的计算/分块合同，不外推正式长矩阵或完整模型。
本夹具先 reserve scratch，T=1/4 wrapper 覆盖已存在 scratch 情形，
没有独立测试首次小 prompt 的无 scratch 临时路径。

完整 k3 初始调用显式 Last 后首次通过：16 自然步骤的 count=1/2/3/4
分布仍为 2/1/2/11，另一次强制拒绝通过；17 次 logits/trunk/接受
后 recurrent state 同形回放逐位相同，三行 raw checkpoint 通过。
两组证据及身份见阶段目录 `group.json`；下一步唯一五档测量。

独立 HTTP 包装器还修正了异常退出时 owned process group 的清理，
纯 host 三项检查通过，无模型调用。质量使用的原 wrapper 精确
归档（SHA `79dd7994`）；性能使用修订版（`93175ecc`），版本与
检查记录见 `wrapper-version-history.json`。核心 evalscope runner
及候选运行时未变，不因工具异常清理加固重复已通过的质量测试。
