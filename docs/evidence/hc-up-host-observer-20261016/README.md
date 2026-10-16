# Host 区间观测工具 CPU 校验

工具：tools/evalscope/host_interval_probe.cpp。只拦截原二进制已有的
__fprintf_chk 层计时报告（T>1），调用原 __vfprintf_chk 保留格式/参数。
无 CUDA 调用、模型改动或推理测试。

构建到 .q4t-work/hc-up-host-observer-20261016：

```sh
g++-14 -std=c++23 -O2 -Wall -Wextra -Werror -fPIC -shared tools/evalscope/host_interval_probe.cpp -ldl -o .q4t-work/hc-up-host-observer-20261016/host_interval_probe.so
g++-14 -std=c++23 -O2 -D_FORTIFY_SOURCE=2 -Wall -Wextra -Werror docs/evidence/hc-up-host-observer-20261016/fixture.cpp -pthread -o .q4t-work/hc-up-host-observer-20261016/fixture
```

fixture在主线程与第二线程分别报告基线、100ms线程CPU工作后、100ms
睡眠后，另有T=1与普通格式输出。plain/probed日志删除host新增行后逐字节
相同；各线程seq独立0/1/2，T=1不采集；忙段CPU>=100ms，睡眠段CPU<10ms、
wall>=100ms且自愿切换增加；PMU三计数均成功。cpu-validation记录实际值。
本机schedstat文件及等待值可读，不把零等待当作全环境无竞争证明。

重要边界：首次PMU打开34.874ms，已独立记录init_ns；基线在初始化之后。
后续capture仅约数十微秒，但不含日志写出成本；不能据此宣称全工具零扰动。
先前首版将wall放在初始化之前，CPU校验发现不一致后修正，未执行模型。
第一个prefill chunk不能分析；相邻报告差含上次观测/报告与chunk间工作。
CLOCK_THREAD_CPUTIME_ID含内核线程CPU时间；PMU明确排除kernel/hypervisor，
不能用其比值宣称整线程IPC。PMU报告enabled/running，复用或读取失败须
显式处理，不能把零当成硬件无活动。失败errno、调度读取错误均保留。
其他线程、GPU内部执行和依赖等待不由本工具直接测量。

本轮尚未冻结或执行新的HTTP。未来仅用于机理诊断，禁止用插桩TTFT验收。


## 执行前冻结的四条观测

protocol.json固定A B B A，新鲜服务各一条原200K off；两身份均未改二进制。
run.py全阶段由单一GPU lease保护。analyze.py在四条结束后只分析chunk2–25，
校验单TID/25报告、计数单调、错误码、每区间PMU覆盖>=99%、每次capture
<=1ms且累计<=5ms。日志写出和初始化不包含在capture界限内。
每指标同时给出两对B−A及同身份R；仅两对均正且>R才称版本相关可辨识
观测，仍不能认定因果。CPU可能含CUDA自旋，残差不能称为GPU busy。
任何运行错误保留并停止，不自动重试；全部结果保留，不扩样。


## 2026-10-17结果

完整四条输出合同通过，guard正常COMPLETED，803次运行监控未检测到竞争。
全部区间有效性通过，三PMU事件无复用，捕获总开销每请求1.315–1.374ms，
初始化21.438–29.848ms单列。全部观察见host-review.json与observations。
原始日志/遥测留本机，SHA见raw-manifest；退出见completion-audit。

chunk2–25的两组B−A事件区间为+303.000/−131.430ms；CPU时间为
−311.727/−222.368ms，runqueue为+9.131/−2.779ms。没有任何报告指标
满足两组正且均大于自身R的冻结条件。不能据此认定稳定host额外工作、
排队根因，也不能把历史失败归为噪声或接受HC；不扩样，不重跑旧测试。
下一步先复用原device trace做离线依赖/空隙审计，明确污染区间及覆盖
限制，再决定是否确有必要新增定向设备观测。活动目标仍未完成。
