# Linear attention输入投影参考

## 结论

覆盖原4K请求36层的QKV/z/a/b投影，取prefill末行及首个pooled
decode行，共288次、1186560个BF16输出。实际权重全部逐字节同
checkpoint，每层同阶段四次投影输入位同；QKV输出接到卷积原始
输入，decode a/b输出接到GDN参数，边界逐位一致。
生产源码和接受二进制2392f6b1未改，原4K答案仍600440（应360284）。

| 对FP64投影经FP32→BF16舍入的差异 | QKV | z | a | b | 合计 |
|---|---:|---:|---:|---:|---:|
| prefill末行 | 488 | 147 | 1 | 2 | 638 |
| 首decode | 31 | 5 | 0 | 0 | 36 |

decode另外用CPU按实际GEMV的双FP32累加链和warp归约顺序重算，
144次、593280个输出全部位同。CPU直接使用捕获的实际权重和输入，
不调用生产GEMV；36项高精度差异可由指定精度运算复现。
prefill638项仍未逐项解释，不能将decode结论或其他投影的重放结果
泛化至此。没有设新容忍阈值，没有宣称linear层或整模型全部正确。

## 首次观测失败及修正

初版以N=10240,K=2560识别QKV，误采了第1层PLE key投影。
只捕获141份prefill投影而非288份，完整性门禁失败。初版实际
误标权重已逐字节匹配checkpoint的layers.1.ple.key_proj.weight，
确认是观测归属错误。无/有观测两次HTTP均原错600440、服务0，
控制器在观测完整性断言处退出1，未运行after组或低层数值分析。
失败源码、日志和数据保留在不带v2的同名prepared/e2e目录。

修订版要求QKV随后出现同输入指针的z投影，确认后才分配linear
层编号，并保留两次调用各自的实际alpha。不是根据形状猜测层。
修订版零警告构建后第一项重新执行原4K无/有/无观测HTTP，三次
全文600440、4096输入/7输出、stop；三服务0，质量驱动1。
288份元数据及所有卷积/GDN观测完整成功后才开始CPU分析。
这次重复是修复采集器后的必要验证，不是重采样寻找质量/性能通过。

## 实际操作数与参考

prefill拦截对应BF16 cuBLASLt调用，检查输入/权重/输出形状、布局、
转置、主机标量及beta=0；decode拦截Bf16GevKernel。两者均要求
alpha=1，记录实际输入、权重和输出，取prefill第4095行或decode
第0行。权重形状分别[10240,2560]、[6144,2560]、[48,2560]、
[48,2560]，逐层匹配linear_attn.in_proj_{qkv,z,a,b}.weight。

每阶段四次投影共用的输入另行按位核对。prefill QKV末行与卷积
末四行的最后一行相同；decode QKV同卷积当前输入，a/b同实际
GDN a/b。z后续NormGate边界尚未采集，不将投影核对当成全链路证明。
新观测与上一轮的原始QKV、GDN a/b及更新S共180份文件逐字节相同。

独立数学参考直接解码实际BF16矩阵和向量，FP64点积，经FP32与
BF16最近偶数舍入，保存全部差异索引。decode CPU参考明确32个
lane的偶/奇累加链、fma和16/8/4/2/1树形相加，编译关闭隐式
浮点收缩；全部输出再由原始文件逐字节复核。两份工具构建均零警告。

## 后续与限制

层输入仍为实际hyperconnection混合值，不是独立生成。prefill仅
验证末行，尚未覆盖全部4096行，也未复现其具体cuBLAS算法。
下一步对prefill记录算法建立同形状重放，隔离638项差异的来源；
随后补NormGate/out_proj及hyperconnection，不能把当前片段一致
等同完整线性attention或原错题根因已确定。已知尺度缺陷继续保留。

证据：`.q4t-work/e2e/linear-projection-reference-v2-20260923/`的
三侧HTTP、288份实际投影、projection-reference.json、
decode-cpu-reference.json、chain-binding.json和artifact-binding.json。
初版失败目录保留observer-failure-diagnosis.json。仓库保存修订版
观测、独立数学分析和decode CPU重算模板。

## 后续：记录实际算法的prefill独立重放

新观测保留QKV→z同输入归属规则，分别复制各次调用自身的算法对象，
包括暂存QKV的算法，检查FP32 compute/scale、默认epilogue、BF16
列主序布局和转置，并记录workspace。初次构建因文本插入范围过宽
失败，日志保留；修正后零警告，第一项执行原4K无/有/无观测HTTP。
三次仍600440、4096输入/7输出、stop，服务均0、质量驱动均1；
288份投影及144份prefill算法记录完整，workspace均33554432字节。
这只通过观测不改变本请求结果的检查，原题质量没有通过。

