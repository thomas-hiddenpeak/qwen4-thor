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
