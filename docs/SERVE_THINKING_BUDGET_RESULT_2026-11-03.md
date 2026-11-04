# SRV-05b 开发与未完成验收记录

结论：**实现及CPU合同通过，真实HTTP尚未验收，不交付候选。**
2026-11-03 02:38 CST关闭本轮GPU阶段；长期工业化目标保持active。
默认仍是SRV-05a的ecd5d21f/91ce0ed9。这里的未接受不是已证明预算
算法失败，而是启动夹具失败后完整业务验收未执行，不能跳过出口。

## 实现与身份

- 运行时源码：55cfae11；首次启动参数修正：91652746。
- 未接受候选ELF：
  `c02b94144c5a40c6118bd7dde306b298dce10c963045708f82082f6a5f2b8b54`。
- 已接受默认ELF：
  `ecd5d21f7b3a9adb221b17a352499df5a5803020f15149ed6cef978c916f8dcc`。
- 已接受sidecar：
  `91ce0ed94e4ee2c9bcb30895258a6bb30bef3cfe8e37f0dd6ac9160fb314ecfb`。

[合同](SERVE_THINKING_BUDGET_2026-11-03.md)定义精确整数/null输入、
自然推理token计量、有限UTF-8及闭合控制token、usage明细及总长度
优先；控制作用于实际选词。grammar先交集再接受一次；MTP每批开始
前保留一个合法pending，余量不足转host尾部，不截断已提交KV。
日志/metrics区分实际MTP批次、回退、自然预算和强制token；客户端
检查计量等式。新增schema是投影，不是完整OpenAI输入合同。

10个已有server运行时文件及2个新增预算文件变化；模型、GPU算子、
MTP计算和调度源码不变。数值源码身份可复用，但新的生成策略仍需
真实HTTP质量与缓存恢复证明，不据此宣称性能非劣或推理计算FLOPs上限。

## 已有证据与缺失项

| 检查 | 结果 | 边界 |
|---|---|---|
| CPU无结构化后端 / 有后端 | 179/179、188/188 | 两套配置有重叠，不相加为独立覆盖数 |
| Thor构建必需host / 真实grammar后端 | 145/145、9/9 | 无模型推理；含强制token交集、失败不改变matcher及UTF-8闭合 |
| 项目客户端 / 验收驱动 | 27/27、最终9/9 | 包括usage等式、错误raw保存、0700目录与拒绝复用已有目录 |
| 新增生产启动预检 | 5/5 | 两种原失败及缓存/plain/MTP正确argv，实际SessionCache.Open直读写；无CUDA/权重/HTTP |
| 原模板差分 | 复用120项 | 渲染、分词器、history resolver函数身份相同；未重新执行120项 |
| 缓存fixture分词 | 8467 tokens | 只证明足够长；未证明模型缓存命中 |
| 真实业务、MTP回退、工具/schema、缓存恢复、安装健康 | **未执行** | 0生成、0输入负例、0模型加载；不能填写质量或时延结果 |

构建零警告。开发中CPU探针首次将目录作为tokenizer.json参数导致
SIGABRT，仅有事后说明，原stderr未落盘；第二个6667 token短fixture
原输入/输出保留，修正固定padding后8467通过。SDK验收驱动现在在
异常路径也保存已捕获raw响应，避免失败证据只剩Python异常。

## 两次启动失败及停止条件

1. 原package（55cfae11）：缓存argv漏`--seq-capacities 16384`，生产
   参数校验退出，耗时约0.66秒，无模型加载。
2. correction（91652746）：容量参数已修正，但私有缓存目录尚未创建，
   `SessionCache.Open`返回不存在，约0.66秒，无模型加载。

两次guard均COMMAND_FAILED，未发生ENV_ABORT；finally报unclean
shutdown，真实根因以保存的server.log为准。原始时间从02:30:31 CST
连续计到02:38:28，共477.29秒，没有重置计时。总4次启动上限已用2次，
还需MTP业务、plain、安装健康3次，因此停止本轮，不借改名/跳过安装
健康挤进预算，不扩大或把未执行项标通过。

后续开发已补上新建0700私有目录，以及在Popen前对**实际argv和目录**
执行生产选项/缓存CPU探针；复现两个原错误并验证修复。探针不验证
GPU内存容量、模型可用性或HTTP，也不等于SRV-07完整产品预检功能。
本轮`retired.json`阻止继续execute；原package和correction均保留。

## 当前环境、证据与下一步

02:38 CST核对GPU计算PID为空、共享锁可用、无恢复标记，统一RAM
MemAvailable约117.00GiB；默认ELF/sidecar完全未替换，无回退动作。
本地完整材料位于`.q4t-work/serve-thinking-budget-20261103/`：两套
sealed package、两套sealed execution、最终CPU预检和postflight；
大ELF及bundle不重复纳入Git，仓库证据记录其路径/大小/SHA256。
[原始记录入口](evidence/serve-thinking-budget-20261103/README.md)：155份直接复制记录，
加说明、身份和seal共159文件，约2.32MB；10个本地大产物身份单列。

下一工作包先明确冻结尚未执行的19生成/12负例/3启动及新阶段边界，
复用同身份CPU和候选；再验证强制闭合、实际MTP回退、严格工具/JSON、
缓存恢复和安装路径。只有这些出口通过才替换默认。SRV-06 stop、
SRV-07产品预检、完整协议/JS SDK与运行验收继续待办。
