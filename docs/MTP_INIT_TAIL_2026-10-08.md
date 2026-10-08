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
   每步draft/verify原始logits、trunk、接受/token、recurrent及draft
   状态exact；长初始化激活新分支。旧短17步证据按身份复用，不
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
