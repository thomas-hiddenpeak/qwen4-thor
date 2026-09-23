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
