# 完整 4K down 投影边界与路由专家合并

2026-09-24。生产未改，原4K期望360284、仍答600440。完整down
输入/权重/输出及路由合并消费者已接通，503316480个路由合并
FP32值同CPU指定算术参考；**尚未核对完整down GEMM算术**，不
表示整模型正确或质量验收通过。

## 首项 HTTP 与边界采集

只读观测器零警告构建后，第一项测试是tools/evalscope原4K请求，
关闭/开启/关闭观测各一次。三次请求字段相同，输入4096、输出7、
stop、答案600440；服务退出0、质量驱动退出1。只通过这个请求的
观测不干扰检查，诊断时间不作性能对照，未运行bench。

完整48层、21197个活跃专家、1966080个专家行。原子token list
使本次1894366个行位置与GU阶段不同；以(token,slot)重排后，
实际SwiGLU输入（GU输出）、产生的packed FP4与有效SF全部匹配
冻结快照。
保存实际flat/reorder，独立验证全覆盖、唯一性及router专家归属。

对84788个checkpoint段共19535324776字节重新读取校验，现场
直接逐字节比对GPU down weight及swizzled SF，实际input_scale、
weight_scale_2乘积与alpha一致。四段来源计划保存路径、offset、
长度、SHA及原文件大小/mtime，避免重复落盘约19.54GB权重。

矩阵形状、FP4/BF16类型、转置、FP32 compute/scale、host pointer
mode、两侧VEC16_UE4M3及SF指针、beta=0、默认epilogue和32MiB
workspace逐次核对；实际算法已保存，尚未用它宣称GEMM算术通过。

保存5033164800个down BF16值，逻辑内容10066329600字节。
路由合并执行前确认每个expert输出指针正好对应grouped buffer
的计数前缀切片，并将该实际输入与已捕获完整down输出逐字节比较。
实际row_of_flat、router权重亦完整捕获并独立核对。

## 路由合并参考

本阶段核对的是`CombineGroupedKernel`，只合并十个路由专家。
`src/model/moe.cu`先清零routed缓冲，再调用路由专家，随后才执行
shared expert及最终`MoECombineKernel`。这三部分不能混为一个
“完整MoE合并已通过”的结论。

合并前后均保存4096×2560 FP32，48层两侧逻辑内容4026531840
字节。所有503316480个合并前值均为正零，与实际清零路径相符。
CPU按实际十槽顺序，以FP32 fma累加router_weight×down_BF16，
最后加合并前值；关闭隐式乘加收缩，不使用fast-math。
完整503316480个结果与实际合并后值逐位一致。

每层先生成完整CPU参考，再以实际输出为基底保存差异索引/替换
位值；重新从落盘delta恢复全部参考，逐字节及SHA均同完整参考
后，才移除临时副本。最终审计再次完整重建，全部差异为0。参考
可完整恢复，不是仅保存“通过”标志，也不占用第二份相同2GB输出。

该参考条件是实际down输出、实际router权重及实际行映射。它
验证路由合并的接线和指定浮点顺序，不替代down独立算术、shared
expert、最终MoE合并或已知量化尺度缺陷的验证。

## 证据及后续

证据`.q4t-work/e2e/moe-full-down-20260924/`：三份HTTP原始结果、
checkpoint-plan、三类prior binding、boundary-reference、完整
observed-captures、combine-reference及reference中的可恢复delta、
最终audit-summary和artifact-binding。模板`tools/verify/moe_full_down/`。

采集的14.09GB是上述大张量逻辑内容，不含大量小文件、来源计划、
HTTP数据库和文件系统分配。实际运行一直保留20GiB余量，参考工作
预算600MB，通过逐层临时文件与无损差异控制，不复制完整权重。

下一步复用冻结证据核对完整down矩阵乘的记录算法及独立高精度
参考，再补shared expert与最终MoECombine。原错答和E4M3编码
缺陷仍未解决，没有新性能结论；生产实验仍须先完整HTTP门禁。

## 后续：完整 down 记录算法及高精度参考（2026-09-24）

复用上述冻结HTTP输入输出，生产和观测器均未改，不重跑HTTP。
两种参考零警告构建后，重新读取全部实际packed/SF、行序、算法、
输出及checkpoint段，按当前down行序重建中间量化输入。

- 记录算法参考独立生成有效SF物理布局，padding行清零，执行现场
  记录的cuBLASLt算法并检查算法兼容性。21197次矩阵乘的
  **5033164800个BF16输出全部逐位一致**。
- 高精度参考按E2M1数值表和E4M3指数公式解码实际输入与权重，
  用FP64 Dgemm，再经FP32/BF16最近偶数舍入。上述5033164800
  个输出也全部同实际值。两种参考均保留实际SF与FP32 alpha，
  不调用生产量化转换函数来构造高精度数值。
- 末token的480个专家行另用CPU顺序FP64乘加交叉核对，关闭乘加
  收缩。1228800个未舍入值同Dgemm逐位一致；舍入结果同实际。
  CPU只覆盖这些末行，不能说完整输出均做过CPU计算。

每层每种参考临时完整输出经独立读取后，保存基底路径/摘要、
完整参考摘要及差异索引/替换位值。完整重建逐字节及SHA相同才
删除临时文件；最后审计从落盘delta再恢复全部结果，均一致。
全部差异索引及对应未舍入FP64保留（本轮为空），末token保存完整
CPU/GPU FP64；其他一致位置的未舍入FP64不落盘。避免复制两份
10.07GB结果，临时与参考按600MB预算、20GiB余量运行。

证据`.q4t-work/e2e/moe-full-down-reference-20260924/`含全部
input-binding、checkpoint计划、逐层结果、reference-encodings、
reference差异和末行FP64、最终summary与manifest；模板
`tools/verify/moe_full_down_reference/`。

本请求的实际量化输入上，没有发现这两种参考能揭示的down输出
偏差。路由合并已在上阶段接通，但实际SF中仍含已知编码错误，
没有传播修正或建立原错答因果，也没有新性能结论。
下一步核对完整shared expert和最终MoECombine；采集前按来源
manifest处理重复的清零缓冲证据，为新边界留出容量。
