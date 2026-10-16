# GDN anchor + rank-one record 有界候选

状态：2026-10-16验收收口，**NO_GO**。默认MTP decode五档几何平均
+0.681%，但44K/200K TTFT触发冻结保护门槛；生产未接入。

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
GPU验收要求与同机HC工作全程互斥。实际发生的两次环境中断另行
记录，不能把受干扰的尝试充作正式结果，详见下方环境边界。

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

## 实现与运行身份

候选提交为
[`dcb2a7d07c6aba1c319d2c689c5e752c5616997d`](https://github.com/thomas-hiddenpeak/qwen4-thor/commit/dcb2a7d07c6aba1c319d2c689c5e752c5616997d)，
q4t SHA256为
`b154e4a7c1ccf8ccd5d83d28f8464ecc9e068b830d680aae26d2ec15117e0ac5`。
三个target（q4t、q4t_tests、q4t_mtp_lifecycle）构建成功且零警告。

关键实现可直接检查：

- [rank-one恢复kernel](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/dcb2a7d07c6aba1c319d2c689c5e752c5616997d/src/model/linear_attention.cu#L1417)：只读anchor和接受的records，逐元素恢复。
- [新旧verify分派](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/dcb2a7d07c6aba1c319d2c689c5e752c5616997d/src/model/linear_attention.cu#L1883)：保留旧kernel，single和multi分别命中新record路径。
- [恢复接线](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/dcb2a7d07c6aba1c319d2c689c5e752c5616997d/src/model/model.cu#L1746)及[诊断快照](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/dcb2a7d07c6aba1c319d2c689c5e752c5616997d/src/model/model.cu#L1977)：按最近verify表示恢复，诊断重建用临时scratch。
- [新边界差分测试](https://github.com/thomas-hiddenpeak/qwen4-thor/blob/dcb2a7d07c6aba1c319d2c689c5e752c5616997d/tests/mtp_checkpoint_layout_test.cpp#L549)：已编译，因性能门禁失败未执行。

每组前后记录隔离source的commit、空patch、全文件manifest、binary、
CMakeCache及runner SHA，正式两组全部一致。runner自带的commit.txt /
worktree.patch属于主工作区的HTTP harness，不能代替隔离候选身份。

## 实测结果与裁决

正式证据只采用`http-resume/`。默认MTP质量11/11；五档各3次，15/15
性能请求输入/输出/finish与确定性检查通过，提示与生成文本均同已接受
基线。两个HTTP runner与server均正常退出、无清理失败。顶层driver
exit 1是检测到NO_GO后的预定停止，不是服务崩溃。

吞吐采用每档三次decode_tps调和平均（相同255个后续输出token），
五档比值几何平均 **+0.680728%**；五档decode范围均整体优于基线。

| 上下文档 | 基线decode tok/s | 候选decode tok/s | 变化 |
|---|---:|---:|---:|
| 1024 | 34.5229 | 34.7560 | +0.675% |
| 4096 | 35.4635 | 35.6614 | +0.558% |
| 8192 | 37.3846 | 37.6554 | +0.724% |
| 45056 | 35.8875 | 36.1279 | +0.670% |
| 204800 | 33.1678 | 33.4252 | +0.776% |

| 上下文档 | 基线TTFT范围（秒） | 候选TTFT范围（秒） | 保护门槛 |
|---|---:|---:|---|
| 1024 | 0.898795–1.063480 | 0.898419–1.065339 | 未触发 |
| 4096 | 2.912987–2.956810 | 2.911842–2.973856 | 未触发 |
| 8192 | 5.610536–5.633046 | 5.618213–5.638024 | 未触发 |
| 45056 | 31.871076–31.918835 | 31.942685–31.949691 | 失败 |
| 204800 | 166.945850–166.996610 | 167.166324–167.225591 | 失败 |

44K和200K满足`min(candidate TTFT) > max(baseline TTFT)`，平均TTFT
分别增加46.728ms和231.356ms。因此即使decode收益门槛通过，也按冻结
规则判定 **NO_GO**，不以吞吐收益抵消TTFT。未增加样本或重跑基线。
这是三次样本的预定操作门槛，不是统计显著性、长期稳定回退或因果证明；
本轮没有定位TTFT差异来自何处。

质量DB的HTTP状态码字段为空；采用其成功标志及明确记录的响应身份、
时间和输入输出合同，不补造HTTP 200。质量与文本相同不能证明SSM逐位
正确，也不能代替近tie合同。

| 验证项 | 本轮结果 |
|---|---|
| 默认MTP质量 / 五档HTTP输出合同 | 11/11、15/15通过 |
| 默认MTP性能保护门槛 | NO_GO：44K、200K TTFT |
| off质量 / off五档 | 未运行，按冻结出口停止 |
| checkpoint / samepath / multi / 新边界差分 | 已编译，未运行 |
| lifecycle / 完整256步N=4近tie与causality | 已编译，未运行 |
| HTTP尾部、取消及后继恢复 | 未运行 |
| bench / profile / 新参数扫描 | 未运行 |

完整模型near-tie诊断仅恢复row0，未来即使通过也不能单独证明整模型
的a1/a2 fold逐位正确。准备的新旧差分主要覆盖两层含PLE模型，且
SnapshotCheckpoint共用恢复helper，不能充当独立oracle。当前未执行，
不能宣称这些边界已通过；既有主线数值证据也不覆盖新候选。

## 环境、证据与交付边界

在HC会话完整收口（9d16fca）、主工作区登记独占窗口（df182c5）后，
才执行本次正式矩阵。两组全程每2秒检查`/proc`，未检测到竞争GPU
进程；它是协作排程加尽力监控，不是原子GPU锁或绝对排他证明。
正式26条请求结束后释放GPU，不继续off或GPU细分析。

此前`http/`在模型加载期间重叠，0条HTTP；`http-clean/`在质量组
期间重叠，服务观察11次尝试/10次完成/1次取消。第二组DB未提交，
恢复副本仍0条可复用响应，因此原11题按原字节重新采集。两组保持
ENV_ABORT，不记候选失败、不补造质量通过，也不扩大正式性能样本。
详见[环境记录](GDN_GPU_QUEUE_2026-10-16.md)。

[证据归档](evidence/gdn-record-fold-20261016/README.md)包含固定协议、
原始结果、输出、逐组身份、环境中断、恢复证据和构建日志；
[最终裁决](evidence/gdn-record-fold-20261016/raw/final-decision.json)与
[逐档原始比较](evidence/gdn-record-fold-20261016/raw/http-resume/on-comparison.json)
可复算结果。大型DB、请求文件和binary留在原绝对路径，大小及SHA256
列入raw-manifest；不把归档动作当作验收通过。

通过保留候选为第二父提交的`ours`合并记录研究历史，工作分支最终树
不带候选运行时代码。报告、证据和候选历史一并推送工作分支，main未改。
生产src/include/CMakeLists与main 28a3783相同，build/q4t仍为
`be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072`；
相同身份直接复用既有证据，不为文档或归档重复完整推理。

本轮只否决这个anchor＋逐层fold变体，不否定NInfer的全部Record/Fold
实现。静态少写约212.6MiB/verify，本轮观测仍不足以接受整体性能，
也未带来分配容量节省。该方向暂不进入生产；GPU接受元数据、合并fold
或独立草稿head仍需各自的新协议与证据，本轮不自动追加候选。
