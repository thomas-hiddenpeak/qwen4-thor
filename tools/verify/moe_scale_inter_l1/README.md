# 四分支 layer1 新路由中间量化

各支自身GU按新专家行序计算CPU SwiGLU（两个有序FP32乘法，
冻结完整BF16设备sigmoid表），再legacy E4M3/E2M1量化。继续
保留layer1原编码缺陷，隔离layer0干预。361个down input_scale
checkpoint重读，原支按old-GU-row映射全部恢复SF和packed。
新增专家行无原对照，独立保存；共有GU不变时量化不变。

模板去.in复制到 `.q4t-work/prepared/moe-scale-inter-l1-20260924/`，
新建对应e2e/reference。inter用g++-14 C++20 -O3 -fopenmp
-Wall -Wextra -ffp-contract=off。空build.log后run.py，stdout在
prepared/run.log；真实终态后seal.py，stdout也留prepared。
新实验新目录，不覆盖冻结数据。

预算1GB+20GiB余量，封存10112项557035742字节。保存完整F32
激活/比例、SF/packed及全部共有行差异/行分布。没有独立高精度
整模型证明，没有生产或观测修改，无新HTTP。终点中间FP4。
