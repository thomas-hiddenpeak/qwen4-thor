# 主干 MoE 固定 IDs 诊断 A1

状态：实施中，尚无 GPU 或 HTTP 验收。2026-10-19 冻结初始范围。

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
