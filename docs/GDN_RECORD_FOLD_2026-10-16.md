# GDN anchor + rank-one record 有界候选

状态：2026-10-16候选实现并零警告构建；尚未验收，不是已接受优化。

用户在外部实现源码评估后授权沿选定方向继续研究。本轮只检验减少
MTP verify的GDN checkpoint写入，不展开GPU接受元数据、草稿head、
QSA驻留或其他优化。代码基线为main 28a3783的运行时，文档起点
a2353b6；与另一个HC up复审独立。生产build与main保持原身份。

## 从参考实现到本项目

NInfer的ReplaySSM记录紧凑中间量，verify后按接受前缀更新持久状态。
本项目已经在verify内写入最终状态，且始终接受输入token0（bonus）；
全接受直接保留最终状态。因此首轮采用更小的变体：

| 接受draft数a（k3） | 状态动作 |
|---|---|
| 0 | 复制token0后的完整anchor |
| 1 | 从anchor重放token1的rank-one更新 |
| 2 | 从anchor重放token1、2的rank-one更新 |
| 3 | 保留verify写入的最终状态，不重放 |

记录直接取自verify实际计算的FP32 normalized K、delta、alpha、beta。
更新维持 `alpha * old + (beta * k) * delta` 的运算顺序，不重新计算
归一化、exp、sigmoid、kS或投影，不压成BF16。不以源码表达式相同
代替编译后逐位验证。

## 冻结实现范围

- 旧single/multi GDN kernel及helper保留，新增record kernel；仍传入非空
  checkpoint，避免误进默认GDN_REG而改变归约。生产入口是
  MtpSpeculativeStepMulti → ModelVerifyMulti（含B1/T4）。
- ModelDecodeBatch和ModelVerifyMulti均接入；保存至少2行且所有线性层
  满足kd*vd >= kd+vd+2时启用。其余路径保留完整checkpoint。
- 原allocation、cap/pooled slot stride和预算不变：每slot row0为完整
  SSM，row1起紧凑排列records[rows-1,nv,kd+vd+2]。conv/PLE不变。
- Model新增诊断A/B启用标志和最近成功verify表示标志；后者与rows/slots
  一起失效，沿用各入口成功边界后发布：生产multi确认stream完成，
  旧异步ModelDecodeBatch只确认提交成功。重复restore从不可变anchor开始。
- SnapshotCheckpoint在诊断临时GPU scratch中重建，不能改live状态；
  raw完整行诊断显式使用旧表示。无未计费持久镜像。
- 不改接受算法、KV、模型权重、精度、默认k、调度策略或CLI。

36层、nv48、kd=vd128、k3、单活跃序列的静态SSM写payload：

- 原3份：324 MiB。
- anchor：108 MiB；两条记录：3.4013671875 MiB。
- 合计111.4013671875 MiB，减少212.5986328125 MiB/verify。

这是写payload上界账目，不是测得的带宽或吞吐收益。原324 MiB的SSM
checkpoint分配仍在；本轮不能声称节省分配容量。partial恢复增加
逐层kernel调用与读写，full accept无额外fold。

## 冻结验收与出口

只实施一个候选，不扫描参数。所有产物位于
`.q4t-work/gdn-record-fold-20261016/`，隔离检出source/、独立build/。
同机HC up测量结束后才构建/测试，不污染其计时。

1. 零警告构建；第一项测试为tools/evalscope真实HTTP：
   默认MTP质量11题，继而on五档×3；通过再off质量11题、off五档×3。
   固定输入及reference复用
   `.q4t-work/mtp-numerical-closure-20261016/http/`。
   基线binary SHA为
   `be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072`。
2. 质量、确定性、输入/输出/finish合同须通过。逐档分别比较TTFT和
   decode，不以短档收益抵消长档回退。沿已用重复范围判断：
   候选三次decode范围整体更差，或TTFT范围整体更慢，则不接受；
   范围重叠只表明本轮未辨识回退，不证明统计等效。
3. 这是增加恢复逻辑的候选。若无至少一档decode范围整体优于基线且
   五档吞吐比几何平均>1，则按无可辨识收益NO_GO，不仅凭少写字节
   接受。此处未继承上一有界任务的+5%目标；不把三次样本宣称为稳定
   尾延迟或长期收益证明。
4. HTTP门禁通过后，统一执行受影响checkpoint/samepath/multi/旧MTP
   合同，新增生产B1/T4新旧同形状差分（含非零初始状态、PLE、
   a0/1/2/3、重复restore、拒绝尾poison、两轮、过期失效）。
   必须断言record分派命中，不能用fallback绿灯替代。
5. 完整模型256步N=4近tie与causality仍必需；原数值测试明确断言
   新表示。prefill、PLE上传等未改实现的专项证据复用；
   HTTP尾部/取消恢复按影响复核，不跑bench/profile。

失败原样保留；仅具体实现缺陷允许修复后重验受影响项。不为微小
性能差扩样本或改变门槛；HTTP/性能失败时停止后续无必要的细分析，
保留候选与证据，生产不接入。提交/推送只表示阶段快照，不表示验收
通过；即使接受也不自动合入main。

## 本轮结果

q4t、q4t_tests、q4t_mtp_lifecycle构建成功且零警告。
候选q4t SHA256：
`b154e4a7c1ccf8ccd5d83d28f8464ecc9e068b830d680aae26d2ec15117e0ac5`。
尚未运行任何测试；下一项为真实HTTP。完整模型near-tie诊断只恢复
row0，不能单独证明整模型a1/a2重放逐位正确；新恢复逐位对照主要覆盖
两层含PLE模型，完整实际使用路径另由HTTP覆盖，结论不扩大。
