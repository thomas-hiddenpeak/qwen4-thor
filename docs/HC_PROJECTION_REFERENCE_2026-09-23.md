# HC低秩投影与注入门参考（2026-09-23）

## 结论

原4K请求48层attention/MLP两组，prefill末token/首pooled decode
两阶段，共576次投影、2028288个BF16输出。实际全部权重逐字节同
checkpoint；normed→down/inject、up→混合门、inject→残差写回边界
位同。3696份已有BF16快照与上一轮相同。生产实现未改，原错仍在。

| FP64参考经FP32→BF16后的不同项 | down | up | inject |
|---|---:|---:|---:|
| prefill | 155 | 195 | 2 |
| decode | 2 | 36 | 0 |

prefill inject列为原始投影；decode为融合门输出，不能将两列作为
同一中间量比较。decode原始投影保存在寄存器，没有现场原始值快照。

prefill按记录算法同形状重放288次、1014144输出全部同HTTP。
decode按CPU双累加链/warp树、保留BF16中间舍入及门函数重算288次，
1014144输出也全部同HTTP。CPU门函数本轮无需替换为设备近似即可
匹配；不代表所有输入下CPU/CUDA数学函数相同。

实际down输出除4后的SiLU对FP64参考61440项全同；实际prefill
inject输出除4后的2*sigmoid门384项全同。原352/38项投影差异保留，
不设置新容忍阈值，不把算法重放等同独立cuBLAS内部算术证明。

## 观测和身份

新观测沿用HC来源文件标识、实际第0层norm权重确定decode起点，
排除PLE同名核和prefill最终T1输出头混合。HC norm完成后建立本层
attention/MLP上下文，依次要求down、up、inject形状及实际输入指针；
不单靠矩阵形状推测归属。归一化输出不替换为重算值。

形状固定：down[N320,K10240]，up[N10240,K320]，inject[N4,K10240]。
权重逐层核对对应attn_hyper_connection或mlp_hyper_connection的
input_mix_weight_down、input_mix_weight_up、block_inject_weight。
down/inject实际输入均同对应归一化输出；up输出同实际混合核up。
prefill注入门单独记录ApplyInjectGate后结果，decode记录融合核输出，
均同attention融合写回或MLP独立写回所用gate。

零警告构建后首项为tools/evalscope原4K无/有/无观测HTTP。三次
全文600440（应360284）、4096输入/7输出、stop，服务均0、质量
驱动均1。576份投影及96次prefill门记录完整后才开始数值分析。
MTP关闭，接受二进制2392f6b1不变，诊断时间不作性能数据。

## 计算方法和限制

FP64投影使用实际BF16权重、实际BF16输入；FP32→BF16最近偶数
舍入。decode inject参考明确先舍入投影BF16，再除4与2*sigmoid，
没有用不经中间舍入的表达式代替融合核的精度合同。非线性参考单独
使用实际down/prefill inject输出，避免上游投影误差混入局部比较。

CPU按32 lane的偶/奇累加链逐次fma、链相加、16/8/4/2/1树形求和
重算投影。inject再BF16舍入，以double exp舍入FP32、FP32加法/
除法计算2*sigmoid(raw/4)，最后BF16舍入。编译关闭隐式浮点收缩。
CPU保存全部原始投影参考及最终输出；decode原始inject值是参考值，
不能声称已与不可见的设备寄存器中间值独立比对。

prefill记录实际cuBLAS算法、形状、布局与32MiB workspace，AlgoCheck
通过后独立进程重放，cuBLAS130501。捕获末行复制4096次，只比较
末行；不是原全行输入。576份重放输出另逐文件与现场输出复核，
所有工具零警告、执行退出0。当前只覆盖原请求指定token/阶段。

证据在`.q4t-work/e2e/hc-projection-reference-20260923/`，包括
raw-http-audit.json、projection-reference.json、prefill-replay.json、
decode-replay.json、previous-binding.json、artifact-binding.json及
全部实际操作数和参考数组。仓库保存观测、独立分析、两阶段重算
和审计模板。

## 下一步

结合HC_HANDOFF_REFERENCE的残差、归一化和混合门结果，当前HC
局部计算链路的实际操作数已接通。仍不是整模型正确性确认，更不能
据此认定原错属于模型能力。下一步检查12个全注意力层的索引器、
选中块与KV读取；完整prefill GDN递推、PLE及最终输出头仍待覆盖。
已知量化尺度缺陷及原4K质量失败继续保留，不进入性能改造验收。


## 2026-09-24后续：完整4K实际投影及门值链

