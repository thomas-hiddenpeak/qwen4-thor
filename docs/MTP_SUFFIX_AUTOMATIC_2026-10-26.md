# API 自动收尾与报告就绪：有界修复验证

2026-10-26，采集工具修复阶段；固定 k3 suffix 交付目标仍未完成。
本轮不改模型运行时，不重新测两种 head 已通过且身份未变的重复收益。
结果：46项CPU合同首suite通过；唯一GPU阶段53.061789秒后ENV_ABORT，
自动报告回调未闭合，0模型/0HTTP。不交付，默认及原运行时NO_GO保持。

## 新依据与边界

旧手动 `stop` 流程在 2026.1 与隔离 2026.5.1 都被内部
`RangeCollectionState` 拒绝。不能据此断言交互 start/launch 不受支持，
也没有证据证明旧 none + manual stop 违反文档。

固定 2026.5.1 的 start help 支持 `capture-range-end=stop`：API stop
自动结束采集、目标继续运行，并支持 `after-report-ready` 回调。
据此采用一条固定替代流程：start → launch → 目标 API stop → 等待精确
session/nonce/report 的原子回调凭据及非空常规报告 → 目标仍存活时
shutdown(kill=none) → 独立释放/停止实际目标 → 回收 → 导出/检查。
失败时没有从手动 stop 切换流程的后备策略。

