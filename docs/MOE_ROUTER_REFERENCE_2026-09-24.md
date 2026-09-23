# 原4K完整MoE路由参考（2026-09-24）

## HTTP与实际数据范围

新增最小只读观测，以RouterTopkKernel实际logits指针绑定前一个
M4096/N512/K2560 BF16 router GEMM，排除形状相同但不被router
消费的投影。完整输入逐字节匹配已冻结的HC MLP mixed输出；保存
48层实际权重、算法、4096×512 logits、4096×10专家ID及FP32权重。
预算原始数据342884352字节，另2GiB参考及20GiB预留。

零警告构建后首项tools/evalscope原4K无/有/无观测HTTP，三次仍
600440、4096输入/7输出、stop，服务0、质量驱动1。48份观测完整，
MTP关闭。只证明观测未改变本请求，任务质量未通过，观测耗时不作
性能验收。冻结证据共享文件只读复用，没有原地改写。

## 投影、专家选择与softmax

48组实际gate.weight逐字节匹配checkpoint的[512,2560] BF16。
完整100663296个logits同本次记录算法重放。cuBLAS Dgemm高精度
结果经FP32/BF16舍入后有51380项差异，完整double矩阵、舍入输出
及差异索引保留。48条末行另以CPU double矩阵向量乘法核对，舍入
结果与全行Dgemm末行一致；相对实际logits仍15项差异。

对实际BF16 logits按值降序、相同值按专家ID升序排序，1966080个
选中ID全部一致。参考只把exp交给设备，CPU按选中顺序累加10项、
求倒数并乘回，1966080个FP32路由权重全部逐位一致。纯CPU
exp(double)→FP32版本有808536项FP32位差；double softmax最后
转FP32版本有1179008项位差。全部参考输出/差异位置保存，不设置
临时容忍阈值，不以设备数学复现宣称非线性已完全独立证明。

实际选择正确不意味着改变投影算术后仍选择相同专家。196608个
(token,layer)组合中，使用FP64投影再舍入BF16后，有360行Top10
顺序变化，其中103行选中集合变化。103行中实际第10/11名59行
同分、44行严格有分差，最大分差0.03125；高精度舍入结果77行
边界同分。因此不能全部概括为同分换序。全部行号、增减专家ID、
相关实际/高精度舍入/FP64 logits与边界分差都已保存并逐项审计。
这不是用原始未舍入double logits直接选专家；也没有证明这103行
导致原4K错答，未将高精度路由写回生产或推广为精度提升结论。

## 证据与限制

192条旧末行input/logits/ID/权重绑定一致，旧文件先按原manifest
校验。最终审计覆盖全矩阵、参考输出、所有差异索引、完整选择及
103条排序边界。首次边界明细JSON输出因NumPy int32不能直接
序列化失败，初版脚本/日志保留，转换为Python int后成功；原始
数值数据没有修改，修正不需要重新执行HTTP。

证据`.q4t-work/e2e/moe-full-router-20260924/`包含三侧HTTP、
prior-binding、checkpoint-binding、replay-reference、fp64-reference、
routing-reference、selection-boundaries、previous-tail-binding、
cpu-last-reference、summary及artifact-binding。工具模板位于
`tools/verify/moe_full_router/`。

生产源码/接受二进制未改，原错答与已知E4M3尺度问题仍在；不覆盖
专家mapping/gather/激活量化或完整expert/shared计算，不是全模型
独立前向。下一步接通这些路由输出的全位置mapping/gather及输入
量化，保留实际精度与任务因果的区分，再继续QSA/PLE未覆盖部分。
