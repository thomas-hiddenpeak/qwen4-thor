# 主干 MoE 固定 IDs 诊断 A1

状态：**DIAGNOSTIC_CONTRACT_PASS（2026-10-19）**。主干最小固定 IDs
诊断完成；下方实施快照与“剩余”段保留过程历史，以本节最终结果为准。

## 后续驱动补强（2026-10-19）

用户要求进一步完善后，复审发现驱动仍读取原夹具且准备阶段/中断状态
不完整。现使用冻结fixture副本、逐阶段核对快照，保存准备失败与Ctrl+C
终态。5项纯host假执行器测试通过，见[独立证据](evidence/moe-replay-driver-hardening-20261019/result.json)。
仅run.py变化，原result.json中的驱动SHA是历史验收身份，新的驱动SHA
见上述证据；CUDA与模型执行未改，不为此重复GPU/HTTP。真实自然文本
层输入捕获仍未实现，不将本次可靠性修复扩展为此能力。

## 最终结果与逐项完成核对

入口：[工具说明与完整复现命令](../tools/verify/moe_replay/README.md)。
独立 CLI `q4t_moe_replay`，库 `MoEReplayForward`，固定夹具
`tools/verify/moe_replay/fixture.json`。最终 binary `9a6f2e01…`；首次 GPU
suite binary `9833648f…`，两者完整 SHA 见[最终身份](evidence/moe-replay-a1-20261019/result.json)。
默认服务 binary 仍为 B1a `44abd8b1…`，未部署替换。

| 原目标 | 实现及证据 | 判定 |
|---|---|---|
| 基线、Megatron 和 A1 调用链 | 本文来源段、baseline.json；固定参考 SHA 与当前 B1b/B1a 分别核对 | PASS |
| 真实 MoE 选择点固定 IDs、当前分数 | 独立编译同一 MoEForward，保留 BF16 gating 和专家链；生产无 replay 分支 | PASS |
| 身份、完整性与生命周期 | model/6149个实际tensor/hidden/binary/token/层/位置/shape；严格文件、host 2/2、GPU 4次拒绝及恢复 | PASS |
| 同形状重放 | T4及四行T1，5组 IDs/weights/logits/MoE输出逐位一致；另5组与生产路径逐位对照 | PASS |
| 当前 logits 与 CPU 参考 | 固定 IDs 改分数，权重随之改变；double参考最大abs 4.87762835e-8 ≤ 2e-6；同分/近同分/0/511/slot顺序 | PASS |
| 跨 T1/T4 诊断 | 4行正常对照和固定T4 IDs均保留；本夹具ID/权重/output差异计数为0，不扩展为普遍batch不变 | RECORDED |
| 文件独立复现 | suite导出→另一进程读取重放逐位；最终binary显式兼容旧binary重放再次逐位 | PASS |
| 正常路径保持 | MoE全部可加载ELF段一致、SASS和host指令/重定位一致；新增目标未链接生产；HTTP证据复用 | PASS（原范围） |
| 可回退交付 | 固定夹具、CLI/API、文档、身份/原始记录及阶段提交；无默认部署 | PASS |

原始首次[suite日志](evidence/moe-replay-a1-20261019/gpu-v1/suite.log)、
[独立文件回放](evidence/moe-replay-a1-20261019/gpu-v1/replay.log)、
[guard退出](evidence/moe-replay-a1-20261019/gpu-lease-v1/exit.json)均通过。
全部19份保存的真实层输出已离线检查有限且非全零。

交付检查发现 CLI 漏拦截 Q4T_FP8_SHARED；首次执行环境未设置该变量，
并未使用FP8。补齐七项FP8入口检查和兼容差异落盘后，冻结
[受影响补验](evidence/moe-replay-a1-20261019/final-plan.json)：7次加载前
拒绝、1次层加载/前向，显式允许“仅CLI检查与身份说明变化”的精确
source→execution binary 对，结果逐位通过；[结果](evidence/moe-replay-a1-20261019/final-replay-result.json)、
[兼容说明](evidence/moe-replay-a1-20261019/final-replay/compatibility.txt)、
[最终guard](evidence/moe-replay-a1-20261019/final-lease/exit.json)。
没有重复完整suite或扩大数值阈值。两阶段各完整持锁；中间为离线检查，
不声称一个覆盖全开发过程的连续lease。两段均COMPLETED/child=0。

首次可执行文件在后续入口构建时被覆盖，随后用SHA完全相同的原源码
重构，得到同一完整binary SHA `9833648f…`，现独立保留；未凭源码推定
binary相同。驱动已改为启动前保存可执行文件和源码快照，避免后续构建
覆盖。原始记录及构建/恢复日志见[167项清单](evidence/moe-replay-a1-20261019/raw-manifest.json)。

### 正常路径与 HTTP 证据复用

唯一涉及生产源文件的改动在 moe.cu 编译条件内。与已接受 B1b 对象比较，
[全部可加载段](evidence/moe-replay-a1-20261019/production-sections.json)内容、大小、
flags/type一致；SASS和host指令/重定位一致。CMake新增目标均为独立
EXCLUDE_FROM_ALL目标，未修改生产库/服务的源列表、编译选项或链接依赖。
因此正常路径无新增分配、回读、同步或采集；trace collector和MTP未改。

