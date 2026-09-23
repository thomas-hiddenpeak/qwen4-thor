# 最终mixer：原4K请求的七步局部参考

2026-09-23，生产运行时未改，2392f6b1二进制。原题期望360284，
实际600440仍未修复；局部参考一致不代表整模型正确性通过。

## HTTP先行与边界

新观测器零警告后首项tools/evalscope原4K无观测→观测→无观测。
三组均输入4096、输出7、文本600440、stop、服务退出0、质量驱动
退出1。只通过观测不改变结果的门禁，不是质量或性能验收。

82份元数据完整；实际embedding输入划分七步，每步计数48次主干
写回、49次独立HC归一化。第48次写回末行的实际指针绑定最终
mixer归一化输入，接着逐指针绑定down→原地SiLU→up→混合→lm_head。
同时保存各边界完整字节核对，避免将同形状的中间层当成输出头。

prefill与decode的最终mixer均T=1、hc=4、hs=2560、lowrank=320，
epsilon等于FP32的1e-6，SiLU输入先除以4。三组实际权重每步均与
checkpoint的model.language_model.hyper_connection_mixer下对应
hc_norm、input_mix_weight_down、input_mix_weight_up完整字节相同。

此前输出头114份快照逐字节不变，包括全量lm_head权重、全部logits
和greedy链。末prefill与首decode主干输入还与既有HC第47层写回
快照逐字节绑定；其余五步只证实本轮实际写回与mixer入口交接。

## 数值参考

| 操作 | 七步输出总数 | 指定参考与实际BF16差异 | FP64参考差异 |
|---|---:|---:|---:|
| 分组RMSNorm | 71680 | CPU 0；设备rsqrt+CPU 0 | 0 |
| down/up投影 | 73920 | CPU双累加链/warp树 0 | 4 |
| SiLU(down/4) | 2240 | FP32分步CPU 0；设备SiLU 0 | 2 |
| sigmoid门控四分支混合 | 17920 | CPU 0；设备sigmoid+CPU 0 | 0 |

每项参考使用该项实际输入，独立检查局部算术，不把逐项参考当成从
模型输入开始独立生成的整条前向。所有CPU构建和设备数学辅助构建
零警告；执行及核查退出0。设备辅助只调用基础数学函数，不调用
生产归一化、投影或混合kernel。FP64差异索引全部保留，不设阈值。

投影的FP64差异为步骤0/1/3的up各1项、步骤6的down 1项，全部
由CPU指定FP32累加顺序逐位复现。

两个SiLU差异为步骤2/5的第74项：实际除4后输入为−91/−94，
当前v/(1+exp(-v))在FP32的exp阶段溢出为正无穷，结果为负零
(0x8000)。FP64值分别约−2.7431119944e−38、−1.4107386169e−39。
显式保留FP32 exp溢出、加法、除法后CPU结果全部相同，设备SiLU
也相同。这是有限精度行为差异，不能笼统称为普通最后舍入，也没有
证据证明它导致原错误答案。本轮没有改变公式或重置精度标准。

## 分析失败与修正保留

首版分析把hc_norm权重存储形状写成[4,2560]；checkpoint实际为
[10240]，因此在权重数值比较前断言失败。保留initial-analyze脚本/
日志与初始空reference目录，改正存储形状后复用已通过HTTP门禁的
冻结快照，不改生产代码、不重跑或挑选HTTP结果。

SiLU首版CPU参考的FP64→FP32转换在上述两项产生overflow警告，
记录完整保留。后续显式记录每步溢出数量并将超出FP32最大值的exp
设为正无穷，再作FP32运算；两版最终CPU输出相同。该处理解释
实际有限精度语义，不是裁剪输出或忽略差异。

## 结论边界与下一步

最后一层写回→最终mixer→lm_head→greedy→下一步输入，在本请求
七步范围内已接通并符合指定局部算术。原错未修复，完整prefill GDN
状态未独立生成，已知E4M3尺度编码问题也仍在；下一步优先补完整
prefill GDN状态参考，不能据此开始宣称模型已正确或直接转入优化。

证据：`.q4t-work/e2e/final-mixer-reference-20260923/`，含HTTP原件、
observed-captures、checkpoint-binding、previous-binding、hc-binding、
FP64/CPU/device参考、失败记录与artifact-binding。工具保存在
`tools/verify/final_mixer/`，模板复制到对应prepared目录，先构建
observer.so零警告并运行run.py，再构建/运行离线参考。
分析顺序analyze.py→silu.py→audit.py；脚本拒绝覆盖已有主参考目录，
新实验必须使用新证据名。运行时、性能参考与其他任务内容均未改。
