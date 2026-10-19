# 首分叉局部投影验证冻结（2026-10-20）

目标仍active，承接d832ad5捕获的layer0位置74 a[43]。本轮仅局部投影，
不加载模型、不重新运行完整前向，不扩展文本或层。生产源与入口保持。

独立q4t_upstream_projection已零警告构建；精确FP32归约参考4项host
边界/抵消测试通过。每次FMA与加法使用精确rational中间量并单独
舍入到binary32，匹配gemv.cu双累加器与warp shuffle顺序；GPU匹配
仍待验证，不能用实现说明代替实测。

一次单lease驱动：4次Bf16GevFp32，1次原BF16 T4投影，1次FP32输出
cuBLASLt诊断；无重试、HTTP0。原capture输入/权重/输出先核对旧摘要，
形成独立fixture/source/binary快照。旧T1/T4输出必须逐位复现。
四行head43的GPU FP32与独立精确归约逐位核对，再核对BF16舍入。

输出类型变化可能改变算法：记录原BF16算法ID/tile/splitK/reduction/
swizzle/stages、原算法对FP32输出布局的兼容检查，以及新FP32输出
heuristic算法身份。**新FP32输出不自动作为原BF16内核的累加器值**。
若不能证明对应关系，明确保留该缺口，不把诊断便利变成因果证明。

固定路径`.q4t-work/upstream-projection-20261020/gpu-v1`与同级lease-v1。
完整命令为gpu_exclusive包裹`python3 -B
 tools/verify/upstream_first_fork/run_projection.py`（从仓库根运行）。
运行plan.json绑定精确binary/source/fixture摘要。失败保留现场；守护
失败记ENV_ABORT。当前仅此六次局部调用预算，内部GDN验证另冻清单。
