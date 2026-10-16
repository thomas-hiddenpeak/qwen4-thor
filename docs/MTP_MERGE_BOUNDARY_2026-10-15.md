# MTP 归并边界决策（2026-10-15）

本文件记录将当前工作分支 `codex/moe-residency-20260930`（下称**当前分支**）的
MTP 性能成果推进到可合入主线 `main` 的归并边界决策。代码为实现事实来源；
验收规则见 [EVALUATION.md](EVALUATION.md)。

## 1. 两套 MTP 实现对照

两分支自 merge-base `8ea85b1`（早于 main 的 MTP 交付 `7f6a69c`/PR #3）各自独立
演化，对同一组 8 个文件产生分叉改动（`src/mtp/mtp.cu`、`chat_generation.cpp`、
`chat_scheduler.cpp`、`chat_server.cpp`、`server_options.cpp`、`chat_server.h`、
`server_options.h`、`server_options_test.cpp`）。

| 维度 | main：sequential verifier | 当前分支：T>1 性能路径 |
|---|---|---|
| 入口 | `serve --mtp`（默认关，`use_mtp=false`） | 默认开（`use_mtp=true`，k=3），`--no-mtp` 回退 |
| verify 算术 | target 复用**普通 scheduler B1** 算术（`ArgmaxBf16Rows`，与普通 decode 逐位同算术） | 一次 **T=k+1**（k=3→T=4）batch 前向 |
| 与 plain 关系 | target 与 plain **逐位一致**，无数值分岔 | T=4 vs T=1 算术不同，存在**近 tie 分岔**（合同内） |
| 数值合同 | 严格 sequential（bit-identical to plain） | 近 tie 合同（N=4 ULP，256 步 PASS） |
| 性能（五档 decode） | 低 10.057%–12.032% | +85.0%/+97.5%/+107.4%/+104.7%/+92.9% |
| 质量 | 原 8 题 4 题语义不足仍保留 | 五档 E2E 质量不回退 + 近 tie 合同 PASS + 45K 路径六轮 bit-exact |
| 定位 | 可显式启用的**正确性参考** | **默认部署状态**（目标达成） |

## 2. 根本权衡

差异本质在 **verify 阶段算术路径**：
- **main sequential**：target 用 B1 算术（与 plain 相同）→ target 输出与 plain 逐位
  一致，无数值分岔；代价是 T=1 顺序 verify，慢 10–12%。
- **当前分支 T>1**：target 用 T=k+1 batch 算术（与 plain 不同）→ 存在近 tie 分岔；
  收益是 T=k+1 batch verify，快 85–107%。

即 **正确性（bit-identical）vs 速度（near-tie 分岔）** 的权衡。

## 3. 主流引擎对照（2026-10-16 来源复核）