新旧1368份BF16快照逐字节相同，包括实际投影权重、输入、输出；
据此绑定上一轮FP64参考及checkpoint核对，不另选样本或改变参考。
独立进程使用记录算法、相同矩阵形状/布局/权重和32MiB workspace，
先用AlgoCheck验证兼容性，未重新选择算法。cuBLAS版本130501。
输入用捕获的末行复制4096次，只核对末行；不是原始4096行完整输入。

144次、593280个输出全部逐位同HTTP。因此原638项FP64差异
（QKV488、z147、a1、b2）可在没有模型调度的独立GEMM重放中
复现。没有将这些差异清零，也没有独立证明cuBLAS内部累加算术。
生产实现和接受二进制未变，没有新的质量/性能验收结论。
下一步补NormGate/out_proj及其输入交接，仍需继续核对完整prefill
状态生成、hyperconnection、QSA及PLE等上游，原错题因果未确定。

证据：`.q4t-work/e2e/linear-prefill-algorithm-20260923/`中的
raw-http-audit.json、observed-captures、previous-binding.json、
replay-result.json、replay/及artifact-binding.json。独立重放退出0，
原始输出文件另行逐字节复核；构建失败与随后零警告日志均保留。
仓库保存算法观测和独立重放模板。

## 后续：完整4096行输入投影（2026-09-23）

此前同形状重放复制末行输入，本轮改为捕获并重放实际完整矩阵，
不将旧末行结论扩展成全序列结论。生产运行时未改，仍2392f6b1。

新观测器零警告构建后首项原4K无观测→观测→无观测HTTP，三侧
仍4096输入、7输出、600440、stop，服务0、质量驱动1。288份
投影元数据、144份prefill算法完整，workspace均32 MiB。
观测一致不等于任务质量通过，诊断时间不进入性能结果。

36层QKV/z/a/b实际输入均为[4096,2560]，每层四个输入完整字节
相同；权重逐字节匹配checkpoint的相应BF16矩阵。QKV全部行同
完整短卷积输入，a/b全部行同完整GDN的a/beta输入，均核对旧证据
artifact-binding。z完整输出本轮已捕获，但下游NormGate的全行消费
尚未直接观测，不将末行证据放大。

记录实际cuBLASLt算法、布局、BF16输入/输出、FP32计算、alpha1/
beta0，在4096行原始输入上重放144次，2430074880个BF16输出
全部逐位一致。算法兼容性检查通过，cuBLAS版本记录在原始结果中。
这里仍使用同一cuBLAS算法，是脱离模型调度的全矩阵重放，不是
独立证明cuBLAS底层算术或硬件正确。

另将实际BF16权重与全行输入精确扩展为FP64，使用独立Dgemm路径
完成全量点积，再经FP32→BF16舍入，与HTTP输出比较如下：

| 投影 | FP64参考舍入后不同的BF16值数 |
|---|---:|
| QKV | 2017085 |
| z | 551949 |
| a | 5080 |
| b | 4876 |
| 合计 | 2578990 |

FP64完整分数、舍入结果及所有差异索引留存，不设临时阈值、不删去
差异。该参考也是GPU库路径，不能声称完全独立于设备数学实现。
每次末行的差异索引还与此前独立CPU FP64参考逐项交叉核对，仍为
原638项；绑定旧参考摘要，非重新生成旧结果后再比较。

首版FP64辅助程序使用cuda_runtime_api.h时传double**给cudaMalloc，
编译类型错误留档；显式转换后零警告，尚未执行数值测试时即修正。
HTTP计划的scope文字还保留旧“last prefill token”，其同时记录的
full_prefill_rows=4096、观测器源码和实际文件大小明确本轮范围；
原计划不覆盖，另存scope-clarification.json说明，永久模板已更正文字。

全量核查再次比较记录算法结果、FP64舍入与差异索引、checkpoint
权重和上下游全行边界，退出0。当前给定实际混合输入时，完整输入
投影→卷积→q/k归一化→GDN递推已接通指定参考。但混合输入本身
尚非全token独立生成，z门控及最终out_proj全行尚未核对，原错题
仍未修复，已知E4M3编码问题仍在，不能据此认定整模型正确。

下一步补完整NormGate与linear out_proj及其实际输出交接。
新证据`.q4t-work/e2e/linear-full-projection-20260923/`，包含三侧HTTP、
全行投影快照、算法、checkpoint/boundary/previous-fp64绑定、全量
重放/FP64原件及artifact-binding。工具在tools/verify/linear_full_projection/；
必要构建零警告后先run.py HTTP，再analyze.py→audit.py，不能覆盖
已有证据。没有运行旧bench、改变生产计算或接受新性能结论。
