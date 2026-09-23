# 原4K请求：lm_head全词表与greedy交接

2026-09-23。生产运行时未改，仍为已接受的2392f6b1二进制。
原题期望360284，实际600440，未修复；本报告不代表整模型正确性通过。

## 先行HTTP与观测范围

新观测器零警告编译后，第一项测试为tools/evalscope原4K请求的
无观测→有观测→无观测三侧HTTP。三侧均输入4096、输出7、文本600440、
stop、服务退出0，质量驱动退出1。该结果仅通过观测不改变本请求的门禁，
不是质量通过；观测耗时不进入性能比较。

观测40份元数据完整，涵盖末prefill和后续6次decode。每步保存实际
lm_head输入2560个BF16和全部248320个logits；首步分块保存实际GPU
lm_head权重[248320,2560]，后六步逐字节确认整份权重未变。权重
1271398400字节与checkpoint的lm_head.weight完整比较一致。
原PLE查表观测的56份记录逐字节不变。

当前普通请求在prefill和decode都只对最终一行调用BF16 GEMV，
alpha=1；没有将prefill误当成4096行输出投影。最终mixer产生的输入
在本阶段视为实际给定操作数，尚未独立证明其计算正确。

## 独立重算

CPU读取实际权重和输入，按32个lane、每lane偶奇两个FP32显式fma
累加链、二者相加、16/8/4/2/1树归约、BF16 RNE重算。共1738240
个输出与HTTP记录逐位一致。CPU程序零警告，执行退出0。

另以FP64对全部权重和输入做点积，转FP32再BF16后共109项不同，
每步差异为40/11/14/10/7/7/20；所有差异索引和完整FP64分数保留。
FP64原始分数、FP64舍入后的分数及实际BF16分数，七步argmax均相同。
不将FP64差异隐去或设置临时容忍阈值。

| 步 | 选中ID | token | 实际最大分数 | 实际次大分数 | FP64第一与第二名差 |
|---:|---:|---|---:|---:|---:|
| 0 | 21 | 6 | 20.625 | 18.25 | 2.301917 |
| 1 | 15 | 0 | 22 | 18.25 | 3.684804 |
| 2 | 15 | 0 | 23 | 19.25 | 3.756347 |
| 3 | 19 | 4 | 23 | 18.375 | 4.606420 |
| 4 | 19 | 4 | 22 | 20.875 | 1.046302 |
| 5 | 15 | 0 | 22.375 | 20.25 | 2.124613 |
| 6 | 248046 | 结束标记 | 24.5 | 16 | 8.491554 |

这里所有最高分均唯一，不宣称实际覆盖了最高分平局场景。

## 选择与交接

prefill在CPU从低ID向高ID扫描、严格大于才更新；decode在GPU分970
块再归约，平局选低ID。CPU独立argmax逐项核对6×970=5820组局部
最大值及其索引，六个最终GPU token全部一致。每次GPU选择读取的
logits指针与刚捕获的lm_head输出相同，完整输入字节也相同。

前六步的独立argmax与下一步实际embedding输入token逐个一致；
末步248046属于实际generation_config中的停止token，HTTP确实stop。
用checkpoint tokenizer映射前六个ID得到600440，与三侧HTTP一致。
第0步没有直接捕获CPU局部变量，以完整logits、实际下步输入及HTTP
进行行为核对；不把它表述为CPU选择变量的直接观测。

在给定实际最终mixer输出的条件下，本请求错误答案不能归因于
lm_head指定累加与FP64之间的差异、GPU最大值选择或下一步token
交接。最终mixer、完整prefill GDN状态以及已知E4M3尺度编码问题
仍需处理；本实验没有证明错误答案的上游原因，也不覆盖其他请求、
长上下文、MTP、多序列或多模态。

## 证据与复现

证据目录：`.q4t-work/e2e/output-head-reference-20260923/`。
包含三侧HTTP原始数据库/日志、实际权重/输入/logits/选择中间值、
checkpoint-binding.json、cpu-reference.json、output-reference.json、
previous-binding.json、FP64全量结果和差异索引、artifact-binding.json。
核查脚本另重读checkpoint、CPU结果和选择链，退出0。

永久工具位于tools/verify：output_head_observer.cpp.in、
capture_output_head.py.in、verify_output_head.py.in、
replay_output_head_cpu.cpp.in、audit_output_head.py.in。
将模板放入独立prepared目录后构建observer.so与replay，构建日志零警告，
先执行run.py完成HTTP门禁，再执行analyze.py与audit.py。
目录名固定为本次证据名，脚本拒绝覆盖已有证据；新实验必须使用新目录。
生产代码、构建配置和正式性能参考均未修改。
