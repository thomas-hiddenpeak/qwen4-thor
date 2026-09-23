# 首层输入尺度修正后的SwiGLU/中间量化传播

复用冻结原4K HTTP与moe-scale-propagation-l0-20260924新GU，不
改运行时/观测，不新增HTTP。复制至prepared新目录、去掉.in，
更新路径并新建e2e/reference。inter.cpp用g++-14 C++23 -O2
-fopenmp -ffp-contract=off -Wall -Wextra，build.log为空后run.py。
stdout留prepared，真实终态后seal.py，禁止追加封存日志。

先原GU，再输入尺度修正后GU。CPU两次FP32乘法，sigmoid来自
已冻结完整65536项BF16设备表。中间尺度故意保留原编码规则：
独立枚举正常RNE值后code>=120映射126；次正规half-up/clamp7。
此项不是中间尺度修复。基线必须逐位恢复原SF及FP4，才传播新GU。
每专家保留全部FP32激活/ratio、SF、packed及差异索引/行计数，
确认量化变化只出现在GU变化行；按实际flat映射统计唯一token。

本次moe-scale-inter-l0-20260924，约256MB，预算1GB及20GiB
余量。两个分支每支1638400尺度组、26214400个FP4元素。
依赖冻结设备数学，不是独立数学库证明；终点仅中间FP4。
