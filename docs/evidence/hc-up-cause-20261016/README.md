# HC up TTFT定位证据

见[定位报告](../../HC_UP_TTFT_CAUSE_2026-10-16.md)。本目录仅诊断证据，
不代表性能或完整数值验收通过。原NO_GO保留，生产binary未改。

A/B为各一条Nsight HTTP trace；light为各一条已有event计时加只读分配
API探针的HTTP。四条全部输出匹配原main，服务正常退出；两阶段不混算。
各运行的命令、二进制SHA、退出和结果保留。实际脚本保留原本机路径，
不要覆盖原输出目录执行。trace导出分析按实际204789 tokens核验。

trace-comparison以kernel名称/配置分组；全局顺序不同不能推断计算路径
改变。trace-details保留每stream有序hash，四stream完全一致。
light/review逐块记录事件时间及分配size序列；事件区间包含host提交等待，
不能把event差全称为GPU busy，也不能把API wall time叠加到event差。

原始大型trace、完整服务日志、频温记录和系统日志在
.q4t-work/hc-up-cause-20261016/，raw-manifest保留路径/大小/SHA。
探针编译（零警告）：g++-14 -std=c++23 -O2 -Wall -Wextra -fPIC -shared
-I/usr/local/cuda-13.3/include alloc_probe.cpp -ldl -o alloc_probe.so。
.so产物仅保存在.q4t-work；日志完整记录所有分配，不按耗时筛选。
