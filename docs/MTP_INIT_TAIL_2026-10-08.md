# MTP 非末初始化分块尾部省略（2026-10-08）

整体Goal的第二个、最后一个优化候选；本页在实现和测试前冻结。
组合保留第一候选已知位置改动，但第一候选严格性能NO_GO不变。
主验收基线仍为21d85a17/control-off-01，7e7e1732仅辅助比较。
默认MTP关闭，不合并、部署或自动启用。

## 有界实现与数学/状态合同

MtpForward末尾增加默认Full的显式MtpForwardMode，
MtpDraftExtend末尾增加默认Full的MtpInitPolicy。不能将已有
compute_logits=false改义为“不需要hidden”。旧调用保持完整。
Skip只允许S1、无d_seq_id、完整max_prefill行、compute_logits=false，
非法组合在提交GPU工作前失败。输出sample/multi/logits允许null或
哨兵，在Skip模式均不写；Full且无logits仍生成hidden。

仅HTTP单流文本seq0初始化且T>8192时，对非末完整分块选Skip。
保留input/projections/attn_hc.mix及整个FullAttentionForward；
其后省略attn_hc.combine、mlp_hc.mix、BF16 MoE与final mixer。
非末块head原已省略。保留该边界cudaGetLastError及失败传播，
无新增同步；下一块继续同stream，输入来自对应main_trunk，
不读前块sample/multi。KV/raw/压缩indexer全部写入保留；末块
完整形状/计算、最终g/d0、decode/extend、B>1、旧调用保持。
保守预算、临时缓冲尺寸、初始化position回读不顺带优化。

计时BeginChunk末尾增加tail_skipped=false，绑定实际选中路径。
保留原10个marker；省略的四段mlp_hc/moe/mixer/head标为skipped，
stream_ms=null。实际marker物理间隔计入skipped_marker_gap_ms，
不能充作有效计算或记零。旧trace缺省false且保留旧闭合预算；
新四项gap累计的预定义预算为各active叶ULP32之和+总区间ULP32
+4*ULP32(gap)+3*ULP64(gap)，不从结果拟合、不改变数值exact合同。

## 固定验证顺序

独立build/mtp-init-tail-20261008构建q4t与q4t_tests，零警告；
产物与证据只进build/和.q4t-work/mtp-init-tail-20261008。

1. 第一测tools/evalscope HTTP固定质量11，同原输入/reference、
   S1/k3/greedy seed20260920、max_len208896/max_prefill8192。
   仅开启Q4T_MTP_INIT_TIMING=1，cycle关闭；同组记录承担真实
   11条init trace/40chunks/29次tail skip覆盖，不追加诊断矩阵。
2. 通过后一次有界parser合同（保留原合同，并增加旧记录兼容、
   长短拓扑、非法flag/末块skip/部分skip/null/ready/边界/gap闭合
   及11条身份覆盖）。离线核对本次质量11trace，cycle必须0。
3. 真实MTP直接A/B：T={8192,8193,8196,16384,16385}，C8192。
   同一二进制Full与Skip、同初态/shape/输入，比较完整KV/raw/comp、
   RoPE/page/输入与末块sample/multi/logits、最终g/d0逐字节一致；
   nonfinal Skip输出哨兵不写，有限值与guards通过。覆盖单块对照、
   末块1/4/8192行及连续两个非末块；非法Skip组合在提交前失败。
   测试可直接分块与wrapper互证，固定五个长度，不加长度扫描。
4. 一组长全模型集成：取原8192-token正式输入的token序列再追加
   固定四token形成8196；48层主模型、正式容量。Full/Skip各一次
   从ModelBeginSequence独立prefill（共享只读加载权重），各固定
   4个自然k3步骤加1个预定forced-d0拒绝，共10步，禁止追求接受
   分布追加步骤。比较初始化g/d0及draft完整KV/raw/comp，随后
   每步draft IDs、完整verify logits/trunk、完整extend logits/
   multi/sample、接受/token、g/d0、recurrent及draft状态exact；长初始化激活新分支。旧短17步证据按身份复用，不
   再跑旧矩阵，不声称有限输入等同所有跨模式数值证明。
5. 全部前置通过后唯一关闭所有诊断的正式五档各3次（15条）：
   1024/4096/8192/45056/204800输入，256输出；文本、usage、
   实际MTP、步数、容量及无fallback对原基线一致。

共26条新HTTP、5长度直接A/B、一组长集成；没有bench、warmup、
额外探测、k扫描或有利重采样。仅具体失败修复后重验受影响项，
首次失败保留。必要构建不算测试；测量时不并行构建/重CPU分析。

## 出口与解释边界

每档TTFT/decode_seconds/latency的候选后两次必须分别不超过
原基线后两次最大值；全部三次保留，不发明容忍百分比，不跨档
抵消。质量、有限数值、性能、默认启用分别记录。对第一候选的
辅助比较只说明新增尾部方向观察值，不能替代原基线严格15格。

旧测量非末尾部44K约0.330秒、200K约1.545秒，仅为机会规模。
省略MoE也改变host同步与Lt计划首次使用时点，故必须测整个请求；
不能承诺等量TTFT改善或主验证提速。验证约八成循环的主要工作
仍是主模型计算，其同步窗口含前序GPU工作，不当可删除纯开销。
本候选完成后统一封存完整成本账与剩余优先级，不扩第三候选。

## 实现前观察边界勘误

