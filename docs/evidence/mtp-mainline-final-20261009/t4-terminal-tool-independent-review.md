# T4 terminal 工具独立审查（2026-10-09）

结论：静态审查通过，可进入构建及冻结后的统一测试。本文不代表构建、
解析器测试、真实 CUDA 或 HTTP 已通过；审查期间没有执行这些工作。
审查者为 `/root/terminal_implementation`，被审工具作者为
`/root/terminal_control_variant`。本审查只读取工具与对应生产调用链；
没有修改生产代码或被审工具。

## 范围与核对结果

- 固定 9 个 HTTP 生成请求：4 个真实 T4 verify 后的 terminal 选择控制，
  stop 位于 row0/1/2/3；随后 max_tokens1/2/3/4/5。输入沿用既有固定
  1K 字节与摘要，不搜索或替换题面，不增加生成探针。
- wrapper 先执行真实 draft、T4 verify 及 argmax D2H，再改变 host 选择；
  保存原始与修改后的四行预测。只强制可达前缀匹配及指定 stop/correction，
  不改后续不可达预测；所需真实 draft 已提前 stop 时判 coverage 失败。
- terminal row0/1/2 对应真实 restore checkpoint0/1/2；row3不恢复。
  terminal 不做 extend，next_d0=-1；cap5执行真实四行 extend。
- Prefill wrapper 只观察并保存原始 1024 个输入 ID。End 前检查实际 history
  等于该 prompt 加真实已消费前缀；cap1 的零后缀也逐值检查。core 返回时
  host position/history 不变，随后请求线程按同一个前缀提交。
- cap1..4 不执行 T4；cap2..4 的普通 scheduler forward 次数为 cap-1。
  cap5执行一个完整 T4 步，最后 pending 输出不再提交为输入；End 的位置
  为 prompt+4。HTTP 合同独立核对实际 request ID、finish、usage、文本形状，
  并检查路径、正常尾部原因及每请求健康/资源/计数清理。
- 真正 ModelPrefill 的 ASM 包装符号已通过只读 `nm` 查询既有 M1
  `build/mtp-mainline-20261009/libq4t_model.a`，与现有实际定义逐字一致：
  `_ZN3q4t5model12ModelPrefillERKNS0_5ModelEPNS0_13ModelSequenceEPKiiPtP11CUstream_stS8_PKNS0_14VisionFeaturesEiNS0_10LogitsRowsENS0_18SequenceCompletionE`。
  其他新候选符号仍由正式构建验证，本次没有构建。
- driver 绑定自身、wrapper、合同、测试、链接配置、输入、候选 binary、
  build manifest、相关生产源码及 checkpoint metadata。生成请求只有固定
  9 次 POST；其余仅为 health/metrics GET，不做额外生成或自动重试。
- 请求组上限 1500 秒、单次请求上限 120 秒，startup 与清理轮询同受组期限
  约束。保留真实失败、原始响应、stderr 与 summary；正常 SIGTERM 后检查
  进程组，必要时 SIGKILL 并判失败。setup/import/identity 或 finalization
  异常在本轮拥有的新目录中保留失败 summary，不覆盖旧实验。

## 审查发现及修复

1. 初版 cap1 仅核 history 长度，却记录 history_exact=1。作者新增只读
   Prefill 观察，所有 End 均按真实输入与实际消费前缀逐值检查，已复核修复。
2. 初版解析反例把 row0 stop 之后不可达的 draft0 改为 stop，并错误期待
   拒绝。作者将不可达 stop 保留为正例，另在 ordinal2 已接受前缀设置 stop
   作为反例；已复核两者符合真实可达语义。

## 结论边界

本组是运行真实模型后的受控选择与接线合同，不能称自然质量、T4 数值准入
或性能验收。next_g 标为 invalid_not_read，没有额外读回证明缓冲逐位不变；
HTTP 没有独立逐 token ID 采集，最后 pending 的身份依赖生产接线、受控 core
及实际 HTTP 文本/计数，而非独立 HTTP token ID oracle。restore 的真实调用
和索引被观察，不把这些证据扩大为所有设备 cache 内容的独立数值证明。
进程组字段描述正常清理之后的观察；若需强杀，保留失败，不宣称正常退出。

## 受审文件当前完整 SHA-256

下表记录完成上述审查时的五个文件。若复制到 canonical 路径后字节未变，
本审查可按摘要复用；后续内容变更须明确受影响范围。

| 文件 | SHA-256 |
|---|---|
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/.q4t-work/mtp-mainline-20261009/t4-terminal-control/mtp_t4_terminal_server.cpp` | `b5ffe36151dfae3bf1cfe677de7cd443d2240f025e211dbf53fb5f17e405bcf4` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/.q4t-work/mtp-mainline-20261009/t4-terminal-control/mtp_t4_terminal_contract.py` | `23facf809e32670981eedc919afe4b814163bebfcc77b8455a39c315ff078dc2` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/.q4t-work/mtp-mainline-20261009/t4-terminal-control/test_t4_terminal_contract.py` | `933bfc8e5be2f8f8878adac6aaf2b5af17dedae5a189982abce3dcbbb3577be3` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/.q4t-work/mtp-mainline-20261009/t4-terminal-control/run_mtp_t4_terminal.py` | `054ff08d01f35c004790c5bbc752a1aac53a5888cca8c50e1ee73a09379bd8cc` |
| `/home/rm01/models/dev/qwen4-thor/.q4t-work/mtp-admission-20261007/source/.q4t-work/mtp-mainline-20261009/t4-terminal-control/targets.cmake` | `861f00863fd7604153a856caa1091030bb90445477a673193b1eae240a348a40` |
