# SRV-02e驱动计数纠正与剩余计量

原812f78d4执行保留FAILED/guard COMMAND_FAILED，默认仍c597adc3。
两次启动拒绝、三个正常启动快照和五个完整HTTP生成均已通过，随后
驱动错把grammar worker数量断言为2；实际3次start/ready/reap正确：
strict JSON、required工具、tool_choice=none最终文字均进入约束解码。
源码output_constraints.cpp已有明确kNone激活规则；运行时不修改。

原始execution和package完整封存。纠正只重新读取原始响应/日志/
metrics/配置/退出证据，验证worker3/3、五成功、slot/owner归零、
真实MODEL引用，原来的失败不追认成成功。没有执行到的最后三个
health HTTP及最终独立资源样本明确缺失；已有实际ready指标/业务
HTTP/资源回收/clean shutdown作为本次有界接受依据，不补模型。

继续完成原来尚未执行的4个256MiB探针和安装CLI/worker检查，HTTP
复用相同SHA候选。累计仍限3启动/2拒绝/5生成/4探针/128控制和自
首轮lease起1800秒；零额外模型启动/生成、零额外原生HTTP重验。
纠正驱动在新的完整lease持锁运行，明确是两段独立lease，不称原阶段
全程未释放。失败保留，预算不扩。冻结和两项正反计数CPU门禁之后执行。
