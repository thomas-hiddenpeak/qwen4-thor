# MoE grouped GEMM 静态审查与候选取舍

2026-11-06。用户要求结合已有耗时推进、暂不追加测试，本阶段完成
源码、既有记录和接口合同审查。**未找到足以支持整体≥3%收益的
算法选择修正；发现可明确删除的描述符重复构建，保留最小未应用补丁。**
没有构建、单测、HTTP、模型加载、GPU调用或新的性能采样；没有调用
cuBLAS heuristic API。结论不是运行时验收，也不是MoE性能已达上限。

## 身份与复用证据

- 审查主线`def8cc96e7f94b50fbc917303c8570ec23e841ea`，默认ELF
  `c1bdb0a7`/sidecar `0efc2c0d`。223项接受来源全文摘要与主线一致。
- 研究分支入口`067cfc5ddf719636a8967f424ccf121f3f756e14`；本次
  只新增文档及离线证据。其MoE源码与接受默认一致，已有GDN研究的
  三文件差异保持NO_GO，不能作为下一候选的隐含运行基线。
- 复用[当前默认分析](DEFAULT_VERIFY_PROFILE_RESULT_2026-11-06.md)
  的1K/45K、full/subset四组数据。当前日志确认R40 expert-grouped
  GU/DN、cuBLASLt130501；未采集当前逐专家M_e分布或算法ID。
- 本机头文件版本13.5.1.27；官方在线文档访问时为cuBLAS13.5。
  头文件、源码、原日志及分析输入SHA见[离线重算](
  evidence/moe-grouped-static-20261106/analysis.json)。

## 平均行数提示没有被证明错误

当前T4、top-k10产生R40条路由。device plan将其按专家合并，活跃
专家数为U，各自M_e行。cuBLAS descriptor仍固定40组，后40−U组
`dim_m=0`，并未把descriptor数量改成U。因此：

```
sum(M_e) = 40
全部40个descriptor的平均M = 40 / 40 = 1
仅活跃专家的平均M = 40 / U
```