在任何候选测试前确认：Multi会覆盖中间草稿head logits，现有
接口只能取draft IDs及完整extend logits；冻结清单原“draft/verify
原始logits”措辞过宽。本页改为实际可读观察量，不加runtime插桩，
不改输入、调用数、exact条件或性能规则；原冻结文本保留在证据目录。

## 实现与必要构建快照（尚未验收）

服务962b5632、模型库cdc93096、测试96dc3188独立零警告构建。
生产范围为两个显式policy及server S1/seq0/text/8192分块入口，
计时增tail标记并保持10markers；28项parser合同、五T/30forward
直接三路径及长10步/2probe测试已编写但尚未执行。
128运行来源、独立测试/工具身份及56项HTTP来源绑定已封存。

首测前静态审查补齐工具身份与8192原始输入派生链；协议早期
两个prebuild版本保留，最终请求数、输入、数值/性能条件未变。
长测试完整draft缓存及recurrent在RAM逐字节比较，额外host约
5GiB；原始小输出受64MiB限制。中间草稿head logits不在观察量内。
下一项quality-on-01 HTTP11，构建和提交不代表验收通过。

## 质量、计时合同与有限数值结果

运行提交2d232a5。quality11/11首次通过，实际MTP及正式容量保持、
无fallback；服务/runner/采样器均自然0退出。28项parser首次通过，
真实质量11trace/40chunks/29tail skip逐条符合只跳非末块；旧15trace/
102chunks兼容只读复核通过，不追加HTTP。

五长度手工Full/Skip及wrapper共30forward首次全部exact；末块
sample/multi/logits、g/d0、完整KV/raw/comp/只读元数据、输入及
guards通过，Skip哨兵不写，六项非法调用在GPU提交前拒绝。
长8196初始化两次独立main prefill、Full/Skip各4自然+1强制，
共10步及2固定probe首次通过。两分支自然4次均接受3draft，
强制1次接受0draft；不追求其它分布追加步骤。初始化和每步约定
输出、完整draft缓存及recurrent逐字节相同，旧短17步覆盖1/2draft
的证据按旧身份/未改引擎复用，不将长测试声明为自然分布全覆盖。

`parser-contract-01.json`和`group.json`绑定来源/二进制/夹具/顺序。
当前满足五档性能前置，接下来唯一performance-on-01共15条；
不因有限exact通过宣称跨模式等价或默认启用。

## 最终结果：整体更快，严格性能仍NO_GO

固定26条新HTTP全部完成，无fallback，正式容量保持。五档输出、
usage和步数与primary21d及辅助C1均一致。服务/runner/采样器
自然0退出，未追加性能样本。与primary21d比较的三次均值如下：

| 输入 | TTFT秒 | 整请求秒 | decode tok/s | TTFT变化 | decode变化 | 整请求变化 |
|---|---:|---:|---:|---:|---:|---:|
| 1024 | 0.978173 | 9.780545 | 28.969464 | +0.186% | +1.936% | -1.695% |
| 4096 | 2.877812 | 11.575896 | 29.316800 | -0.067% | +1.588% | -1.195% |
| 8192 | 5.504490 | 15.293434 | 26.049798 | +0.038% | +1.898% | -1.187% |
| 45056 | 31.578135 | 43.412125 | 21.548101 | -0.819% | +1.758% | -1.069% |
| 204800 | 165.621796 | 175.089981 | 26.932301 | -0.696% | +1.263% | -0.726% |

Decode使用三次合计765/总decode秒数。平均TTFT在44K减少
0.260920秒、200K减少1.160944秒；整请求五档均减少，decode
五档相对primary改善1.263%–1.936%。严格后两次筛查14/15格通过，
唯一失败1K TTFT：基线后两次0.920309156/0.921143794秒，候选
0.923146147/0.919202951秒，第一值超冻结max **2.002353毫秒**。
故保持 **NO_GO_FOR_CANDIDATE_PERFORMANCE_ACCEPTANCE**。
三次固定先后采样不能证明这2毫秒是因果回退，也不能自行改规则。

辅助比较C1：44K/200K TTFT再减少0.309091/1.271006秒，整请求
减少0.633%/0.698%；短三档整请求增加0.060%–0.108%，五档decode
低0.135%–0.422%。以上差异完整保留，不解释为已证实的因果；
C1未接受，辅助比较不替代primary15格，也不宣称第二项额外提速decode。

原始主/辅助45条HTTP独立复核首次通过，114来源绑定，15格结果
完全相符，errors/pending为空。主摘要绑定quality/parser/numeric/
运行来源、128旧来源与128辅助来源的独立快照。资源采样C2性能组
821点、最大间隔1.004秒以内、进程VmSwap全0，观察VmHWM最高
3,947,740KiB、系统MemAvailable最低20,790,460KiB；这些不能
证明GPU统一内存的整机物理RAM峰值或新旧RAM差额。

153项最终保护检查通过，原两个dirty工作区、main和原13项构建
身份未变，新增前序7项构建及当前128运行来源保持；无测量进程。
核心证据在`.q4t-work/mtp-init-tail-20261008/`：
`candidate-summary-final-01.json`、`candidate-independent-review-01.json`、
`parser-contract-01.json`、`group.json`、`final-protection-audit.json`。
默认MTP关闭，未合并/部署；跨模式整模型数值NO_GO仍在。
本Goal两项候选全部完成，验证主体的后续优先级见整体报告。
