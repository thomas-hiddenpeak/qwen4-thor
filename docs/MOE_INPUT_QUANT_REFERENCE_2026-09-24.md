# 原4K完整MoE输入量化参考（2026-09-24）

## 结论

完整输入量化中，E4M3尺度编码有1208940项不符合最近偶数舍入，
全部位于ratio∈[248,432)，实际编码126（448），正确编码落在
120..125。覆盖48层、68119个(token,layer)组合。去除10个专家槽
重复后，是120894个(token,layer,group)；不是1208940个独立token。
已知高区间编码缺陷在本次完整4K输入上得到确认，不能把整个输入
量化判为正确。没有将修正尺度传播到生产或后续GEMM，没有建立
该缺陷与原4K错答的因果关系。

给定**实际尺度**，5033164800个FP4代码与独立CPU枚举参考全同。
这只说明FP4舍入按收到的尺度工作，不能抵消尺度本身错误。

## 来源与独立性

复用moe-full-gather阶段已完成无/有/无观测HTTP的冻结输入，先
重读校验来源manifest、所有实际映射/量化数据和完整HC输入。
无新增生产或观测器改动，未重跑HTTP；原错答仍600440，质量未
通过。两工具零警告构建；预算8GiB参考存储、20GiB余量。

CPU从实际列表恢复token/expert身份，从16个BF16元素重算gmax，
保留FP32的gmax/6、除input_scale、有效尺度乘法、倒数及输入乘法。
E2M1通过枚举0/.5/1/1.5/2/3/4/6的距离、相同距离选偶数编码，
独立重建全部FP4字节；没有调用生产FloatToE2m1Code函数。

设备独立重建gmax和除法，保存全部314572800个ratio；同时调用
已接受format.h的legacy编码函数解释实际输出，并调用CUDA原生
E4M3饱和转换作对照。CPU另枚举127个有限非负E4M3值，按距离
及编码奇偶实现最近偶数舍入，分别应用CPU和设备ratio。

- CPU/device ratio全部逐位相同，最近偶数结果也全部相同。
- CPU独立最近偶数与CUDA原生转换全部相同。
- legacy编码与实际SF全部相同，确认了当前实现的实际行为。
- 实际SF与最近偶数差异1208940项，全部为上述高区间；本请求
  输入量化没有触发其他尺度差异，不意味着子正规编码缺陷不存在。

全部48层各自512个专家input_scale位值相同。审计验证每个错误
(token,group)恰好对应10个不同slot，不是映射重复或计数放大错误。
这一事实解释了去重口径，也作为后续模型专用设计的配置事实保留。

## 证据及下一步

保存全部CPU/device ratios、CPU最近偶数SF、device-ratio最近
偶数SF、legacy/native SF、完整CPU FP4参考字节，以及全部差异
索引。逐层scale-error-details包括token、slot、group、ratio、
实际/正确编码，完整文件清单及SHA保留。实际输入/映射/尺度均
沿用上一阶段checkpoint与HTTP绑定，不使用人工构造输入替代。

证据`.q4t-work/e2e/moe-full-input-quant-20260924/`；主要文件
plan、input-binding、reference、scale-error-summary、summary及
artifact-binding，模板`tools/verify/moe_full_input_quant/`。
format.h摘要24c372233cebbb73ca88ee9b69e8937c4bf97cf2dbb096975eb85132eed24247，
接受二进制及生产源码未改。该结果不替代完整模型质量/五档性能门禁。

下一步将完整FP4/SF绑定到实际GU GEMM消费者，核对专家投影、
中间量化和down/shared路径；所有参考继续明确“使用实际错误SF”
与“修正SF传播”的区别。已失败的运行时候选不能仅因新增错误
计数就视为可接受，仍需新的可检验策略及完整E2E验收。
