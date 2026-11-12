# R09 外层失能与人工停服分离 CPU

8项首过，5.387秒，0GPU。必要Guardian构建5.237秒、零警告。
3项新实际进程用例与5项直接实例/原前台/daemon回归。

显式v5外层退出后，原Guardian/controller/gateway/model均保持
同PID和原RUNNING意图、预算，报告READY_DEGRADED；随后明确
stop仍持久STOPPED并清理。原父仍活的SIGUSR1不能伪造丢失；
先stop再退出不会复活。原普通前台父死停服合同与daemon保持。

记录仅接受CPU生命周期与辅助隔离；未接新观察者收养、完整外层
运行与可选宿主适配，未发行。默认及原R07冻结候选保持。
