# 完整PLE卷积算术参考

复用ple-full-conv-20260924已完成三侧HTTP的冻结快照；生产与
观测器不改，不重跑HTTP。模板复制至prepared新目录并去掉.in，
修改run.py输出路径，不覆盖旧证据。本次ple-full-conv-reference-20260924。

reference.cu使用nvcc C++20 -O2 -arch=sm_110a -ccbin=g++-14
-Xcompiler=-Wall,-Wextra,-ffp-contract=off，生成reference和空
build.log，然后运行run.py。设备辅助只执行SiLU，CPU按四项
FP32 fma独立累加，读取实际9行因果历史。另比较CPU expf与
FP64卷积/稳定SiLU参考，保留卷积、PLE加法、主干加法三次BF16
舍入。没有重算上游gated/gated_n，不是独立整层前向。

每块最多128行；每种完整输出先落盘，生成全差异索引/位值，
逐字节及SHA恢复一致才删除临时BF16。完成后再次重建每个完整
phase/variant并核对连续覆盖。全部CPU FP32累加值、设备SiLU
保留，FP64在最终输出不同位置的累加/SiLU保留，其他未舍入
FP64不落盘。融合内部值未直接观测，输出一致不证明内部全同。

总参考预算600MB，单块临时40MB，始终保留20GiB磁盘余量。
生产不改、原错答仍在，所有差异须报告，不能把位差自动判为缺陷。
