# 零命中路径：独立运行时静态与既有日志诊断

2026-10-24。只读源码、协议和上一阶段封存记录；新增 GPU、HTTP、测试均为 0。
本文件及 observations JSON 是新的离线衍生材料，不修改上一阶段证据或
`NO_GO_PROTECTION_NOT_CLEARED` 决定，也不构成新候选的接受结论。

## 结论先行

子集新45K/200K的主要 TTFT 差值已经存在于**本请求目标 prefill 完成时**，
当时尚未执行 MTP prompt draft-extend、suffix.Init、首次 suffix 查询和
首次 MTP step。将当前请求不足0.9ms的索引初始化直接解释成数十至数百ms
TTFT损失，与执行顺序及原日志不符。跨请求分配/缓存残留、服务启动差异
和计算环境仍可能影响后续 prefill；现有记录不能排除，也不能证明其中任一项。

新8K没有增加目标调用/行数。子集第二块的23.366–29.509ms首输出后耗时
增加，大部分同时出现在 scheduler completion 等待总量内；query+append
仅0.070–0.087ms。这个等待总量含实际模型工作和调度/唤醒，不是纯GPU时间。
应先区分它们，不能把差值归因于几十微秒的索引计算。

普通decode的同期负向结果发生在无分类表、无suffix Init/query/append的
路径，不能作为“suffix lookup拖慢GPU”的证据。普通模式只有一个父→候选
启动对，既不能确认新二进制造成损失，也不能拿它推翻MTP两个配对块的
保护疑点。

## 身份与可复核来源

- 仓库：`/home/rm01/models/dev/qwen4-thor`；已读 AGENTS、STATUS、EVALUATION
  和 PERFORMANCE_DECISION_PROTOCOL。性能优化仍要求HTTP首门、适用五档、
  目标/保护分别审查、完整阶段独占和事前预算；此处没有执行新测量。
- 上轮运行时源码：`cb0fa73c3271e6fbce2cd9d282838de9643d2a3b`；候选
  `6a4d3d62c09d814a9342cc698e1523e1029d1d3aa3e36e1b80cf7a625a30b090`。
- 默认父程序仍为 `f356b2c9...`；上一阶段末未部署候选。
- 原始目录：`.q4t-work/suffix-admission-20261024/acceptance-v2/run/`。
  seal SHA256：`6cea30d12983739f12dbcae87dbe04e8bb3d6617e65e1c3c45453678ce7ef3a1`。
- 本轮衍生原值：同目录新阶段的 `diagnosis-runtime-observations.json`，SHA256
  `c5130c270ab5545583519920f12ab84b14f0e36e20bb121ed990c14408d9c376`。
  包含120条MTP五档请求、60个同序号ON/OFF配对；不包含普通decode。
  以HTTP `response_id`精确绑定服务日志后，才按同case ordinal配对；保留
  输入/输出数量、阶段原值、差值与被读取文件SHA。没有使用位置猜测代替ID。
- 此诊断复用上轮原始seal已通过的完整性审计，没有重新哈希模型权重或重做
  全量seal审计。新JSON的源hash用于定位读取材料，不替代原审计。

## 实际执行顺序和时间口径

`src/server/chat_generation.cpp`当前关键位置：

1. 请求注册/slot admission、分词、主机logits和MTP trunk分配。
2. `diagnostic.Stage("prefill")`（426行）后，目标模型分块prefill；长输入的
   chunk边界还包含已有stream completion、状态提交与memory snapshot日志。
3. `diagnostic.Stage("decode")`（566行）：目标最后行D2H已完成。该事件本身
   不表示首内容已发出；它之前没有本请求suffix索引操作。
4. 发送HTTP/SSE头和空assistant-role chunk；主机argmax；`ttft_seconds`
   指标（604行）在这里记账。该服务指标并非客户端实际首内容TTFT。
5. MTP rolling trunk `cudaMalloc`、Reset、全prompt `MtpDraftExtend`（656行）、
   scratch/checkpoint准备、prompt trunk `cudaFree`（677行）。两臂共用。
6. scheduler注册，suffix ON执行Init（708行），复制合法已提交历史、分配
   1MiB buckets及capacity×4字节history、遍历prompt建立三元组索引。
7. 查询（740行）在request handler线程、通知scheduler之前执行；未命中时
   `mtp_suffix_ready=false`，scheduler传空span，Multi执行原神经proposal、
   四行verify、恢复及accepted-prefix extend。没有额外CUDA复制或后备GPU分支。
