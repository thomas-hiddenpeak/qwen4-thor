# 终态独立复核

- 证据：**VALID**；错误 0。56 HTTP / 12启动 / 2887.428秒，guard COMPLETED、child 0、lease已释放。
- 独立回读 516 冻结文件、798 原始文件、433 snapshot文件及 197 项只读模型metadata；变化数 0。
- 监控 2888/2888 次完整flush、0未落盘；最后完整sample从 987557.981139355 开始，晚于最后服务退出 987554.032869996 及最终核对屏障 987557.556329393，flush 987558.001913667。
- 真实触发受控io EACCES退出分支 3 次；每次相同pinned目录/start_ticks、Z状态、io errno13，有上下文和traceback。包含最后q4t服务；不能推广为所有未来异常均被覆盖。
- 一次只读/proc复核 61 个历史自有PID/start_ticks：匹配残留 0、不可读 0。另查guard/NVML worker的 2646 个数字PID，当前存在 0；其历史未保存start_ticks，不凭数字PID归属其他进程。
- 默认f356、sidecar422993b0及显式BF16子集manifest/head/IDs与冻结身份一致；运行时仍6a/cb0fa73。
- 只闭合新一轮证据和监控真实生命周期；旧ENV_ABORT不改。收益/保护与机制由根报告，本文不作性能接受、总体非劣或根因结论。
- 22质量/86 host/10阶段合同按原身份范围复用，GDN oracle仅原两层；本轮没有重做这些测试或完整五档验收。
- 子集共享B、完整单启动对、同启动重复及插桩影响限制均保留；无新GPU/HTTP/测试/监控循环或进程信号。

数据：`compact-closure.json` SHA256 `5044d695daf86dc7f5e7833e55fda5c5327b7244a82fc1da7a599a408ceea7d8`。
