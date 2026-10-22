# 独立BF16草稿head：单候选冻结

2026-10-23，性能优化任务。**本轮NO_GO并停止，默认5a不变。**
受测8b7621d8首个HTTP质量门失败；修正版75e85bac只完成零警告构建，
未重测，不接受、不合入main、不部署。以下冻结卡保留开测前范围。

## 身份与范围

源码父ab762aa（同会话准备期间main的DeepInfer文档提交）；实际默认
build/q4t SHA256为5a456351ac7230607e69b6d03e24aace3efb4c7ba521bf0a348a9b92d6c21191，
release记录编译源码3f2c704。运行时代码差异须在构建前核验。
隔离工作区.q4t-work/mtp-subset-head-20261023/source，工作分支
codex/mtp-subset-head；隔离原工作区的并行文档工作。

参考为归档9a5bc0c中的REFERENCE_REVIEW及本地只读NInfer
.q4t-work/reference-review-20261016/ninfer@81c8ce093b2c1646a87566a8e59d807fcf0ec95c。
proposal.py选频率行、load/text.cpp检查shape/唯一合法ID、
execution/text.cpp的proposal_argmax在argmax后remap。本地不采用量化。

## 唯一候选

固定65536行BF16，源为本项目原始lm_head权重，完整target head仍驻留。
校准采用已有moe-evalscope-scenarios-20260928的24条输入及历史响应文本，
只用于token频次，不将旧响应当正确性oracle；不新采模型响应。
强制包含tokenizer全部special/added special tokens及generation EOS。
其余按频次降序、原ID升序补齐，未出现token同样以原ID补齐。
最终行按原ID升序排列，保持argmax同分选择最小原ID的规则。
固定规模不扫描；覆盖率只是校准描述，不是接受率预测。
验收使用独立的现行quality/五档performance输入，不纳入校准。
派生产物仅在.q4t-work/，记录所有输入、权重、tokenizer、映射摘要。

## 数值与资源合同

BF16源位模式逐行复制；投影仍用现有Bf16Gemm，列维改为65536，
可能改变分派与舍入，不承诺与完整head逐位相同或最终文本无损。
直接投影对同形状独立逐行参考及BF16误差分析；target verify不变，
原N=4近tie及状态恢复合同不放宽。五处草稿argmax均需映射；两处
verify argmax继续完整词表。固定k3、停止、接受、回滚策略不变。
为减少本轮改变面，既有共享logits scratch容量保留，草稿行stride
使用子集大小；新增权重335544320 bytes和映射262144 bytes均计费，
加载期间host副本、文件页缓存和其他统一RAM占用另行记录。
非法显式配置失败，不回退为普通decode；未启用沿用原head。

## 验收顺序与停止

必要预处理及零警告构建后，第一项测试必须为真实tools/evalscope HTTP
E2E。开测前另冻结实际binary、输入哈希、请求顺序、完整驱动、预算、
收益目标和各保护项。当前不授权未冻结的GPU测试；不跑bench。
完整阶段同一gpu_exclusive lease。质量/数值合同失败、收益不足或
证据不足均保留默认及候选证据结束；不扫描规模、不自动追加采样。

## 开测冻结卡

运行时与诊断最终构建build-final-v3.log零警告；实际SHA、构建缓存、
输入、工具与全部逐项命令冻结于阶段frozen-plan.json，开跑前逐一校验。
父源码3f2c704到ab762aa的src/include/CMakeLists无差异，默认5a为精确父。
先候选MTP质量11、普通质量11，再父off/候选off/候选on/父on五档各3条，
总82 HTTP、6服务启动，无warmup、重试、丢弃或追加采样。启动限900秒，
每HTTP组限3600秒；每档1024/4096/8192/45056/204800输入，输出256，
max_seq1/max_prefill8192/max_len208896。留出沿用既有冻结quality/performance
请求，原样复制并哈希；校准无任何这些请求或输出。
HTTP全部通过后才执行同一lease内投影(T1/T4/T17)、MTP生命周期
(4次真实MTP加载尝试含失败注入)、全模型17步状态重放(1次加载)。
任何失败即时停止后续，未运行项明确NOT_RUN。

