# MTP 性能分支归并候选（2026-10-16）

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

五档性能、11题检索 off/on、尾部 HTTP 与遗留诊断分类正在执行，完成后回填。
在回填前，本报告不是 main 合入批准。

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
