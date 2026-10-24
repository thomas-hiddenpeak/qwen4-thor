# Nsight 监控修复后补测独立审计

证据完整性 PASS；整体 ENV_ABORT。

实际成功HTTP 0/6，admitted 0；旧37普通+6CPU只引用，不累计。

原始状态 STOPPED，测量完整 False。

统计结论 `NOT_ACCEPTED_TOOL_VALIDATION_ABORT`；工程结论 `NOT_ACCEPTED_TOOL_VALIDATION_ABORT`。不是性能 NO_GO，也不构成新运行时交付。

资源监控写出 11/11，未写出 0；失败尝试及 pidfd 等待保留原值。

已核 CPU 测试数 97；native pending 解决 15；阶段 pending 0。CPU native 覆盖不能替代 collector 生命周期覆盖。

完成 collector controls 2/4；其余停止/未运行，actual-target 警告原样保留。

失败屏障仅涉及 owned-step PGID，外层 guard 的 detached 清理单列；缺失最终 fresh flush 不补造。MEMSET 活动缺失若出现，仅记 UNKNOWN_CAPABILITY，不自动称丢失。

- monitor/cleanup environmental closure
- fixture cleanup direct-external
- parent fixture cleanup
- four fixture controls and sole candidate gate did not complete/pass
- four model traces / six HTTP requests not complete

JSON SHA `cb34554cbdbcf5bacc2b68b8e526ddd9978e80c8ec7f7ef86bbaf3124799f7f3`。没有运行新GPU/HTTP/NVML/传感器或测试。
