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
