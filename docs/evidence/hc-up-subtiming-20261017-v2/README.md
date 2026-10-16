# Prefill subtiming覆盖缺陷修复（尚未执行新模型请求）

v1错误要求prefill存在moe-subtiming，首条运行暴露缺失后主动停止，原
协议与ENV_ABORT记录保留。src/quant/moe_decode.cu才有MoESubTiming
Start/Stop，小T上限8；src/model/moe.cu的大T路径没有这些内部计时。
正确支持的prefill报告是layer-timing、sub-timing、hc-mix-subtiming。
sub-timing的顶层moe仍然存在，只是不能再分解为prep/gemm/combine。

parse_reports.py保留字段、形状、同步错误和顶层求和检查，错误MoE
内部分项不列入prefill要求。5项CPU回归验证正确覆盖、decode报告忽略、
缺失报告/形状、NaN/求和/同步错误、意外prefill MoE详情拒绝。
没有更改性能差门槛或根据测量结果挑指标，没有新模型运行。
下一次必须先冻结修订版执行协议，并在每个HTTP后立即检查报告覆盖。


## v2执行前冻结

修订protocol.json仅纠正报告覆盖，数值/形状/差值规则不变。保持原A B B A
四条、原二进制和单阶段GPU lease，不叠加额外observer。v1首条不完整，
零条可复用合同结果；其协议/现场不改写。本次是已定位夹具缺陷修复后的
受影响项验证，不重跑跳板、注册、host或Gather观测。
run.py每个HTTP结束立即调用已校验parse_reports，成功才启动下一条；
失败保留现场并退出，不自动重试。full_core仍不是单kernel，HC/top moe
仍跨所有层。此阶段只定位，不使用插桩TTFT接受候选。


## v2第二条退出时监控缺陷（2026-10-17）

01-A与02-B均有完整HTTP结果、合同全过、服务退出0；02-B在HTTP结束后
清理期间，guard把自身PID1169273报为竞争。原ENV_ABORT不改写，03/04
未执行。partial-audit锁定两条结果及原日志SHA；02-B三家族覆盖离线通过。
不重跑这两条、不把部分AB当成完整ABBA或性能接受。

进一步工具缺陷：processes原先把stat/cmdline/exe放在同一个try中，
exe消失会丢弃仍存在的stat/进程组。现在保留stat身份，独立标记metadata
缺失/不可读；NVML中的未知/非自有PID仍检测为冲突。真实CPU zombie
夹具复现exe缺失而stat存在并验证归属，合计14项CPU合同及host CTest过。
历史现场没有保存当时stat，不能断言一定是zombie；修复覆盖该已证实
扫描缺陷，原事件身份与时序只证明自有服务退出窗口被误报。
后续只考虑补未执行03-B/04-A，先记录阶段中断与证据复用边界，禁止
把中断的lease伪称全阶段连续。尚未启动补跑。
