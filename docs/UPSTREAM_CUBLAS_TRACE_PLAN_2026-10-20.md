# cuBLAS实际内核识别冻结

目标仍active。局部GEMV与GDN因果证据已通过，当前缺口为原BF16和
float输出的cuBLAS实际kernel/累加路径，不能仅凭相同算法字段判断。

本轮一次Nsight CUDA trace，复用已保存的精确投影binary及fixture，
不重新构建或加载模型。预算原6次局部调用（4 GEMV、1 BF16 batch、
1 float输出），无重试；不采集CPU栈、GPU性能指标，不作性能结论。
追踪后所有输出及算法JSON须与原结果逐位相同，再导出SQLite提取
实际kernel名/启动参数；后续SASS读取为离线工作，不额外GPU运行。

完整驱动`tools/verify/upstream_first_fork/run_trace.py`经gpu_exclusive
运行。单lease路径`.q4t-work/upstream-cublas-trace-20261020/lease-v1`，
数据run-v1。plan绑定原binary/fixture、nsys安装路径/摘要/版本与命令。
trace失败或ENV_ABORT保留现场，不因工具失败自动重跑。

Nsight可能观测到不同输出类型的不同kernel；该结果是待验证事实，
不提前推断是否累加相同。生产代码/入口不改，HTTP0、全模型0。
