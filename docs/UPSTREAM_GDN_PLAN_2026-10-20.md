# 首分叉GDN状态与单标量干预冻结（2026-10-20）

目标active。本轮仅layer0已保存投影输入与初始状态，不加载模型或
重跑完整前向。只读读取4个小BF16权重（conv1d/norm/A_log/dt_bias），
其safetensors分片在前后按原内容SHA核验；原始捕获完整身份复用。

独立目标直接包含未修改linear_attention.cu。5个实际使用的kernel
SASS指令体与生产对象相同（忽略TU符号散列），构建零警告。首次
按完整符号名查找因TU命名不同失败，改按唯一kernel名匹配后审计
通过；未因此改变代码或启动GPU。不启用会改用另一kernel的checkpoint。

冻结8次局部pipeline（每次conv/update/normprep/RegKernel<8>/NormGate）：
4次顺序T1、1次原T4、重置后T1/T2前缀各1次、1次T4单标量干预。
原T1/T4 gated输出及完整SSM/conv后态均须逐位复现旧capture。
前缀1/2的完整FP32状态及qkv/raw_y/gated须与对应T1前缀逐位相同。
干预仅把a[2,43]从0x4020改为0x4021，其他投影和权重不动；所有GDN
输出与最终状态须与T1一致才支持该标量解释layer0 GDN分叉。

单lease路径`.q4t-work/upstream-gdn-20261020/lease-v1`，数据gpu-v1；
完整驱动`python3 -B tools/verify/upstream_first_fork/run_gdn.py`。
无重试、HTTP0、完整模型0。精确binary/source/fixture绑定运行前
plan.json，失败保留现场；ENV_ABORT不计数值失败。该阶段不解释
cuBLAS内部归约，不把局部因果证据扩大为整个模型计算保证。
