# 元数据批次接入HTTP回归卡

状态：CONDITIONAL_FROZEN。仅在多路径78文件差分全部通过且guard
COMPLETED后执行。候选运行时b6870c0、binary753d9769；默认c8f4b241
保持。该阶段是所有权结构整理的质量/取消验证，不是性能优化接受。

单个NVML lease完整驱动：quality-off 11条、quality-on 11条，然后
原run_request_cancellation的8组（20生成POST含拒绝项、9取消POST、
不完整连接及有界health/metrics轮询）。共3次完整模型加载、42生成
POST、9取消POST，无重试，不启动performance模式或采样校准。

质量复用上一轮已部署候选的同模式固定inputs/results，以精确文本、
输入长度、SSE响应ID/终止等既有合同检查；max_seq1/max_prefill8192/
max_len208896、port8740、启动300秒，每质量组上限1800秒。取消
复用原脚本max_seq2配置及固定输入，整个脚本上限1800秒。必须
核对全部服务正常关停及清理，无部分结果冒充完整通过。

运行前将新binary/cache及固定参考复制至新输出，保存SHA和源码
身份；只有runtime src/include未变才执行。失败停止保留现场，
ENV_ABORT不作为候选数值失败；不自动重试或追加请求。成功之后
也仅取得本轮合同范围内运行时验证，不宣称统计非劣或默认部署。
输出.q4t-work/metadata-followup-20261020/batch-http-v1，外层guard
退出后封存，未变对象/入口证据复用。
