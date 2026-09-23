# GDN prefill到首decode的状态交接

## 结论

原4K请求36个linear attention层的prefill最终FP32 S，与首个pooled
 decode读入的S共28311552个值逐位相同。层身份另通过dt_bias/A_log
逐层匹配checkpoint，两参数联合摘要36份互异，seq_id全部为0。
本样本未发现这一边界的状态变化；不证明prefill最终S本身正确。

独立FP64单步递推与实际输出仍有差异：

| 对照 | 数量/结果 |
|---|---:|
| 更新后S vs FP64递推舍入到FP32 | 15201445个位模式不同 |
| 各层S最大相对L2误差 | 9.772326332706868e-8 |
| BF16输出 vs FP64完整单步 | 20项不同 / 221184项 |
| BF16输出 vs 实际S更新后用FP64读出 | 17项不同 / 221184项 |
| 各层BF16输出对未舍入FP64最大相对L2 | 0.0021554114283457286 |

全部差异保留，没有误差阈值或“自动通过”。FP32状态大量位模式
不同不能直接解释为大量实现错误；FP64公式也没有模拟CUDA逐步
舍入、rsqrt/exp/log或归约顺序。本轮尚未对这些差异作因果解释。

## HTTP与实际路径

新观测库零警告构建后首项为tools/evalscope原4K无/有/无观测HTTP。
三次全文600440，应为360284；输入4096、输出7、stop，三服务0、
质量驱动1。观测保持原输出，不等于质量通过，不使用观测时延衡量
性能。生产源码及接受二进制2392f6b1未变。

在GatedDeltaNetRegKernel<8>完成T=4096计算后读取每层S最终值；
完整36层prefill之后，在首36次GatedDeltaNetDecodeKernel启动前
读取实际S、conv后的BF16 QKV、a/b及dt_bias/A_log，启动后读取
更新S与原始y。没有捕获NormGate后的覆写值。检查实际形状为
16个Q/K头、48个值头、kd=vd=128、stride=10240，128线程，seq0。
其余decode步忽略；启动阶段无采集。36份元数据及两侧日志成功。

## 独立数学参考

直接用捕获的conv后Q/K按FP64 L2范数（eps=1e-6）归一化，Q再乘
1/sqrt(128)，每个Q/K头对应3个值头。输入a加dt_bias，经softplus
（ab>20按源码分支直接取ab），alpha=exp(-dt*exp(A_log))；beta
为独立稳定sigmoid。对每个值头，使用实际初始FP32状态S：

- delta = v - alpha * (k^T S)
- S_new = alpha * S + beta * k * delta^T
- y = q^T S_new，最后经FP32→BF16最近偶数舍入比较

另以实际更新后的S替换参考S_new，仅独立重算读出，以分开状态更新
与读出的误差。checkpoint只读验证dt_bias/A_log，未验证上游投影
或卷积的权重及输出；所以不能声称此轮建立完整GDN或linear层参考。

## 后续与证据

下一步在同一冻结输入上核对FP32归一化、衰减和递推/读出顺序，
解释20项输出差异；不先修改kernel或恢复已拒绝的路径统一候选。
同时保留完整[上游参考合同](UPSTREAM_REFERENCE_CONTRACT_2026-09-23.md)
中的attention、PLE和hyperconnection缺口。

证据：`.q4t-work/e2e/gdn-state-handoff-20260923/`含三侧HTTP、
36层前后S及输入快照、逐层reference.npz、state-reference.json、
artifact-binding.json。prepared同名目录保存构建及控制器，仓库
保留只读GDN交接观测和独立FP64分析模板。原4K任务错误仍未修复。
