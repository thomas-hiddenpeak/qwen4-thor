# MTP B1 已知位置候选（2026-10-08）

这是整体Goal的第一候选，沿用同一工作分支；基线为完整循环
诊断二进制`21d85a17`及其control-off-01，默认MTP关闭。
本页在实现前冻结范围与验收，不代表已通过。

## 唯一实现改动

MtpForward增加末尾可选int max_position=-1，向已有
FullAttentionForward参数传递。仅MtpSpeculativeStepMulti的B1
草稿和extend调用提供其同一host positions数组的精确最大值：
draft为P+j-1，extend为P+a。B>1和所有其他调用保留-1回读。
不改变positions指针、上传、KV/indexer/RoPE写入、GEMM行数、
精度、草稿k、接受/回滚/追赶语义或已有其他同步。生产改动仅
mtp.h/mtp.cu；测试注册另改CMake，不改attention计算实现。

去掉的是已有host整数信息的重复D2H与同步。下一处MoE counts
及阶段末尾读回保持，不能把旧positions等待窗口当可删除计算。
无需新增GPU持久分配或预算项。

## 冻结执行顺序与通过条件

产物build/mtp-known-position-20261008，证据
.q4t-work/mtp-known-position-20261008。独立必要构建完成后：

1. 第一测仍是固定quality11的tools/evalscope HTTP，沿用既有
   输入/reference、正式208896/8192/S1、MTP k3、默认诊断关闭。
2. 通过后运行一次新增attention直接合同：T={1,4}乘
   max_position={8194,8195}四组，实际MTP attention权重、有限
   确定性输入及缓存初态，pooled slot0。相同初态分别使用-1回读
   和显式正确max；输出及完整KV/raw/comp/RoPE/page/输入/位置
   观察逐字节一致，检查有限值与缓冲边界。四组包含两侧indexer
   路径；分界是floor((max+1)/4)>2048，不能误写成8192。
3. 固定现有17-step全模型k3回放运行一次，与旧last-row阶段
   123个原始文件（46,300,139B）逐文件/逐字节比较；覆盖自然
   accepted_count1/2/3/4分布2/1/2/11，另forced count1。
   保持独立argmax、checkpoint raw-reader和同形replay合同。
   旧文件不含完整recurrent/KV快照，跨版比较结论仅覆盖实际
   保存内容，不把单次内部状态回放提升为所有状态跨版证明。
4. 关闭诊断的正式五档各3次（15条），固定旧输入、256输出、
   greedy seed20260920。每条输出、usage、步骤与无fallback
   同基线。每档TTFT/decode_seconds/latency的候选后两次均
   不超过基线后两次最大值；保留全部三次，不设新容忍百分比。

共26条新HTTP，四组attention A/B、一次17-step replay；没有
额外warmup、k扫描、bench或开启诊断的第二矩阵。只为具体
失败修复后重验受影响项；失败原件保留，禁止追加有利采样。
数值exact失败不得用质量分数或性能收益覆盖。

## 边界和第二候选

此候选只为S1 k3编排，不解决普通/MTP跨形状数值证明，不能
自动默认启用。attention小合同不证明真实长文本模型质量；
HTTP五档也不证明所有模型输入等价。B>1、非零slot和其他调用
保持旧路径，仅作源码身份审查，不新增无关完整推理。

先做这一候选，再依据净decode、TTFT、整请求及复杂度结果
决定是否值得第二项；不为了用满数量实施小收益复杂改动。

## 实现与必要构建快照（未验收）

仅2个MTP生产文件传递可选整数，CMake另注册独立边界测试。
服务`7e7e1732`、模型库`0c7ba3b9`与q4t_tests独立零警告构建。
128个运行来源和独立测试身份已记录；尚未运行任何候选测试。
运行补丁和固定numeric runner封存在候选证据目录。下一项是
quality-on-01固定11条HTTP，构建/提交不代表性能或数值接受。

## 质量与有限数值结果

运行提交`112779c`。第一测HTTP质量11/11首次通过、正式容量
保持且无fallback。随后固定四组attention A/B共8次forward
全部首次通过，输出及完整KV/raw/comp/元数据逐字节相同，
有限值、只读区域、写入范围与全部缓冲guards通过，实际FP8关闭。

完整k3固定17步首次通过，自然count1/2/3/4=2/1/2/11，另
forced count1；独立argmax、checkpoint raw-reader、同形回放
合同通过。与旧last-row阶段123个原始文件（46,300,139B）
逐文件逐字节一致，55个token轨迹不变。完整recurrent状态仅
有各次运行内部回放对照，旧新raw文件没有完整recurrent/KV
快照，结论不扩大到所有状态跨版本或所有输入等价。

`group.json`绑定候选/测试身份和两项结果；已满足五档性能
前置，接下来唯一15条关闭诊断的HTTP，不追加诊断矩阵。

## 五档最终结果：decode 改善，整体性能 NO_GO

固定26条新HTTP全部完成，质量/数值前置通过，输出、usage及
实际MTP步骤全部相同，无fallback。五档均值如下；decode为
三条合计765除以decode秒数之和，非每条速率的算术平均。

| 输入 | TTFT秒 | 整请求秒 | decode tok/s | decode变化 | 整请求变化 |
|---|---:|---:|---:|---:|---:|
| 1024 | 0.981269 | 9.771767 | 29.008593 | +2.074% | -1.783% |
| 4096 | 2.882182 | 11.563375 | 29.373843 | +1.786% | -1.302% |
| 8192 | 5.509633 | 15.284211 | 26.088081 | +2.048% | -1.247% |
| 45056 | 31.887226 | 43.688708 | 21.607456 | +2.038% | -0.438% |
| 204800 | 166.892802 | 176.321009 | 27.046500 | +1.692% | -0.028% |

按冻结后两次范围，五档decode_seconds和latency均通过；TTFT
在1K/8K/44K/200K未通过，4K通过。因此出口为
**NO_GO_FOR_CANDIDATE_PERFORMANCE_ACCEPTANCE**。1K超上限
4.631/3.113毫秒，8K 8.665/1.881毫秒，44K 41.719/56.869毫秒；
200K后两次为−404.239/+48.698毫秒。保留全部三次，不追加
有利采样，不把微小差异解释为已证实的因果回退。

最终证据`.q4t-work/mtp-known-position-20261008/`中的
`candidate-summary-final-01.json`，`source-snapshot.json`保存
133项运行来源与有关测试/解析器的旧字节，便于后续改动独立核对。
默认关闭、跨模式数值NO_GO不变。第二候选为非末初始化块
省略未使用尾部，见[MTP_INIT_TAIL_2026-10-08.md](MTP_INIT_TAIL_2026-10-08.md)。
组合候选仍对原21d85a17验收，不能以未接受的本候选替换唯一基线。