8. scheduler返回后，逐token分词/写SSE，提交历史并Append（803行），再进行
   下一轮。Append不含拒绝尾部。
9. MTP循环结束后Reset与suffix总结日志（840行起），之后才可能普通尾部decode；
   这些日志在客户端latency窗口内，但不在query/init累计值内。

`chat_server.cpp:285–329`的248320字节分类表在Start期间、ready之前构建，
每ON服务一次约19.35ms，不是每请求成本。OFF不遍历词表、不分配该表。
普通decode同样不执行suffix初始化/查询；栈上空SuffixDrafter和分支不等于
分配一份1.8MiB索引。完整head和子集head都使用相同全词表分类表。

客户端 `tools/evalscope/.venv/.../plugin/api/default_api.py:180–221`忽略无
输出role chunk，首个`has_output`时记first_chunk_latency，最后JSON消息
记latency；`[DONE]`本身不更新时间。故MTP客户端TTFT包含上述MTP初始化、
第一轮推测以及网络/解析；服务ttft指标和stage=decode不能直接冒充它。

`RequestDiagnostics::Lock`只计等待获取mutex的时间，不计持锁执行。
`scheduler_completion_ms`是条件变量等待区间；MTP包括第一轮和普通尾部
等待，普通decode还包括调度prefill。它不能直接称为post-TTFT decode时间
或GPU busy。原8条MTP timing只覆盖旧8K/45K，无法定位新200K prefill或
新8K的本次scheduler等待增加。

## 长上下文TTFT：已有分界排除当前索引直接大开销

下面是同序号ON−OFF，单位ms。`目标完成前差`来自每条handler的
stage=decode elapsed_ms；`后段残差`定义为客户端TTFT减该handler elapsed，
两臂相减。两者算术相加等于同序号TTFT差，但残差还含client/handler起点
差，不是单独的MTP初始化时间。不能将三个同序号差的中位数替代原报告
“各臂中位数之差”。

| case | block | ordinal | TTFT差 | 目标完成前差 | 后段残差 |
|---|---:|---:|---:|---:|---:|
| subset45K | 1 | 0 | 87.971 | 84.666 | 3.305 |
| subset45K | 1 | 1 | 25.801 | 23.584 | 2.217 |
| subset45K | 1 | 2 | 59.755 | 55.657 | 4.098 |
| subset45K | 2 | 0 | 64.446 | 64.407 | 0.039 |
| subset45K | 2 | 1 | 24.003 | 26.697 | −2.694 |
| subset45K | 2 | 2 | 64.451 | 63.889 | 0.562 |
| subset200K | 1 | 0 | 215.678 | 202.069 | 13.609 |
| subset200K | 1 | 1 | 244.996 | 224.559 | 20.437 |
| subset200K | 1 | 2 | 400.980 | 372.840 | 28.140 |
| subset200K | 2 | 0 | 220.960 | 229.015 | −8.055 |
| subset200K | 2 | 1 | 206.013 | 203.309 | 2.704 |
| subset200K | 2 | 2 | 123.109 | 121.831 | 1.278 |

各块三请求差值合计中，目标完成前差占45K的94.46%/101.37%，200K的
92.78%/100.74%；超过100%表示其余阶段部分抵消。这个描述性分解不是
因果归因比例或置信区间。45K的prefill entry前差只有−1.976～−0.047ms；
200K只有−2.754～+2.393ms，主要差值在目标prefill区间，而非当前分词/
trunk分配或进入请求的等待中。子集目标prefill代码本身不读取suffix开关。

当前请求索引Init：45K 0.292–0.634ms、200K 0.785–0.878ms。数值和顺序
同时反驳“当次Init直接耗时造成主要TTFT损失”。启动分类表本身也不能
直接累加到每条TTFT。间接改变先前请求/启动后的allocator、cache、CPU
状态等仍属未证假设；不能因代码分支在后面就推导全服务因果独立。

## 新8K、scheduler及普通模式

新8K两head各两块目标调用/行数与OFF相同，所有请求0次suffix命中。
第一块部分同序号post-first-token差为负；不能从中位数负向推导每条都慢。
最一致的是subset第二块：

| ordinal | post-first-output增加ms | scheduler completion增加ms | ON query+append ms |
|---:|---:|---:|---:|
| 0 | 28.984 | 31.621 | 0.087 |
| 1 | 23.366 | 22.673 | 0.084 |
| 2 | 29.509 | 29.657 | 0.070 |

