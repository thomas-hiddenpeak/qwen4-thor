# MTP 性能分支归并候选与验收边界（2026-10-16）

## 范围与决定

采用替换方案 A：T>1 batch verifier 为默认 MTP（k=3），
`--no-mtp` 为普通 decode 基线；sequential 只保留于历史 `7f6a69c`。
main 尚未移动。本候选将 main 合入 `codex/moe-residency-20260930`，
不新建阶段分支。归并前工作分支回退点 `577dac0`。

验收域为本机 Thor、既定只读 checkpoint、纯文本 greedy、单流 k3、
max_prefill=8192/max_len=208896、五档记录边界输入、256 输出、各三次。
不扩展到媒体、任意 k、并发、真实业务或 CLI generate。

## 恢复后解决的实际缺陷

1. checkpoint 写端和快照端按 reserved capacity stride，合入的恢复端
   却按 saved rows stride。在 cap>T-1 时恢复错误层状态；统一 capacity
   stride，保留 written rows/slot 有效性与过期拒绝。
2. 普通 scheduler 的 ModelDecodeBatchMulti 缺 RoPE 当前 decode 位置。
   保留 main 修复。旧 1K 输出与修复后不同；只撤此写入的单请求消融
   完全恢复旧输出摘要。直接 CUDA 位置合同覆盖 delta=0/5、其余位置不变。
3. 不随 verifier 替换丢掉 main 共用保护：prefill/step 关停入队，
   load/scratch 失败所有权，host/device 位置复制，step 完成等待/失败结果
   清空，首可达 stop 不消费、不 extend，输出/上下文尾部普通 decode。
4. 合并的 response ID 校验使旧合成 runner 夹具失效；补合法 ID 并增加
   missing-ID 拒绝用例，保留生产严格校验。此项不改变模型二进制。

## 证据身份与复用

最终候选与源文件摘要：
`.q4t-work/mtp-mainline-ready-20261016/freeze.json`。
中断恢复、失败与消融：`.q4t-work/mtp-resume-20261016/`。
原失败：`.q4t-work/mtp-merge-verify-20261015/tests-root-01.log`，
归并前对照：`.q4t-work/mtp-merge-e2e-20261015/`。

- 近 tie 修复后 256 步：4 flips、0 双侧 confident、causality PASS。
  后续仅修改 MTP 草稿/服务边界与所有权；该测试调用的 model/算子代码
  未再变化（resume-identity.json 中 model.cu/linear_attention/PLE/MoE
  decode 摘要与冻结候选一致），因此复用数值证据，不为服务边界改动
  重跑相同 256 步。
- host 56/56、位置复制 18/18、checkpoint 布局、multi-step 与非法结果
  sentinel 均通过。生命周期覆盖实际 CUDA 分配失败：scratch 22 边界、
  checkpoint 3 边界、真实 load 第8/39次分配失败与重试、两种 stream。
- 中间版本 off 1K 的 reference mismatch 如实保留；off-remaining 的
  人工停止单独标记，不能用作最终性能通过。最终矩阵重新使用同一 binary。

## 最终验收结果

同一冻结 binary `26639190711e…`，五档各3次，所有 HTTP/输入/输出/finish
合同通过；每档每侧三次逐字节确定。MTP 五档输出与归并前一致。

| 输入档位 | off decode tok/s | on decode tok/s | 增益 |
|---|---:|---:|---:|
| 1024 | 18.706 | 34.535 | +84.62% |
| 4096 | 17.942 | 35.480 | +97.75% |
| 8192 | 18.175 | 37.330 | +105.39% |
| 45056 | 17.889 | 35.855 | +100.43% |
| 204800 | 17.163 | 33.136 | +93.07% |

五档均满足 +50%～+200% 目标。对归并前 on 的绝对速度，45K 从约36.61
降至35.86（约−2.1%）；不宣称每项无回退，也不以三次测量推导微小变化
的因果。TTFT 同时记录在 summary.json，MTP 有前置草稿开销，本结论仅为
decode 提速，不是 TTFT 改善或业务 SLA。

普通 decode 五档文本与旧版不同，保留正确 RoPE，不使用旧错误输出作为
数值 oracle。固定检索 off/on 均11/11通过（含200K、stop终止）。HTTP尾部六请求通过：
输出上限1/2/4/5、上下文耗尽及后继请求；流式首内容后显式取消返回202、
SSE错误而非正常length结束，随后请求成功；服务退出0。

本机公共 host CTest 19/19、严格 runner16项、证据审计4项通过；
GitHub push/PR CI（a3f90a6）均通过。公开CI不运行GPU诊断。

