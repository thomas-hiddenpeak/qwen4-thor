# 首分叉局部重放与状态因果证据（2026-10-20）

后续更新：本页保留当时证据与未决项；现已完成实际内核和独立WMMA核对，
最终分类见[2026-10-20结论](UPSTREAM_FIRST_FORK_RESULT_2026-10-20.md)。

状态：LOCAL_REPLAY_PASS_CLASSIFICATION_PENDING。目标继续进行，尚不
将本次差异判为已证明的正常舍入或实现缺陷。

## 投影局部重放

[冻结预算](UPSTREAM_PROJECTION_PLAN_2026-10-20.md)完成六次局部调用：
4 GEMV FP32观测、1原BF16 T4、1FP32输出cuBLAS诊断。旧192个T1/T4
BF16输出均逐位复现，四行head43的GEMV FP32与独立rational逐FMA/
加法舍入参考逐位一致。没有模型加载，单lease正常退出，无重试。

位置74、head43的关键数值：

| 项 | 数值 |
|---|---:|
| 精确实数点积 | 2.5078174649970606… |
| BF16中点 | 2.5078125 |
| GEMV实际FP32与独立归约 | 2.507817268371582 |
| GEMV输出BF16 | 2.515625 |
| cuBLAS FP32输出诊断 | 2.5078110694885254 |
| 原cuBLAS BF16输出 | 2.5 |

GEMV已可由生产源码定义的双累加器/warp归约逐运算解释，不是仅与
理想点积接近。cuBLAS FP32输出在中点下方，支持累加差异导致BF16
分叉的解释，但还没有直接取得原BF16内核的内部累加值。

两个cuBLAS输出模式的算法字段均为
`id=21,tile=5,splitK=1,reduction=0,swizzle=0,stages=13`。
原BF16算法对象直接检查float布局返回`CUBLAS_STATUS_INVALID_VALUE=7`，
所以改用float布局自己的heuristic执行一次。失败调用后的result.state
是初始化值，不能作为兼容通过；字段相同也不能证明内核指令相同。
未把float调用当作原BF16内核的FP32探针，未改变生产精度或算法。

## GDN局部前缀与单标量干预

[冻结预算](UPSTREAM_GDN_PLAN_2026-10-20.md)完成8次局部pipeline。
直接编译未修改的生产linear_attention.cu，5个实际使用kernel的
SASS指令体与生产一致；TU符号散列不同，已按唯一kernel名匹配。
不传checkpoint指针，因为它会改变原RegKernel<8>分派。

总计30项逐位检查通过：

- 4个T1及原T4的gated输出、完整FP32 SSM后态、BF16 conv后态，均
  逐位复现原模型捕获；证明局部重放对应本次实际轨迹。
- 从相同初态分别运行前缀1/2，完整SSM/conv状态及qkv_norm、raw_y、
  gated逐位等于相应T1前缀。由此排除本次layer0在位置72/73的
  GDN状态中已经存在隐藏分叉；不把BF16输出相同代替FP32状态检查。
- 保留T4的其他投影、权重和初态，只把`a[2,43]`由0x4020改为0x4021，
  四行qkv_norm/raw_y/gated和最终SSM/conv均与T1逐位一致。原始数据
  未修改，干预位置、前后值和独立输出保存在plan与新目录中。

这建立了该投影标量与本次layer0 GDN输出/状态差异的直接因果联系。
没有据此声称后续所有层都没有独立数值差异，或整模型任务质量通过。
权重仅只读提取4个小BF16 tensor，其原safetensors分片前后SHA一致。

## 余下的判定工作

1. 核对原BF16和float输出cuBLAS调用的实际kernel/指令，判断相同
   算法字段是否对应同一累加路径，而非仅凭字段推断。
2. 将cuBLAS实际路径与适用的数值合同、独立参考联系起来。仅相邻
   BF16、普通误差包络或float诊断接近，不能自动判为无害舍入。
3. 汇总本夹具的首分叉覆盖范围与历史beta/HC案例的异同，再给出
   有证据边界的舍入/缺陷结论；不得把本阶段PASS直接作为目标完成。

NVIDIA的[PTX规范](https://docs.nvidia.com/cuda/parallel-thread-execution/)
对相关矩阵指令的累加精度与舍入有专门规定；具体适用条目须在实际
kernel识别后核对，当前不据通用规范推定本次使用了哪条指令。

## 身份、复算与生产保护

- [投影运行与独立参考](evidence/upstream-projection-20261020/run.json)
- [投影算法身份](evidence/upstream-projection-20261020/projection-result.json)
- [GDN全部30项检查](evidence/upstream-gdn-20261020/run.json)
- [GDN生产SASS核对](evidence/upstream-gdn-20261020/sass-audit.json)
- [投影证据审计](evidence/upstream-projection-20261020/audit.json)
- [GDN证据审计](evidence/upstream-gdn-20261020/audit.json)

两份原始目录分别为`.q4t-work/upstream-projection-20261020/gpu-v1`与
`.q4t-work/upstream-gdn-20261020/gpu-v1`，均有source、fixture、executable、
plan与files清单。163及221文件摘要分别复核通过；每阶段单独完整
持锁、正常结束，不存在阶段内释放空档。轮询不能排除短暂非合作干扰。

src/include无修改，默认build/q4t保持44abd8b1。只增加独立诊断目标，
新增HTTP0、完整模型加载0，不作性能或新整模型质量结论。