新增最小观测器，以实际HC read/fused归一化的输出指针确定48层
attention/MLP顺序，追踪down→SiLU→up→inject→ApplyInjectGate。
仅覆盖T=4096，最终T=1 mixer及decode不纳入本轮。新观测零警告
构建后首项无/有/无观测HTTP，三次仍600440、4096输入/7输出、
stop，服务0、质量驱动1。仅观测一致性通过，任务质量未通过。

为避免重复存储，先校验旧HC完整快照的288份norm/up/gate及其
manifest；观测时对现场norm/up/gate完整逐字节比对旧文件。保存
全部288组实际权重和算法、完整down输出、SiLU输出、inject原始
输出与门值。原始数据预算1775763456字节，加1GiB参考工作空间
及20GiB保留，开跑可用26489491456字节。480份元数据全部完整：
288投影、96 norm来源、96 gate来源；在线比较/指针/调用顺序均通过。

实际288组权重逐字节匹配checkpoint。每次实际M4096，down为
N320/K10240，up为N10240/K320，inject为N4/K10240；FP32计算，
BF16矩阵、alpha1/beta0、32MiB workspace。使用本次记录的算法
和完整不同token输入重放4153933824个输出，全部逐位一致。
这不是只重复末行，也不是独立FP64矩阵参考；算法仍复用cuBLASLt。
完整FP64投影公式及差异分析尚待下一阶段完成。

SiLU(down/4)共125829120项，2*sigmoid(inject/4)共1572864项，
CPU指定FP32算术、设备基础函数查表、double公式经FP32/BF16
舍入三种参考在该样本均无差异。输入限定为实际有限BF16；首次
全编码查表中未使用的NaN转double触发RuntimeWarning，初版脚本/
日志/完整结果留存。改为仅转换有限编码后重跑无警告，完整JSON
结果与初版严格相同；不是修改实际算术或忽略现场NaN。

参考采用原始输出加全部差异索引/值的无损编码，生成时逐元素及
SHA复核后才移除本轮临时完整副本；最终审计再次重建全部参考。
所有旧证据保留，生产源码/接受二进制未改，原4K错答仍在。
证据`.q4t-work/e2e/hc-full-projection-20260924/`包含三侧HTTP、
prior-binding、checkpoint-binding、replay-reference、
nonlinear-reference、初版非线性记录、summary和artifact-binding。
工具模板`tools/verify/hc_full_projection/`。下一步用独立高精度
公式检查全部投影；其他全位置MoE/QSA/PLE及任务正确性仍未完成。


## 2026-09-24后续：完整4K FP64投影参考

复用上一节已绑定三侧HTTP的冻结快照，无新增运行时或观测改动，
没有再次运行HTTP。零警告构建后，先重新核验两阶段manifest和
全部实际输入/权重/输出，再以精确BF16→double转换、cuBLAS
Dgemm计算全部288个M4096矩阵。其输出经FP32再BF16舍入，与
4153933824项实际输出相比有1299342项差异：down574830、
up718949、inject5563。未使用容忍阈值或只检查末行。

全部288条末行的差异索引与旧CPU FP64参考严格一致，合计仍为
352项（down155/up195/inject2）。这是不同实现的末行对照；全行
高精度矩阵仍使用cuBLAS Dgemm，并非CPU逐元素全行计算，也不是
从原始token开始的全模型FP64前向。前一阶段记录算法重放全同的
结论不变，不能把本阶段舍入差异直接作为错误归因或可忽略证明。

受磁盘容量约束，保存全部舍入后BF16参考（绑定原始输出+无损差异
编码）、每一个差异位置的FP64值及每矩阵完整末行FP64值。每次
生成后完整重建比对，最终审计再逐文件SHA和差异值舍入复核。
一致位置且不在末行的未舍入FP64值没有落盘，这一限制不能表述为
保留了完整FP64中间矩阵。每组开跑检查20GiB预留加1GiB工作余量；
全部阶段证据约数十MB，旧证据未删除。

证据`.q4t-work/e2e/hc-full-fp64-20260924/`包含input-binding、
reference、全部delta/差异FP64/末行FP64、previous-last-row-binding、
summary及artifact-binding，工具模板`tools/verify/hc_full_fp64/`。
生产未改、原4K错答未修复，全模型正确性仍未通过。
下一步先核验冻结证据中可共享的重复文件以恢复采集容量，再补
全位置MoE/QSA/PLE链。仅清单预检发现两阶段HC证据约36.07GB
重复候选，尚未修改文件或据此宣称已释放空间。