## 遗留失败与合入结论

**用户指定的归并方案、五档性能复确认及来源对照已完成；不能据此宣称
全量GPU合同通过或无条件批准main合入。交付可审查候选，main未动。**

恢复时9项失败中，checkpoint容量和256步数值合同已修复；sequence API
独立通过；partition按完整指定环境复验通过。剩余5项仍失败，保留测试、
阈值及首次失败，不把2层诊断自动归为允许的near-tie：

| 测试 | 最新结果及边界 |
|---|---|
| model_verify_ckpt | 修正过期夹具、等待低层batch后，a=1 L2=0.02632 > 0.02 |
| model_verify_multi_ckpt | 重建两序列并重新保存checkpoint后，a=1 L2=0.02632 > 0.02；与归并前记录相同 |
| model_verify_ckpt_samepath | round-trip、save/no-save、同路径确定性逐位通过；显式完成后partial L2=0.02632 > 0.02，full=0；仍FAIL |
| model_prefill_batch | 三序列L2=0.09737/0.05924/0.16509，首序列argmax不同；与归并前相同 |
| mtp_spec_accept_states | partial/full L2=0.04154/0.07696 > 0.02；与归并前相同 |

前两个旧测试在end/begin/prefill后恢复旧row，违反新增stale拒绝合同；已修正
为独立重建并保存，不撤回生产保护。单序列与samepath低层调用补显式完成，
将原0.12970/0.26336分别缩小至0.02632/0；只证明该测试的异步比较前提有影响，
不宣称已证明所有低层调用都安全。HTTP T>1使用的ModelVerifyMulti自身等待
完成；冻结生产源码与binary未变，故不重复五档。

这些诊断覆盖2层低层ModelDecodeBatch、B=2/3或旧MtpSpeculativeStep；
本轮HTTP为完整模型、B=1、ModelVerifyMulti/MtpSpeculativeStepMulti。
它们早于本轮已失败，但“历史存在”不等于已证明正确；完整模型的N=4
合同也不能代替它们。本轮指定验收已完成，不将未经约定的全量GPU零失败
新增为本轮门槛，也不扩大通过范围。后续针对这些路径应建立独立参考，
区分计算错误与合同适用范围。不能仅将
L2阈值抬高、删除测试或用检索分数证明它们通过。

## 可审查证据

仓库封存：[证据目录](evidence/mtp-mainline-ready-20261016/README.md)。
`summary.json`含五档、TTFT、质量和尾部结果；`final-identity.json`区分冻结
运行程序与随后仅改测试的二进制；`raw-manifest.json`记录581个本机原始文件
的SHA256/大小，完整请求、响应、原始DB留在原路径。未把6.6MB合并补丁重复
塞入仓库，合并提交本身承载源码。证据复用不等于重跑或全量通过。

## 主流对照与数值责任

见 [EVALUATION](EVALUATION.md)。本项目对 vLLM 默认非 batch-不变模式
增加 N=4 BF16 ULP 与 causality 负控的显式边界；不声称强于显式
batch-invariant 或 LLM-42 的确定性保证。来源解释差异机制，不证明
本项目正确，也不允许以 near-tie 掩盖 checkpoint/RoPE 等错误。

## 回退

main 保持 `7f6a69c`；原工作分支 `577dac0`、顺序 verifier 历史及原始
证据全部可达。当前归并提交采用两父历史，审查/提交与验收通过分开记录。
不要为回退删除原证据或重置其他工作区。真正合入 main 时另按实际合入
提交记录撤销方式，本轮不自动合入或部署。

## 复验入口

```bash
cmake --build build --target q4t q4t_tests q4t_host_tests \
  q4t_mtp_position_copy q4t_mtp_lifecycle --parallel
./build/q4t_host_tests
./build/q4t_tests mtp_checkpoint_capacity_layout
./build/q4t_tests mtp_spec_multi_
./build/q4t_mtp_position_copy
# 第一参数必须是不存在的新目录；模型路径只读。
./build/q4t_mtp_lifecycle .q4t-work/new-lifecycle-fixture \
  /home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream
```

完整 GPU 注册表包含历史跨形状诊断和专用环境实验；不将无筛选的一次
`q4t_tests` 输出冒充本轮正式门禁。旧失败不删除、不改阈值。五档使用
`tools/evalscope/run_acceptance.py --mode performance`，off 不传 MTP
参数（工具显式传 `--no-mtp`），on 传 `--mtp-default`；固定记录边界夹具、
各三次，启动超时300秒。保存二进制与源码摘要、请求/响应、退出状态及原始 DB。