[官方指南](https://docs.nvidia.com/nsight-systems/UserGuide/index.html)
明确 shutdown 会取消待处理/活动采集，因此它只能在报告就绪后作为正常
会话清理；失败路径中的 shutdown 不得补写采集成功。CLI launcher 退出、
实际目标退出、报告存在和事件完整性分别记录。

[同版本 NVIDIA 支持答复](https://forums.developer.nvidia.com/t/nsight-systems-2026-5-1-produced-112-collected-100-warning-despite-complete-coverage-of-32-kernels/384749)
解释强制 flush 可产生 CUDA 完整性 warning，即便该实例的工作量已完整。
其环境为 Docker/RTX4060Ti，不是本机 Thor。这支持区分 warning 与实际
丢事件，不能豁免本机 NVTX warning、未知 memset 或任意模型 trace。
[手动 stop 的支持实例](https://forums.developer.nvidia.com/t/question-when-prifilling-megatron-lm/350407)
也保留，避免把本机组合失败推广成一般合同禁令。

旧 SQLite 的 6381 次缺活动 memset 均为成功 API，前一个同线程调用是
cuKernelGetFunction，后一个是有活动的 cuLaunchKernel。独立查询定位：
16×16 BF16 WMMA 家族 6283 次（6216 verify + 67 attention），64×128
BF16 tensorop 家族 98 次（96 prefill + 2 forward）。旧 trace 没有 count
参数，不能据相邻 kernel 或 return=0 判零字节。原 SQLite SHA 不变。
结果与可复现只读查询在 `.q4t-work/suffix-range-contract-20261026/`。
cuBLASLt 动态符号未暴露 cudaMemsetAsync，未尝试不可靠的 preload 假设；
公共 CUPTI callback 参数接口可作为后续独立探针依据，不能与 Nsight
同时订阅，本轮不实现该探针。

## 事前冻结

机器范围：`.q4t-work/suffix-auto-stop-20261026/scope.json`。

- 单一依赖 2026.5.1.161；一次固定流程，0 构建/运行时优化候选/参数扫描。
- CPU 首 suite ≤64 方法、180 秒；仅具体失败允许一次有界针对修复验证。
- GPU 一次完整阶段持共享独占锁，4800 秒；2 控制、600 秒合计、每个300秒。
- 控制依次 direct、wrapper；各32 kernel、64 copy、32非零memset、33 NVTX
  及删除整对launch/kernel、末尾NVTX的真实副本负控保持。
- 两个控制通过，且预选 wrapper 的终态/诊断/内存覆盖通过后，最多4次
  模型启动、6 HTTP 请求：8K off、8K on、200K on、200K off；后两者先执行
  固定未采集8K前序，输出256。沿用旧请求身份、缓存约定和顺序，不扩样。
- 所有实际目标/未知 warning、未知内存覆盖仍阻止模型；任何环境竞争或
  监控/清理失败记 ENV_ABORT，只清理已证明的自有进程。
- 观测时间不能当作普通 HTTP 性能验收。五档历史 NO_GO 仍有效，后续具体
  运行时修复仍须按性能协议验证受影响项。

## 身份和复用

进入 commit `9e85a95914bbd19259acec50e1712236b500ba0e`。
观测二进制 f0ff0fe8（源码55363f18）、参考6a4d3d62、fixture ad0241d7、
子集manifest b63b0f25；精确 SHA 和模型197项元数据沿用冻结计划校验。
默认 f356b2c9 与 sidecar422993b0 不改。旧 monitor64/fixture14/analyzer8
及原生64子进程证据按依赖身份复用；改变的 runner 和自动流程重验。
完整 head 重复代码/文本 +13.6774%/+14.4034%；子集共享B描述性GM
+6.5335%/+6.7304%，只在原统计边界内复用，不代表保护项通过。


## 实际结果与窄化的失败位置

准备器33.503秒完成，冻结46544文件/1765工具文件、12文件源码overlay；
plan SHA `325496bd3065367b04f58d40ac8e71d7ed3cb070c5e263e3f0ce3aca80be058a`。
46项CPU首suite0.424733秒通过，403源身份前后稳定，无重跑。

唯一持锁阶段53.061789秒，仅首direct控制尝试。实际目标完成32轮校验及
成功APIStop；02:03:03.686进入RangeGeneration，04.653进入Importation，
04.693完成报告并回到Launched，server也打印Generated。故本次不再是旧
手动StopRequest拒绝；自动收尾已推进到生成/导入成功的状态。

回调命令已注册且配置匹配（NVLOG151、186、1127）；但45.000151秒等待
结束仍callback=null。没有回调dispatch/退出收据，不能断言helper未执行，
也不能断言自动range支持的public callback存在工具缺陷。实际待解决点是
报告完成的确认流程。82008字节报告保留，未导出，不证明CUDA/NVTX事件完整。
超时后只清理自有目标，随后shutdown报无活动会话。该后续错误不能倒推
自动报告生成失败。目标真实正常退出凭据缺失，launcher143不代替目标退出。
wrapper及全部200K/8K对照未执行；不放宽warning/memset门，不追加采集。

独立离线审计一次8.874061秒：46544冻结/21297父snapshot/33raw、197模型
元数据全部匹配；监控54/54、336条进程观测OK，pending0、无监控失败；
7个记录自有身份全部ABSENT、PGID无残留、lease释放。ENV_ABORT来自收尾
失败，不能称监控再次EACCES。13个overlay条目含patch均保存。
审计SHA `1b1f4e0ab3b93efb2d6490014df562e5b1d0fa378d8fa2d98807e0de5854fd97`。

| 维度 | 决定 |
|---|---|
| 证据身份 | PASS，失败现场可核验 |
| 环境/测量有效性 | ENV_ABORT |
| 测量完整性 | 未完成，0模型/0HTTP/0导出 |
| 统计结论 | 诊断阶段不评估收益，无新性能结论 |
| 工程决定 | NOT_ACCEPTED_TOOL_VALIDATION_ABORT；原运行时NO_GO保持 |
| 交付 | 默认/sidecar/子集不变，不部署 |

下一步须以自动生成已完成但回调未确认这一具体证据，确定可验证的确认
合同；旧6381次memset仍缺参数，200K/8K GPU差分及性能保护未完成。
两head重复收益直接复用。完整目标仍active，不把负向归档当目标完成。
公开凭据：[摘要](evidence/mtp-suffix-automatic-20261026/summary.json)、
[审计](evidence/mtp-suffix-automatic-20261026/audit.json)、
[回调法证](evidence/mtp-suffix-automatic-20261026/callback-forensics.json)、
[memset范围](evidence/mtp-suffix-automatic-20261026/memset-analysis.json)。

## 负向阶段封存

514条目/53325361字节独立复制及SHA回读通过；manifest `bb9204592f02`，
seal `e8b29b5840a5`。包含本轮CPU403源快照、raw/lease/overlay/审计/法证及
研究材料；旧大snapshot和208MB工具包通过前两轮归档锚点复用，明确非
自包含包。原证据与回退身份保留，没有部署、健康HTTP或追加推理。
[封存凭据](evidence/mtp-suffix-automatic-20261026/archive-result.json)。