[HTTP复用核验](evidence/moe-replay-a1-20261019/http-reuse.json)按原manifest
核对B1b on/off各11题质量及各五档3次记录的SHA/大小，保留确定性、输入
长度与原接受范围。新增HTTP请求0，不把诊断运行时间当性能数据。
B1b的统计性能仍为INSUFFICIENT_EVIDENCE，原工程接受风险不被本项改写。
N=4及状态合同的运行代码未变，沿用既有证据；本层测试不替代整模型质量。

### 边界与回退

本工具验证真实权重的合成层边界输入。token元数据明确固定，hidden不是
自然文本经过前序层的采集，不能据此解释任意生产生成分歧。当前CLI固定
层2/四行夹具；底层接口可接受经过调用方正确绑定的其他层记录。固定IDs
只隔离选择；当前权重与专家算术仍可能改变，必须分别检查保存的中间量。
未实现MTP草稿、完整trace、offload、预取、驻留偏置或Graph。

回退诊断交付可按逆序revert本阶段提交（ca52825、f520f6f及最终CLI交付），
保留证据；生产默认binary不需回退。不得删除依赖这些本地路径的证据。

---

以下为初始设计与执行历史。

## 来源与起点

通过 [归档索引](ARCHIVED_RESEARCH.md) 读取 `9a5bc0c` 的
`MEGATRON_INFERENCE_REVIEW_2026-10-16.md` 和综合计划 A1。
本地 Megatron 检出确认是 `3e2807ce5e787f81b7275be2383092fe0c3bfc71`。
重新读取 router_replay.py:219、router.py:1371、moe_utils.py:916：
REPLAY_FORWARD gather 当前 scores，gating 仍执行；post-selection softmax
在 gather 后执行。只借鉴机制，不引入 Megatron 依赖或运行其推理。

本项目源码起点 `047d8ee` 含 B1b；默认 binary 为 B1a `44abd8b1…`。
[身份清单](evidence/moe-replay-a1-20261019/baseline.json) 分别记录源码、
实际默认 binary、模型 config/index 和参考源码文件摘要。权重 payload、
候选 binary、测试 binary 身份待实现完成后补齐，不把 index 当权重摘要。
当前仅 main 有效，复用该分支；不替换默认 build/q4t。

实际链路：DecoderLayerForward → MoEForward → BF16 Bf16Gemm →
RouterTopkKernel → 原 NVFP4 routed/shared/combine。原 kernel 按分数降序、
ID 升序选取，再按 slot 次序 FP32 exp/sum/reciprocal/multiply。
RouterCollector::CaptureLayer 只观测原始 ID，不承担重放执行。

## 最小实现合同

- 独立诊断入口绑定真实层权重与固定输入；不增加服务参数或 MTP 接线。
- 原始有序 IDs 在选择点替代 top-k；BF16 gating 与后续专家计算保留。
  当前选中 logits 按原 FP32 算式和输入 slot 顺序计算权重，不排序 IDs。
- 记录包含模型 config/index 摘要、实际权重摘要、hidden 输入摘要、
  角色、层、完整固定 token 前缀、逻辑起始位置、行数、E、k。
  前缀必须覆盖 position+rows；不从旧 decode trace 位置猜测 token。
- 来源 binary 与执行 binary 分离。默认相同；跨版本仅显式精确摘要对
  与非空差异说明允许，模型/权重/token/位置映射始终必须一致。
- 使用前拒绝空身份、错层/角色/模型/权重/token/位置/形状、重复或
  越界 IDs、缺行、截断和多余数据；文件解析必须消费完整输入。
- 验证后的 IDs 拥有独立副本。诊断 GPU 上传、读取及结果导出必须有
  明确完成边界；错误返回也排空自己的异步工作后才释放所有权。
  正常入口不引入设备分配、回读、同步或采集。

## 冻结验证清单与预算

先构建和直接合同，成组验收；只有具体失败才重验受影响项。
生成产物统一 `.q4t-work/moe-replay-a1-20261019/`。

1. host 输入合同一组：各身份字段逐项负控、形状/ID/文件完整性，
   验证后修改来源 buffer 不影响快照。无 GPU，无请求。
2. GPU router 一组：固定 IDs 改 logits、独立 double CPU softmax 参考，
   同分/相邻 BF16 值、0/511、任意 slot 顺序；CPU 比较预定绝对误差
   2e-6，不以其代替同形状逐位门禁；非法 IDs 不允许进入 kernel。
3. 真实模型层一组：层 2，T=1 和 T=4，各正常记录及同形状回放一次，
   IDs、weights、完整 MoE 输出逐位相等。固定四行输入同时做 T=1
   逐行正常/固定 IDs 对照，跨形状差异完整报告，不要求跨形状逐位。
   检查源 buffer 复用、连续调用及错误清理；不借局部结果放宽 N=4。
