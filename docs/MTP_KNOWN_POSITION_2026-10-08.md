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
