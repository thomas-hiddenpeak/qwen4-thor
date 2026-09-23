# 完整 MoE SwiGLU / 中间量化参考

复用 `moe-full-gu-20260924` 的冻结 HTTP 输入输出；原错答仍存在，
仅通过该请求的观测不干扰检查。没有生产/观测器变化，不重跑 HTTP。

模板放入 `.q4t-work/prepared/moe-full-inter-quant-20260924/` 后去掉
`.in`。将已接受的 `include/q4t/quant/format.h` 复制到准备目录的
`snapshot/q4t/quant/format.h`；它只用于解释 legacy 输出。

构建：

- `device.cu`：CUDA C++23、`-O2 -arch=sm_110a -ccbin=g++-14`、
  `-Xcompiler=-Wall,-Wextra`，include 指向 snapshot，生成 device。
- `cpu.cpp`：C++23、`-O2 -ffp-contract=off -fopenmp -Wall -Wextra`，
  生成 cpu；不使用 fast-math。
- 两份日志分别为 device-build.log/cpu-build.log，必须为空。

依次运行 `run.py`、`audit.py`、`details.py`，线程数固定8。同名证据
不覆盖；重新实验时整体换目录。预留6GiB参考及20GiB空闲空间。

设备生成完整组尺度 ratio、legacy/native E4M3，以及有限 BF16 的
sigmoid 查表。CPU独立枚举最近偶数 FP4/E4M3，保留实际尺度，分别
使用设备 sigmoid、CPU expf、FP64稳定sigmoid与乘法后转FP32。
不把三种结果择优合并，不将 FP4 一致解释为尺度正确。

完整FP4参考、ratio、RNE SF、所有差异索引均落盘；审计重读来源、
完整数组和统计。details 将少量非线性参考差异定位到 token/slot/
expert/column，并区别 FP4 零符号与数值差异。融合核内的每个
未量化 FP32 中间值没有被单独观测；实际输出边界已完整保存。