这是由当前布局得到的算术事实。D采用列主序视图[N,M_e]，源码
`avg_n=n`对应D的rows，`avg_m=1`对应D的cols，不能直接把变量名称
理解为行列颠倒。官方只说明这是D列数的平均提示，未明确零列组是否
有内部特殊估算；因而不能宣称1必然最优，也没有依据把1改成活跃均值。
参见[平均提示接口](https://docs.nvidia.com/cuda/cublas/index.html#cublasltmatmulpreferenceattributes-t)。

旧2026-10-13诊断最后累计2560次调用、102400路由、70018个活跃
专家，U/R约0.683770、R/U约1.462481。该记录来自旧5330e50e/
cbc6ad93，只有累计总数，没有M_e直方图，不能作为当前默认的形状
分布，更不能据此推导当前应选M2算法。当前证据缺口明确保留。

## 只取一个heuristic不是已证实的性能缺陷

[GroupedProjection](../src/quant/moe_decode.cu)按R/N/K/workspace及
grouped模式缓存计划，查询只发生在新key，不是每层重新选算法。
官方返回顺序按预计计算时间排列，因此返回多个但继续取第一个，不
会自动产生新优化。选择其他算法需实际记录候选、适用条件和数值
变化；现有记录没有这些信息，也没有证明存在更快的可用算法。
参见[heuristic合同](https://docs.nvidia.com/cuda/cublas/index.html#cublasltmatmulalgogetheuristic)。

该API可能加载CUDA内核；本轮没有把“只查询算法”当成纯CPU离线
操作执行。也没有为了取得U而增加每层device→host读取/同步，或
修改M_e、padding、NVFP4量化、scale atom和BF16中间输出。

接口另有一项待核对边界：本机头文件/官方表将三个平均提示的类型
标为uint32_t，当前源码传int64_t及8字节。既有运行成功不证明跨版本
字节宽度合同；没有回读实际属性，不能据此断言当前值被忽略。这项
不混入性能候选，后续若核对或修改，单独绑定库版本与算法身份。

## 确定存在的重复工作：批描述符构建grid

`MoEDecodeGroupBatchKernel`用`i=threadIdx.x`，不读blockIdx；每个
线程为一组填写权重、激活、scale、输出、alpha/beta和维度指针。
GU/DN两处却均用`<<<R,128>>>`启动。因此T4时40个block重复执行
同一组写入，存在多个block写同一字段；不是40组各由一个block处理。

[未应用补丁](evidence/moe-grouped-static-20261106/descriptor-grid.patch)
只将两处grid改为1，并增加`kMaxRows<=128`静态断言：

- 入口限制T≤8、k10，R≤80，一个128线程block覆盖全部descriptor。
- T4每层这两次调用的block数80→2，**launch数仍为2**。
- descriptor数量仍为R；M_e、offset、row_of_flat、量化、两次GEMM、
  workspace、stream次序及固定slot FP32合并保持。
- T1与显式per-row分支不受影响；grouped T2..8均属于潜在影响范围，
  不能只验T4就接受全部批量、多槽或小prefill尾块。
- 新实现预期消除重复写者并产生相同字段，但本轮只有源码依据，
  没有独立描述符、GEMM中间值或FP32最终输出的运行验证。

补丁没有应用到运行源码，没有生成候选ELF。其意义是明确的工作量
简化，不能把97.5%的block减少比例当作总耗时收益。

## 收益预算：小简化不承担3%目标

| head / 输入 | GU+DN投影 ms/轮 | 两个准备段 ms/轮 | 达到3%需省 ms/轮 |
|---|---:|---:|---:|
| full / 1K | 16.871 | 1.335 | 2.838 |
| full / 45K | 16.332 | 1.336 | 2.960 |
| subset / 1K | 16.884 | 1.327 | 2.497 |
| subset / 45K | 16.383 | 1.332 | 2.625 |

准备段还含plan、gather/量化等，描述符构建只是其中一部分。以既有
粗计时总轮次估算，即使整个两个准备段都消失、其他成本不变，轮次
吞吐也只提高约1.33%–1.57%。这不是严格硬件上界：细计时含观察
扰动及可能的提交间隙，与粗计时组合只用于投入筛选，不排除未测
下游影响。它仍不足以支持把本补丁列为≥3%的主优化候选。

若由GU+DN本身承担3%目标，需要该段约14.8%–18.1%的净耗时下降。
目前没有支持这种幅度的算法/布局证据。已有按专家复用、SwiGLU+
量化融合、host计划缓存都已实现，不重新计为待获得收益。

## 既有直接用例不能代替新候选的完整合同

静态阅读[moe_decode_grouped_test.cpp](../tests/moe_decode_grouped_test.cpp)：

- RoutingA注释写token0/1共享、U30，实际代码为`t*10+s`，T4时
  全部40专家不同，即U40。RoutingB为`t*5+s`，U25，确有重叠。
  原运行结果保持，但不能继续把A描述为重叠样本。
- 用例只固定T4，没有覆盖T2..8或所有token共享同一组专家的U10。
- RunMulti先将FP32合并结果转换成BF16再比较，并采用≤4 ULP门槛；
  不能据此证明最终FP32逐位相同。缺模型分支也不能算数值通过。

若后续单独实施grid简化，需保持性能优化的HTTP前置顺序；之后直接
合同至少核对有效/空descriptor字段、T2/4/8及U=R/U=10/不均匀
重叠、原GU/DN BF16输出、非零初始y的完整FP32累加、T1/per-row保护。
必须使用独立字段参考并绑定实际形状，不能仅复用两个近似比较用例。
这里仅列验收要求，没有新增/运行用例，也未降低既定性能接受门槛。

## 有界决定与后续入口

| 候选 | 本轮决定 |
|---|---|
| avg_m改为活跃均值 | 不实施：固定R布局没有证明提示错误 |
| 扩大heuristic搜索/更换算法 | 暂缓：无当前分布、算法身份及收益依据 |
| 两处描述符grid收缩 | 保存最小未应用补丁，不作为3%主候选 |
| 新默认或合入运行代码 | 不接受：没有新运行时证据 |

当前静态审查到此收尾，不为了填满优化计划扩展参数搜索或新增采样。
如果目标继续要求明显收益，推荐下一项先审查长提示词QSA prefill；
这是建议，尚未启动其实现或验证。

作为投入依据，离线汇总同一批既有45K记录的5×8192+4084行：QSA
core约9.200–9.208s、GDN状态段8.696–8.704s、MoE5.916–5.938s。
这些是整个请求的prefill分组时间，不能与上表每轮decode毫秒混算；
也不能把core全归于稀疏attention、把状态段全归于递推kernel。
无插桩首字等待约占总响应82%–83%，因此该方向有更大的业务时间空间。
QSA当前已有GQA复用、异步双缓冲及shared padding，GDN已有寄存器
state/归一化预计算，后续审查不重复推荐这些已完成能力。

## 交付与复核方式

[derive.py](evidence/moe-grouped-static-20261106/derive.py)只读现有
文件，输出来源SHA、预算算式和未应用patch文本；本次只执行这种离线
汇总及文本审查。原始完整记录保持原路径，未复制模型权重。
本轮源码/默认/sidecar不变，研究GDN与末行head的NO_GO仍保持。

文档阶段提交使用`[skip ci]`，遵守“不追加测试”，不手动触发工作流。
本仓库工作流只有push/pull_request/workflow_dispatch入口；跳过行为
依据[GitHub说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs)。
提交推送只表示审查与提案已保存，不表示任何性能、数值或部署接受。