vLLM 默认模式明确记录 batch 变化可能引起数值差异。本项目额外规定
N=4 ULP、双侧 confident 拒绝和 causality 负控，在这个意义上边界更严。
NeurIPS 2025 与 LLM-42 解释浮点非结合性机制；LLM-42 本身追求确定性，
不能作为“所有主流实现都允许近 tie 翻转”的依据。本合同也不比显式
batch-invariant/确定性模式更强。来源链接与比较范围见
[EVALUATION.md](EVALUATION.md#近-tie-合同与主流引擎对照2026-10-16-来源复核)。

## 4. 归并策略（推荐 A）

**以当前分支 T>1 性能路径为默认 MTP，替换 main 的 sequential verifier 默认地位。**

理由：
1. 当前分支 MTP 已达默认部署状态（五档 E2E + 近 tie 合同 + 45K 路径六轮 bit-exact）。
2. main sequential MTP 性能不达标（慢 10–12%）、默认关、仍有质量项未闭合，
   仅作为正确性参考。
3. 近 tie 合同与主流引擎行为一致且更严，可作为数值验收合同。

三选一：
- **A. 替换（推荐）**：当前分支 T>1 MTP 成为唯一默认 MTP；main 的 sequential
  verifier **不并入当前分支**（其与普通 scheduler B1 算术强耦合，并入需重写
  `mtp.cu` 双路径，复杂度高），保留在 main 历史中作为正确性参考。归并后默认 MTP
  = T>1。
- **B. 并存**：两套 MTP 均保留，T>1 默认开、sequential 默认关（`--mtp-verifier
  sequential` opt-in）。维护成本最高，需解决 `mtp.cu` 双路径冲突。
- **C. 废弃 sequential**：删除 sequential verifier，仅保留 T>1。最简洁，但失去
  主线上的正确性对照入口。

**推荐 A**：归并后默认 MTP = T>1；sequential 不并入（避免 `mtp.cu` 双路径重写），
其正确性参考价值保留在 main 提交历史中。若后续需要 opt-in 正确性对照，再单独立项
评估 B 的并入成本。

## 5. 验收影响

- 数值验收合同：由「严格 sequential（bit-identical to plain）」变为「近 tie 合同
  （N=4 ULP）」。
- 默认 MTP：由关变为开（k=3）。
- 性能验收：五档 decode +50%~+200%（当前分支已达成 +85%~+107%）。
- 质量验收：五档 E2E 质量不回退（当前分支已达成）。
- sequential verifier 原有验收证据不变，仍归属 main 历史。

## 5b. 归并范围实测（2026-10-15）

两分支自 merge-base `8ea85b1` 以来：main 改动 **704 文件**（含大量 docs/tests），
当前分支改动 **203 文件**。实际**代码**冲突范围（非 docs/tests）：
- MTP 相关 8 文件（见 §1）：以当前分支 T>1 版本为准。
- 非 MTP 代码约 **32 文件**：`moe_gemm.cu`(+468)、`memory_budget.cpp`(+398)、
  `model.cu`(+218)、`linear_attention.cu`、`full_attention.cu`、`moe.cu`、
  `weight_loader.cpp`、`scheduler_submission.h`、`moe_gemm_test.h` 等。这些是 main
  自 merge-base 以来的独立演化（MoE GEMM、内存预算、模型层），当前分支亦有部分
  改动（MoE residency），需逐文件核对取并集或按功能归属取舍。

归并复杂度因此高于「仅 MTP 8 文件」的初判；建议归并时按**功能归属**分文件裁决
（MTP 路径取当前分支，MoE/内存/模型层以 main 为准并核对当前分支 residency 改动
是否已包含），而非整树覆盖。

## 6. 可回退归并计划

1. 归并前 main 保持不动（回退点 = 当前 main HEAD `7f6a69c`）。
2. 以当前分支为基础，将 main 自 merge-base 以来**与 MTP 无关**的改动（若有）
   选择性并入；MTP 相关 8 冲突文件以当前分支 T>1 版本为准。
3. 构建零警告（`-Wall -Wextra`）。
4. 复确认当前分支五档 E2E 性能基础（off=`--no-mtp` vs on=默认 T>1，各 3 次，
   decode_tps 保持 +50%~+200%、确定性、合同全过）。
5. 阶段快照 commit + 推送 GitHub 工作分支，明确标记未通过项。
6. 归并失败可回退到归并前状态（main 未动）。

## 7. 已采用的边界

归并树采用 A，默认 T>1、k=3；sequential 保留于历史提交 7f6a69c。
不新增双路径产品入口。A 与 C 在当前运行树的删除结果相同，A 明确保留
历史回退/参考入口。main 保持不动，先交付工作分支的可审查合并提交。

## 8. 逐文件归并裁决（2026-10-15，Step 2 复确认期间完成分析）

试合并（`git merge-tree --write-tree HEAD main`）实测冲突 27 文件。main 自
merge-base 的 52 提交几乎全部是 MTP 主线交付（sequential verifier + 准入 +
诊断）；唯一实质非 MTP 提交为 `e6b4bc0`（offload 研究工具 + 常驻启动正确性
修复：ShardHandle 重构、内存预算加固、weight_loader 契约测试、physical_ram
等）。裁决原则：MTP 8 文件取 cur（T>1）；main 的 sequential 专属工件从
归并树删除（保留在 main 历史 `7f6a69c` 可达）；非 MTP 代码按功能归属取并集。

### 8.1 MTP 核心（cur 版本为准）

| 文件 | 裁决 | 说明 |
|---|---|---|
| src/mtp/mtp.cu | cur | T>1 路径；main 的 T4+sequential 被替换 |
| include/q4t/mtp/mtp.h | cur（=merge-base） | main 对既有函数签名加参（init_timing/max_position/mode/logits_rows），cur mtp.cu 实现旧签名，取 main 头会链接失败 |
| src/server/chat_generation.cpp | cur | T>1 集成 |
| src/server/chat_scheduler.cpp | cur + 取 main 关停重置 | 取 main 关停时 `mtp_accepted_count/mtp_next_b/mtp_next_d0` 重置（字段 cur 已有）；删 sequential 分派与 timing 钩子 |
| src/server/chat_server.cpp | 手工并 | 预算路径两侧独立演化：cur 侧 residency 字节 + T>1 MTP 预算保留；main 侧 sequential env 检查/verifier 删除；取 main 预算注释修正（"Allocation estimate...not a physical RAM/OOM guarantee"） |
| src/server/server_options.cpp | 手工并 | cur 选项（moe-resident-slots/moe-hot-list/moe-hot-protect/request-deadline-ms/mtp-k）保留；删 `--mtp-verifier`；取 main 的 max_prefill≤8192 校验 |
| include/q4t/server/chat_server.h | 手工并 | cur 的 err/residency 观测/request_deadline_ms 保留；删 main 的 sequential 字段（mtp_sequential_result/mtp_cycle_step/mtp_verify_moe_step/mtp_output_remaining）与 MtpVerifier 成员；取 main 预算注释修正 |
| include/q4t/server/server_options.h | 手工并 | 删 `MtpVerifier` 枚举与 `EffectiveMtpVerifier`；两侧选项字段取并集 |
| tests/server_options_test.cpp | 手工并 | cur 选项测试 + main 非 MTP 测试；删 verifier 测试 |

### 8.2 非 MTP 代码（手工并）

| 文件 | 裁决 |
|---|---|
| src/quant/moe_gemm.cu | cur 的 `MoEDeviceDecodeMulti`（M=2..8，T>1 性能基础）+ ablation env 保留；取 main 的错误处理加固（fail() 清理语义）；删 main 的 M=4 batch gather（sequential T4 专属、env 门控）、replay capture、verify MoE timing 钩子 |
| src/model/moe.cu | cur（main 变更纯 verify MoE timing 测量，随 sequential 删除） |
| src/model/model.cu | cur 的 residency 保留；取 main 的 checkpoint 加固（save_checkpoints 校验/容量检查/越界安全比较/seq_id 校验）——cur 的 T>1 路径同样调用 save_checkpoints=true；RoPE 多数修复 cur 已有，但 ModelDecodeBatchMulti 写位置缺失须保留 main 修复（2026-10-16 核实） |
| src/runtime/memory_budget.cpp | 两侧并：overflow 安全算术（Add/Mul 两侧独立新增，去重取一份）+ main 的 MTP 预算函数（按 T>1 适配）+ cur 的 residency 感知预算（KV 16 对齐、MTP rope 翻倍、has_ple 守卫） |
| include/q4t/runtime/memory_budget.h | 两侧并 |
| src/io/weight_loader.cpp | cur（main 变更的超集：相同 ShardHandle/metadata 重构 + num_shards + SetKeepPageCache） |
| include/q4t/io/weight_loader.h | cur（超集：+ShardOf/ReadRange/ReadRangeShard/ReadRangevShard） |
| src/main.cpp | cur（generate 保持 MTP 默认开；删 main 的 generate 拒绝 MTP 与 --mtp-verifier） |
| src/model/full_attention.cu | 自动合并后核对 |
| src/model/linear_attention.cu | 自动合并后核对 |
| src/mtp/moe_bf16.cu | main（cur 未改） |
| tools/evalscope/run_acceptance.py | cur 语义（--mtp/--mtp-default/--mtp-k、offload 矩阵、memory-phase）；删 main 的准入 harness（admission-limit cases、sustained pair budget、mtp_mode 依赖） |
| tools/trace/*（28 文件两侧均改） | 按文件取并集（两侧 offload 研究独立演化，冲突为同区新增） |
| tests/budget_host、host、io_host CMakeLists | 两侧并 |
| tests/runtime_memory_budget_test.cpp | 两侧并（main +474 / cur +212 预算测试） |
| CMakeLists.txt | cur 测试集 + 删 sequential 工件引用（mtp_sequential.cpp、init_timing.cpp、mtp_*_timing.cpp、sequential 测试与 fixture） |
| docs/* | 手工并（STATUS.md 归并后重写当前状态段） |
| .github/workflows/host-checks.yml | 自动合并后核对 |

### 8.3 删除清单（main-only MTP sequential/准入工件）

- src/mtp/mtp_sequential.cpp、src/mtp/init_timing.cpp
- src/trace/mtp_cycle_timing.cpp、src/trace/mtp_verify_moe_timing.cpp
  （及对应 include/q4t/trace/*.h 若为 main-only）
- include/q4t/quant/moe_gemm_test.h（若仅 capture 使用）
- tools/verify/mtp_scheduler_fault.cpp.in
- tools/evalscope：mtp_mode.py、mtp_failure_recovery_contract.py、
  mtp_t4_completion_contract.py、mtp_t4_terminal_contract.py、
  run_mtp_boundaries.py、run_mtp_failure_recovery.py、run_mtp_t4_completion.py、
  run_mtp_t4_terminal.py、test_mtp_*.py、test_t4_*_contract.py、
  analyze_mtp_*_timing.py、acceptance_mode.py、test_acceptance_mode.py、
  admission_limits_reference.py、test_admission_limits_reference.py、
  sustained_quality.py、test_sustained_quality.py、prepare_sustained_quality.py
- tests：mtp_sequential_admission_test.cpp、mtp_sequential_control_test.cpp、
  mtp_sequential_fault_server.cpp、mtp_t4_completion_fault_server.cpp、
  mtp_t4_terminal_server.cpp、mtp_admission_test.cpp、mtp_lifecycle_test.cpp、
  mtp_cycle_timing_host_test.cpp、mtp_verify_moe_timing_test.cpp、
  mtp_known_position_test.cpp、mtp_init_tail_test.cpp、mtp_init_last_row_test.cpp、
  quant_moe_batch_gather_test.cpp、support/mtp_sequential_state.h
- 待编译判定（测试共享 MTP 基础设施，编译+通过则保留，失败则删除并注明）：
  mtp_checkpoint_layout_test、mtp_k3_replay_test、mtp_moe_path_test、
  mtp_projection_path_test、mtp_path_triangle_test、mtp_position_copy_test、
  mtp_spec_multi_test、mtp_hc_first_fork_test、server_mtp_policy_test、
  server_scheduler_submission_test、support/mtp_hc_dot_envelope.h
- 执行修正（2026-10-15 验证期）：request_cancellation_contract.py 与
  test_request_cancellation.py 原判"保留"有误——合同模块 import 已删除的
  acceptance_mode.py，且 32 项合成测试中 15 项为 MTP 路径/sequential 恢复
  会计（依赖 mtp_sequential_b1/mtp_multi_b1 路径名），归并树无法通过，
  随 sequential 工件删除；非 MTP 部分（run_request_cancellation.py 及其
  自包含合同）保留。
- 保留（main-only 非 MTP）：physical_ram.py、test_physical_ram.py、
  response_identity.py、test_response_identity.py、
  run_request_cancellation.py、model_decode_rope_test.cpp、
  weight_loader_contract_test.cpp、tools/trace 的 gpu_cache_replay/
  decode_supply/mirror_retention 研究工具及其测试

### 8.4 执行步骤

1. `git merge main`（base=cur HEAD `03339bf`），按 §8.1–8.3 解冲突。
2. 删除 §8.3 清单；CMake 同步。
3. 构建零警告（-Wall -Wextra）；全部测试构建，按 §8.3 末行编译判定。
4. 归并树五档 E2E 复确认（off=`--no-mtp` vs on=默认 T>1，各 3 次）。
5. 阶段快照 commit + 推送工作分支；main 不动（回退点 `7f6a69c`）。
6. 归并树验收通过后，main 快进/合并到归并状态（sequential 工件随归并树
   删除，历史保留于 `7f6a69c`）。

## 9. 2026-10-16 恢复与裁决修正

上次解冲突已完成但合并未提交，不能称为验收完成。归并前 HEAD 实际为
577dac0（03339bf 之后的方案/证据提交），MERGE_HEAD 为 7f6a69c。

- 当前分支 checkpoint 写端已使用 reserved capacity stride；main 的
  restore saved-row stride 不能直接保留。统一写/读/快照 capacity stride，
  同时保留 main 的有效行/slot、过期/越界拒绝检查。
- 当前 T>1 支持单序列 PLE checkpoint。main 专属拒绝预期改为成功与
  有效行检查；保留独立 capacity=1 vs 3、saved_rows=1 状态/探针比对。
- 不按“失败就删除”清理共享合同测试：记录基线失败、语义不适用与新回归
  的区别，只有明确专属 sequential 的预期随实现边界适配。
- 最终结果见 STATUS 与当天日志；上次失败证据原样保留。

### §9 补充：共用正确性边界（最终候选冻结）

策略 A 替换的是 verifier 算术，不删除共用正确性保护。§8 中“核心整文件
取 cur”及 lifecycle 专属判定由本节修正：

- 保留 main 的 load/scratch 失败清理、容量发布、借用权重不释放；恢复
  `mtp_lifecycle_test.cpp` 与显式 target，覆盖实际 CUDA 分配失败注入。
- MtpForward 使用共用 UVA position-copy helper；multi step 每个已提交
  Status 出口完成 stream 后返回，失败清空输出，scheduler 标记 Failed。
  draft 位置上传源为不可变逐步切片；不改变 T>1 kernel 或精度阈值。
- 保留首可达 stop 的前缀恢复与禁止 terminal extend；server 在 pending
  stop 处结束，输出/上下文尾部转 ordinary decode，保留已输出前缀。
- 保留 prefill 关停入队原子性与普通/MTP decode 的关停判断和结果清空；
  不把仅测试共用 helper 误写为运行时已接入。
- 原 off 的普通 scheduler B1 缺 RoPE 当前位置写入；修复后的 off 输出
  有意区别于旧错误版。1K 消融只删此写入即恢复旧摘要，旧版只作兼容对照。
  最终性能必须重新以同一修复后 binary 的 off/on 五档三次比较。
- 旧 cross-shape 固定 L2 阈值失败不自动按 near-tie 免检，也不改阈值；
  与本轮直接布局/ULP/HTTP 合同分栏报告，保留原始失败与其基线证据。

### 公共取消合同修正

§8.3 的整文件删除由 CI 首败纠正：保留 request_cancellation_contract.py
与 test_request_cancellation.py 的21项通用格式/计数/fixture/HTTP读取合同，
删除依赖旧 decode_path/verifier 会计及 acceptance_mode 的11项专属测试。
CMake 的公共合同入口继续保留，本机21项通过；不等同于新增真实取消场景验收。

### §9 最终验收与未闭合边界（2026-10-16）

冻结矩阵五档、两侧检索、尾部/取消及公共CI通过，精确证据见
[MTP_MAINLINE_READY_2026-10-16](MTP_MAINLINE_READY_2026-10-16.md)。
5项GPU诊断仍失败，测试夹具修正未放宽阈值；不能把历史失败或性能达标
当成它们通过。本轮指定HTTP验收完成，PR转待审查、main不移动；
§8.4第6步尚未执行。
