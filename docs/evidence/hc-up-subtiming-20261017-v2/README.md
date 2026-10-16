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
