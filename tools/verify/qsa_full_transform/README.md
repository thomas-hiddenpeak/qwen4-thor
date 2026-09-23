# 完整QSA q/k归一化与RoPE参考

复用qsa-full-main-20260924三侧HTTP已封存证据及绑定的完整QSA
输出，不改生产/观测器、不新增HTTP。复制模板至prepared新目录、
去掉.in并更新路径。本次qsa-full-transform-20260924。

reference.cu以nvcc C++20 -O2 -arch=sm_110a -ccbin=g++-14，host
-O3,-fopenmp,-Wall,-Wextra,-ffp-contract=off构建reference。
build.log必须为空。run.py stdout放prepared/run.log；真实终态后
运行audit.py，stdout放prepared/audit.log；再运行seal.py，stdout
仍放prepared，禁止生成清单后继续向证据目录中的日志写入。

绑定252份输入、24份norm checkpoint权重段，覆盖12层4096位置
的q/k两侧。三种参考：

- device：CPU八个32-lane XOR树及顺序warp累加，仅rsqrt复用设备；
  RoPE的pow/cos/sin复用设备，CPU按当前分量*cos的显式FMA顺序合成。
- cpu：norm以double sqrt/倒数后舍入FP32，RoPE以double CPU函数
  生成舍入为FP32的频率/三角值，再执行同一CPU FP32/FMA顺序。
- fp64：独立CPU双精度平方和、归一化、频率/三角函数及旋转公式，
  最终经FP32/BF16舍入。使用实际epsilon/theta和位置。

RoPE始终使用实际捕获的norm输出，不传播norm参考；不能将两个
局部参考相同合并成独立高精度整层前向。全量RoPE包含未旋转的
后192维，报告须区分完整覆盖与前64维计算。

保存所有变体的完整BF16基底+delta、所有差异索引及未舍入值
（FP32值精确提升为FP64保存）。完整方差/倒数以及设备、CPU、
FP64的频率/角度/cos/sin表均保留；匹配位置的最终未舍入值不保存。
144份参考逐块与最终再次无损恢复。预算1GB，另留20GiB。

summary.json的affected_queries汇总q/k两侧，是query×side×layer
计数；不能当成唯一query数。必须运行audit.py，检查完整笛卡尔
覆盖并合并同层同位置q/k计数。coverage-audit.json明确区分
affected_query_layers与affected_query_side_layers，另存完整分布。
seal.py在两个控制器退出后生成终态清单并逐项复核。

这些是给定实际输入的算术参考，不证明原始错误答案已修复，
也不证明其他上下文/分块/全部decode的正确性。
