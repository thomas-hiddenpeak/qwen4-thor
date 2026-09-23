# 完整 down 矩阵乘参考

复用 moe-full-down-20260924 的三侧 HTTP 冻结证据，原错答仍存在。
生产和观测器不改变，不重跑 HTTP、不使用 bench。

将模板放入 `.q4t-work/prepared/moe-full-down-reference-20260924/`，
去掉 `.in`；同名证据不能覆盖。两个C++程序使用 C++23、`-O2
-ffp-contract=off -Wall -Wextra`、CUDA include/lib64 构建；replay
链接 cudart/cublasLt，fp64 链接 cudart/cublas。对应工具与日志为
replay、fp64、replay-build.log、fp64-build.log，日志须为空。
随后依次运行 run.py、audit.py。

按当前down行序重建上一阶段实际中间FP4/SF，绑定完整权重计划。
replay 重建SF物理布局并执行记录算法；fp64 独立按数值表/指数
公式解码，执行FP64 Dgemm，经FP32/BF16最近偶数舍入。末token
480个专家行另做CPU顺序FP64交叉核对。全部参考保留实际SF和
实际FP32 alpha，不能据此消除量化尺度编码错误。

每层、每种参考先保存完整临时BF16输出；Python保存差异索引和
替换位值，重建全部输出，逐字节及SHA相同才删除临时文件。
audit 再从落盘delta重建完整参考，核对所有差异及末token FP64。
保存差异处未舍入FP64和末token完整CPU/GPU FP64；其他一致位置
的未舍入FP64不落盘。临时与参考预算600MB，另留20GiB空闲。