新增复杂度的投入目标：MTP五档最终decode中位数各至少提升5%；这只作
本项收益筛选，不是统计显著性或容许退化阈值。保护项为普通decode、
两模式逐档TTFT、质量、确定性、数值/恢复合同、容量与统一RAM。
当前没有跨启动不确定性或实际损失界限的可信估计；不主张统计非劣，
保护证据不足则保留基线结束，不套用旧简化型例外、不追加采样。
父5a不输出接受直方图，该项明确不可用；候选逐请求记录0..3草稿接受数，
不能由SSE chunk推算父接受率。内存记录系统meminfo、进程RSS、NVML及
预算/加载日志；RSS与device不得相加当作独立物理RAM。校验文件缓存
保守预留4GiB，device新增335806464 bytes，host副本计入加载峰值；
缓存与分配实测另列，非完整驱动私有内存计量承诺。


## 实际结果与停止出口

[机器可读结果](evidence/mtp-subset-head-20261023/result.json)。实际1次主模型/MTP
启动、11条HTTP；evalscope首条success=1仅表示收到流，内容只有部分回答，
不满足质量/输出合同，其余10条success=0。质量0/11，性能0条。
请求组一次发送11条，门禁在组结束报告失败；无重试或额外探针。
普通模式质量、五档父候选、投影、加载失败注入、重复调用、释放、
状态恢复/终止等直接合同均NOT_RUN，不能宣称这些合同通过。

72次NVML查询全OK、无检测到竞争；guard为COMMAND_FAILED，非ENV_ABORT。
服务exit0仅代表进程退出：CUDA illegal memory access(700)导致两类
metadata drain失败，reservation最终FAILED、release_unknown=1608。
不把这一退出当作所有权或释放验证通过。原始日志和现场完整保留，
进程已退出，没有遗留本阶段服务；未更改全局GPU/系统配置。

失败时启动总67.620秒，主模型29.776秒，MTP加载34.709秒（包含源文件
摘要校验）；计划device97,023,144,328 bytes。新增head/map335,806,464
bytes，校验读缓存预留4GiB。系统采样MemAvailable最低22,370,496,512
bytes，Cached峰值23,961,481,216 bytes，单进程RSS峰值4,152,672,256
bytes。这些不是候选增量或稳定推理峰值，CPU/GPU/RSS/缓存不能相加。
没有有效TTFT/decode、草稿接受分布或两模式完整质量结论。

静态源码定位两项确定的接入缺陷：脚本替换误把EmbedLookup的vocab
改成子集65536，同时真正草稿投影仍以248320行读取65536行分配。
前者破坏原token域（特殊token也可能越域），后者直接违反内存形状合同；
首次MTP init的D2H argmax报错与该越界一致，但没有运行sanitizer定位
实际触发的首个GPU指令。不能把这次失败解释为BF16舍入或模型质量问题。

修正版恢复完整embedding域，仅草稿投影使用DraftVocab；零警告构建
q4t及三项诊断目标。**没有新增GPU/HTTP验证，未证明运行时修复通过。**
这是同一65536行配置的源码修正，不生成第二词表或新校准样本。
依用户“合同失败结束本轮”出口停下，不把未使用预算当作自动重试授权。
本轮交付转换器、运行时研究实现、固定产物与可复核失败，非优化成功。

## 保留、复核与回退

根目录绝对路径为/home/rm01/models/dev/qwen4-thor，以下相对此根：

- 默认build/q4t及release sidecar未替换；SHA仍5a456351…。
- `.q4t-work/mtp-subset-head-20261023/parent/`保留默认binary、真实构建缓存
  及原release元数据；既有正式回退包不动。本轮没有需要执行的部署回退。
- `failed-candidate/`保留受测8b7621d8及全部诊断binary、构建缓存；
  `source-snapshot/`、`source-manifest.json`保留编译源码。
- `build/`保留修正版75e85bac，`artifact/`保留唯一词表产物。
- `source/.q4t-work/acceptance-v1/`保留请求、响应DB、服务/客户端日志及
  系统内存采样；`lease/`保留完整跨会话锁与72次监控记录。
- 本文同目录的`evidence/mtp-subset-head-20261023/`保存摘要、冻结计划、
  失败日志与构建日志。恢复受测源码：从ab762aa解压并应用failed-candidate.patch.gz，
  再解压覆盖failed-new-files.tar.gz，按failed-source-manifest.json核对编译输入。
  不运行恢复结果，不覆盖原模型或reference目录。

分支codex/mtp-subset-head保留独有实现和失败证据，不因NO_GO而删除；
不创建合入PR、不合并main。后续若另立阶段，必须重新冻结验证身份及
范围；本轮不自动扩样、扫描规模或启动量化/suffix等后续研究。