4. 正常路径保护：优先使用独立诊断实例化，保留生产调用及 kernel。
   构建后比较精确父版本和候选生产设备/host 代码；若正常执行代码
   身份可证明不变，复用 B1b HTTP/数值证据，新增 HTTP 预算 0。
   若正常执行代码变化，测试前补冻具体 HTTP 质量/性能保护清单，
   不以当前 0 预算省略必需验收。此任务不宣称性能改善或统计持平。

第 2、3 项由同一完整驱动经 gpu_exclusive.py 持有单 lease。
无 skip 算通过；guard 与子进程退出均须通过。ENV_ABORT 保留现场，
仅终止自己的进程。所有首次失败保留；不扫描随机种子或阈值求通过。

## 交付与边界

最终应包含独立入口、固定夹具、严格文件校验、复现命令、源码/binary/
模型/权重/测试身份与结果报告，分开记录合同、HTTP、性能状态。
固定 IDs 可隔离选择差异；当前权重与下游计算仍可不同，不能仅凭
固定 IDs 将剩余差异全部归因于专家算术。保留正常路由对照。
草稿 MTP、完整 trace、预取/offload、驻留偏置及 CUDA Graph 不在范围。
按清晰边界提交推送；未验收快照明确标注，不自动部署。

## 2026-10-19 实现快照（非最终验收）

`q4t_moe_replay_gpu` 独立目标再次编译同一 MoEForward 函数体，
仅定义 `Q4T_MOE_REPLAY_DIAGNOSTIC` 时加入 replay kernel 和选择分支。
生产编译完全排除此代码，trace collector 未改。GPU wrapper 先验证
来源/执行身份及实际 hidden bytes，拥有 ID 上传副本；成功和失败均
等待自己的 stream 完成后释放，返回时调用方可复用输入记录/缓冲。
此版本仅支持全驻留，不将 offload 纳入验证范围。

`q4t_moe_replay_io` 使用版本化 little-endian 编码与 SHA256 尾部校验，
16 MiB 上限，角色/形状/数量显式编码，必须恰好消费完整 payload。
读取只信任来源字段作为待校验记录；执行身份由诊断 loader 重建。
校验和不等于模型身份可信，仍必须通过 Prepare 与 loader 对照。

独立 host 测试 2/2：身份、ID 顺序和副本生命周期；每个字节截断点，
checksum 错误、重算 checksum 后的非法结构和额外 payload。无 skip。
GPU 库及 host CMake 目标零警告构建；首次多目标构建因 Makefile 自动
重生成后旧 make 进程未识别新目标失败，单独构建成功，原日志保留。
[构建与比较证据](evidence/moe-replay-a1-20261019/implementation-build.json)。

与 B1b 原生产 MoE 对象比较，完整 SASS 相同，host 反汇编/重定位
除路径标题相同；原始对象摘要不同，不宣称对象逐字节一致。
最终生产路径复用还需结合数据段、链接依赖审查，不能单以指令相同
替代完整影响审查。新增 GPU/HTTP 运行均为 0。

当前可复现 host 检查：

```bash
cmake -S . -B .q4t-work/moe-replay-a1-20261019/build \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=g++-14 \
  -DCMAKE_CUDA_HOST_COMPILER=g++-14 -DQ4T_CUDA_ARCHITECTURES=110a
cmake --build .q4t-work/moe-replay-a1-20261019/build \
  --target q4t_moe_replay_gpu q4t_moe_replay_host_tests --parallel 8
Q4T_REPLAY_TEST_OUTPUT=.q4t-work/moe-replay-a1-20261019/io-user-check \
  .q4t-work/moe-replay-a1-20261019/build/q4t_moe_replay_host_tests
```

剩余：真实层独立 CLI/固定夹具及权重 payload 身份、GPU router 边界与
CPU 参考、同形状逐位/跨形状诊断、GPU 所有权负控、生产路径最终审核、
完整复现与结果报告。当前静态库构建不构成这些项目通过。

## 首次 GPU 阶段执行冻结

2026-10-19：在首次 GPU 执行前具体化原清单，不扩大模型层/形状范围。
使用仓库 `tools/verify/moe_replay/fixture.json`：真实层 2 权重，四行
确定性合成 BF16 输入；token 是显式夹具元数据，不宣称这些 hidden
来自完整模型前缀执行。权重逐 tensor 摘要含名称/dtype/shape，形成
加载身份；模型 config/index、实际 executable 另记。

单 lease 两个进程（suite + 独立文件 replay）、两次层加载：22 次完整
MoE forward（5形状各 production/normal/replay=15，4次跨形状，错误
恢复参考/重放=2，独立进程replay=1），3次合成 router kernel，4次拒绝
检查。HTTP 新增 0。每个形状包含 T4 及四个 T1 行；错误后复用有完成
边界和输出 sentinel 负控。CPU abs 2e-6、同形状逐位门禁不变。
实际命令/精确 binary 摘要冻结在 `.q4t-work/moe-replay-a1-20261019/plan.json`。
任何失败保留并停止，只定位实际缺陷后重验受影响项。
