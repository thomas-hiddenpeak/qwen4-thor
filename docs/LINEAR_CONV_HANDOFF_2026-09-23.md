# Linear attention短卷积历史与GDN输入参考

## 结论

原4K请求36层linear attention，核对prefill最后一个token及首个
pooled decode的4-tap因果卷积。生产源码与接受二进制2392f6b1未改。

| 对照 | 数量 | 结果 |
|---|---:|---|
| prefill历史 vs 最后三项原始QKV | 1105920个BF16 | 全部位同 |
| prefill历史 vs decode读入历史 | 1105920个BF16 | 全部位同 |
| decode新历史 vs 旧历史后两项+当前原始QKV | 1105920个BF16 | 全部位同 |
| decode卷积输出 vs GDN输入 | 368640个BF16 | 全部位同 |
| 两阶段卷积输出 vs 独立FP64卷积/SiLU | 737280个BF16 | 5项不同 |
| 两阶段卷积输出 vs CPU FP32 fma+CUDA SiLU | 737280个BF16 | 全部位同 |

5项数学参考差异保留，没有新增容忍度。指定运算顺序能够复现全部
采样结果，不能由此证明原始QKV投影、整个prefill或模型质量正确。
原4K答案仍600440，应为360284。

## HTTP先行和采集

只读观测库零警告构建后首项tools/evalscope原4K无/有/无观测HTTP，
三次全文、4096输入/7输出、stop一致，三服务0，质量驱动均1。
观测不改变已测全文，不等于质量通过或性能验收。

在CausalConv1dWithCkptKernel完成4096-token prefill时保存最后4行
原始输入及最后1行卷积输出，在Conv1dUpdateStateKernel后保存
各通道三项历史。完整36层prefill后，只读取首个pooled decode的
旧历史、当前原始QKV、实际卷积权重、卷积输出及更新历史。
随后同一轮GDN入口再次读取QKV，确认没有在中间被改写。

实际形状为10240通道、conv_k=4、seq_id=0。每类卷积采集36份，
GDN状态观测仍保留，启动阶段无采集，所有成功标志核对通过。
本次decode QKV及更新后S共72份文件与前次GDN观测逐字节相同，
因此可接上既有单步递推参考，不将不同请求的状态拼接为一条链。

## 独立计算

读回的decode卷积权重逐层与checkpoint的linear_attn.conv1d.weight
（BF16，[10240,1,4]）逐字节相同。prefill未另外读回其权重指针内容；
其最后一行输出用同一checkpoint权重独立验证。原始输入仍取实际
投影输出，尚未独立生成。

每个通道以最旧到当前顺序取四项：prefill为原始token4092–4095；
decode为三项历史加当前原始QKV。独立FP64点积后稳定sigmoid形式
计算SiLU，再经FP32→BF16最近偶数舍入，记录全部5项差异。

另一参考在CPU用四次显式libm.fmaf形成FP32累加值，再送入只包含
x/(1+__expf(-x))及BF16舍入的设备工具，737280项全部位同。
CPU没有调用生产卷积kernel，设备只重建非线性；并不声称CUDA
SiLU在全域等于高精度数学值。设备工具零警告构建、退出0。

历史的三个条件直接按数组位模式核对：prefill历史为末三行转置，
decode读入相同，decode后历史为旧历史移位加当前原始输入。
这验证了本请求该边界的因果窗口与历史更新，不覆盖其他序列槽、
并发、任意chunk长度或所有prefill token。

## 后续与证据

下一步独立核对生成这些原始QKV及a/b的输入投影，并补linear层
NormGate/out_proj和hyperconnection边界；完整prefill递推初态来源、
QSA、PLE和最终head仍列为未闭合。原尺度缺陷和任务错误继续保留。

证据：`.q4t-work/e2e/linear-conv-handoff-20260923/`的三侧HTTP、
卷积/GDN快照、conv-reference.json、conv-order-analysis.json、
previous-gdn-comparison.json和artifact-binding.json；prepared同名目录
保留控制器和零警告构建。仓库保存观测、CPU数学分析和SiLU重建模板。