这把大部分差值定位到等待模型step完成/handler被唤醒的区间，而非其前后的
索引CPU累计区间。两列窗口不相同（scheduler总量还含首轮），不能精确相减
得到“CPU调度成本”。当前日志没有每轮submit、scheduler开始、模型返回、
handler醒来的四个边界；也没有该新8K样本的GPU阶段区间。

同一候选二进制的ON/OFF排除了候选构建差异这一因素，但不排除启动与
时间漂移。普通父→候选对照二进制不同且仅一个启动对，五档decode中位
变化为−0.326/−0.059/−0.598/−0.072/−0.459%。suffix未执行，所以这些
结果支持“不能把所有小差归到suffix查询”的限制，不支持新的归因结论。
现有NVML记录主要证明进程归属/内存及查询可用，没有完整CPU/GPU频率、
热状态、runqueue与每chunk GPU busy证据。

## 可证伪项与最小机制建议

| 假设 | 现有判断 | 新观测能如何反驳/定位 |
|---|---|---|
| 本次索引Init直接导致长TTFT主要损失 | 被时序与数量级反驳 | 无需重测来证明这个已知顺序 |
| 新8K增加verify/restore工作 | 上轮计数不支持 | 保留同接受分布/目标调用审计即可 |
| query/append纯CPU时间解释数十ms | 数量级不支持 | 将累计CPU时间与同窗口等待/计算分开 |
| suffix日志I/O影响末尾 | 未独立计时，可能有小成本 | 一次end日志累计格式化/写出耗时；不能解释已发生的prefill差 |
| GPU执行变慢还是scheduler唤醒慢 | 未分开 | 仅在诊断请求中记录submit/start/model-done/handler-resume累计值 |
| 上一请求索引分配影响下一次prefill | 未排除但没有证据 | 保留启动内序号及allocator/缺页计数；不以跨阶段差值直接认定 |
| 频率/热/负载/缓存或测量顺序 | 未分开 | 现有独占不等于固定硬件频率；加入固定低频环境记录并披露开销 |

如果现在必须给出一个不改变准入语义的最小实现候选，建议仅在数字分类
扫描遇到第一个`kSuffixAllowedAscii`为false的token时停止继续读取分类表：
整段不可能再满足“全部allowed且含digit”，仍随后执行同一min32门。
最长匹配、最近四候选、d0门、reason优先级、proposal、提交索引与所有GPU
状态操作均不改变；unknown类别0也走相同放行逻辑。`classified_tokens`
必须改为真实检查数量，并调整只依赖旧全段计数的审计，不能假报扫描了
整个匹配。numeric_checks和numeric_rejected/hits/short_rejected关系保持。

这只是可核验的冗余工作删除，不是上述TTFT问题的已定位修复：新8K每请求
只做一次6-token numeric检查，约0.3–0.6µs；即使全部消除也无法回收
23–30ms。因此不建议为取得名义“优化实现”便据此承诺主目标改善，或
单独展开大规模GPU验收。较长hash改变最近四候选集合、跳过索引维护破坏
未来命中、重建可复用池增加跨请求状态合同；目前没有证据支持这些更大
改动。若根决定把机制选择留到粗分相观测后，本诊断支持该顺序。

## 最小有界新增观测建议（未执行、由阶段统一冻结）

先完成新阶段约定的HTTP首门。随后可将最多6条诊断HTTP编入同一总预算：
子集OFF、ON各一次新服务，每次固定新8K、新45K、新200K各一请求，输出
256，合计2启动/6请求、独立子预算至多900秒；不是六次同一case重采。
两head重复保护及完整适用五档仍由主验收矩阵完成，不能由这6条代替。
这个小诊断只有一次启动对，定位窗口使用，不证明总体效应。

保持原同步边界，不加cudaDeviceSynchronize。host粗计时暂存在请求对象，
一次终态日志写出：encode/alloc、每target prefill chunk的已有完成边界、
MTP prompt extend、trunk free、suffix Init、首次提交/首次返回、首次内容
写出；每轮只累计scheduler submit→start、model start→done、done→handler
resume，不逐tokenfprintf。诊断模式另用CUDA events围住prefill及第一步，
在已有完成点取值，事件区间仍不是纯busy。需要标明该模式会改变观测成本。
记录每请求输入/输出SHA、先前请求序号、目标工作计数、RSS/缺页与已有
监控状态；如果加频率/温度采样，应先冻结固定频率及失败出口，不调系统
频率或扩大环境权限。任何监控失败/竞争按ENV_ABORT，只有根完整driver
持锁；诊断结束不自动扩样、扫参数或改写前轮NO_GO。

本文件提出方案，不授权单独执行这些请求；当前完成项只有离线诊断。
