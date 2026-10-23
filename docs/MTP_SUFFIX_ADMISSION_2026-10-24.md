# 固定k3 suffix：长匹配准入有界改进

2026-10-24，性能优化。用户要求继续研究45K回退及旧8K多一次尾部
forward，在保住完整head和65536行BF16子集head重复收益的前提下，
实现并验证一个有界准入改进。此前两个NO_GO及封存材料不改写。
本文件是新候选开测前的判定卡，结果在阶段结束后另行追加。

## 基线与已知原因

- 工作分支`codex/suffix-k3`，父提交`9ea4267`。数字准入受测源码
  `82ce03a7d9766353d5b3c7eeabf3cefef941d4ba`，程序SHA256
  `94d004be82d45d0d15110419eac060e441990bc109cad797e1f50f8386b7338a`。
- 当前交付程序仍为`build/q4t`，SHA256
  `f356b2c96330101abf1e5fa3fced495a37f4ea5a270ed4457037e0d55735d313`；
  发布元数据SHA256
  `422993b0aba5d21462ac3a7e44a78cbb8fb1721b41ca961bd476c679581e7e96`。
  实际构建源码9a902a8，已被main包含。
- 子集继续显式选用
  `.q4t-work/sha2-startup-20261023/delivery-package/artifact`；manifest
  SHA256 `b63b0f251fddc8a75749a54dd4b22477fdb8da875cc26f83595534c71062c3ce`。
  完整head默认，模型及reference只读。
- 离线诊断目录`.q4t-work/suffix-admission-20261024/diagnosis-index`
  和`diagnosis-runtime`。只读取已封存数字阶段记录，新增GPU/HTTP为0。
  原始输入/输出、日志与SSE逐文件核对旧seal；生产tokenizer扫描完整
  `[0,248320)`域，248077可解码、243 padding失败。116请求对应8组
  唯一prompt/output序列，SSE片段均能唯一反解token。

旧8K的四条原生MTP timing轨迹表明，两head前25轮的起点与接受数相同。
第26轮（零基step25，输出位置78，P8237）以19-token句段匹配历史续文
`memory traffic.`，但当前引用句的续文是`memory traffic."`，suffix
接受2，OFF神经草稿接受3。随后各43轮均接受3，ON一直少推进一个token，
末MTP提交254→253，普通尾部forward1→2。更早位置27的7-token匹配
虽然只接受2，OFF也接受2；它本身没有增加目标工作。

数字阶段45K两head的首次步序分歧均在输出位置92/P45127：8-token
匹配`transcript identifies uncertain words without silently replacing them`，
历史续文`.\nEntry`，当前列举续文`.\n2`；ON接受2，OFF接受3。四个
候选的匹配长度及错误续文相同，因此只要求候选续文一致仍会放行。
这是SSE步边界加日志计数闭合的还原，不冒充原生45K逐步trace。
45K完整head目标forward66→71、处理行264→275，子集69→73、276→283；
两head各多8次restore。上轮没有45K分阶段计时，不能给这些调用虚构
独立耗时。完整head和子集省去相同草稿次数时，子集省时更少。

部分接受不必然亏损：有与OFF接受数相同的匹配，新1K还有同位置
suffix接受2而OFF接受1的例子。共同位置的两条已观测路径也不能替代
“相同内部状态下改用神经草稿”的反事实。最终按整请求净吞吐判定。

## 唯一实现候选

固定要求选中的**完整匹配长度至少32个token（含pending）**。仍先以
三元组查询，精确比较最多4个候选、最多64token，最长优先、新者平局；
依次经过完整三个候选、首个等于已有d0、现有数字格式过滤，然后检查32。
失败即回到神经MTP，不改选其他候选，不添加命令行门槛或动态策略。

32是本轮保守的长拷贝边界，不声称最优阈值或普适成本模型。选择依据：
四个开发探针（旧8K、数字阶段1K/8K/45K）在已记录输出所有位置的最大
匹配分别21/13/13/12，旧实际命中都将被拦截；重复场景则大量达到64。
只分类这一个32候选，不扫描阈值。按旧轨迹，新重复代码/文本保留55/62
命中，更早代码55/62、文本54/61；子集新代码保留的一次接受2也仍存在。
这些是静态保留数，不是改变步序后的命中、性能或状态证明。

58条ON请求的命中及suffix接受直方图全部闭合；其中52条所有查询计数
闭合。其余6条子集重复请求有合计14个matched a0，SSE不能提供它们的
d0，明确保留UNKNOWN，不靠聚合统计填造。四个退化探针没有该缺口。

范围仍为单流、greedy、MTP k=3及显式`--mtp-suffix`。不支持配置仍按
原启动校验失败。suffix为全局token ID，三候选不要求属于子集。
目标四行verify、checkpoint恢复、接受前缀draft extend、next_d0/next_g、
普通decode长度/上下文回退及取消/槽位生命周期均不改。

新增`short_rejected`计数和`min_admitted_match=32`启动身份标记；
查询范围仍3..64，numeric_checks=hits+numeric_rejected+short_rejected。
没有新增分配、tokenizer调用、索引容量或候选搜索。索引上限2MiB，
max_len208896时payload1884160字节；共享分类表248320字节，上限256KiB。
对象新增一个64位计数；实际sizeof、构造/增长/查询/释放和统一RAM实测。
numeric_ns包含在query_ns内，不能重复相加。

## 开测前冻结的收益目标与保护项

1. 两head、两个配对块分别消除开发旧8K及旧45K相对同配置suffix OFF
   增加的目标工作：total target forward calls和processed rows均不增加。
   verify、普通尾部T1、restore、神经proposal和extend分别报告。
2. 两head的每个新/开发重复代码与文本场景，两个ON/OFF decode配对比
   的几何均值至少1.05，且每块均大于1。完整head收益不替代子集收益。
3. 两head每档decode/TTFT、新非重复/数字枚举、旧8K/45K吞吐、同期普通
   decode五档、质量、输出身份、生命周期及资源分别保护；可信的回退
   不能由其他场景收益抵消。所有原值、首请求、启动顺序必须保留。
4. 不沿用上一候选人为要求旧8K每个观测比都非负的门。本轮按性能协议
   分别记录微小观测差、是否有重复的工作量/耗时退化证据、统计能力与
   工程决定；不设统一百分比容差，也不把工作量相等当作吞吐相等。
   该新判定卡不改变已封存数字候选NO_GO。
5. 两启动配对块只能给出有界描述性证据，不能证明总体统计非劣、所有
   输入输出完全相同或未来每次都不变慢。证据不足在预算终点明确报告，
   不自动补样、改门或换统计方法。

## 数据、顺序、预算与停止

新验收材料在输出观察前由`tools/mtp_suffix_admission/prepare.py`生成，
seed202610240731；五档为新的公共学习活动记录，完整记录截断至
名义1K/4K/8K/45K/200K的[n−48,n]范围。新重复代码为快递装箱，新文本
为气象课堂，新非重复为社区观星活动；新数字枚举使用独立seed+1。
具体prompt/token/文件SHA及程序源码快照由frozen-plan记录。

已观察材料全部标记development_regression：原8159-token旧8K、数字
阶段45035-token旧45K、数字阶段520/423-token重复代码/文本。更早重复
及数字阶段1K/8176-token 8K只作离线回归重放，不混称未见验收。

统一单流greedy、MTP k3，max_prefill8192、max_len208896、输出256。
自然继承页缓存，startup auto；不drop_caches、手工预读、额外warmup，
不声称SSD冷态。每个服务新启动，全部请求串行。

| 顺序 | 内容 | HTTP | 启动 |
|---|---|---:|---:|
| 1 | full ON现有11条真实evalscope HTTP质量，第一项测试 | 11 | 1 |
| 2 | 新host/阶段审计/旧轨迹准入合同，然后subset ON质量11 | 11 | 1 |
| 3 | B1 full OFF→ON、subset ON→OFF；B2 full ON→OFF、subset OFF→ON | 184 | 8 |
| 4 | 当前交付普通decode→候选普通decode，均新五档每档3条 | 30 | 2 |
| 5 | 两head分别control/实际cancel/新请求recovery | 6 | 2 |
| 6 | full OFF→ON、subset ON→OFF；每服务旧8K及旧45K各一条timing | 8 | 4 |
| 合计 | 每个主服务23条；timing不进入吞吐验收样本 | **250** | **18** |

主服务顺序固定：新五档各3条（15），新code/text/nonrepeat/numeric各1
（4），开发old8K/old45K/code/text各1（4）。取消使用已知重复代码材料，
另记取消控制和health调用，不将控制调用计为生成请求。为覆盖长匹配
实际使用后的取消，新wrapper收到至少64个非空SSE片段才发cancel，
并要求取消请求已有suffix命中；每个结束记录released=1，共享表释放
必须在所有请求结束之后。请求数及硬超时保持不变。

墙钟上限14400秒，直接合同共享1200秒；必要构建不算测试。首HTTP
通过后才运行新host、阶段合同及新门的CPU回归重放。GPU独立接受前缀
oracle、子集投影/映射、普通质量与未改runner合同仅在源码/编译选项/
产物依赖审计一致后按原覆盖范围复用；两层合同不变成任意整模型证明。

一个完整驱动通过`tools/evalscope/gpu_exclusive.py`持有跨worktree共享
独占锁，覆盖所有服务间隙。NVML与统一RAM持续监控；竞争、监控失败或
RAM采样过期记ENV_ABORT，只停止自己的进程并保留现场。轮询不能排除
采样间隙干扰。首HTTP/质量/合同/身份/超时失败即停止，不在本轮修复重试；
中途性能不改变矩阵。预算用尽即分别记录证据有效性、测量完整性、统计
结论与工程决定。

若通过，按既有授权先形成可核验回退包，再替换适用程序与发布元数据，
另有最多3启动/3生成健康检查预算（总上限253请求/21启动），更新文档并
提交推送。未通过则保留当前f356交付，封存候选和失败证据，阶段提交
推送仅保留研究边界，不宣称交付通过。无参数重扫或自动追加GPU请求。

## 构建与开测准备

候选程序SHA256：
`6a4d3d62c09d814a9342cc698e1523e1029d1d3aa3e36e1b80cf7a625a30b090`。
独立目录`.q4t-work/suffix-admission-20261024/build`，Release、g++14、
CUDA13.3、SM110a；主程序、host合同和CPU replay均零警告构建，未提前
运行新候选测试。`build-identity.json`记录实际源码hash/构建命令/日志及
构建时父提交，完整源码快照随frozen-plan冻结。

独立静态审计在243源码/测试/CMake文件中只发现预期5个运行时/host
文件变化；79原GPU依赖、8项复用证据及9组实际编译选项保持。新准入、
新阶段审计、延迟取消、当前质量和性能不在复用范围。
[离线诊断摘要](evidence/mtp-suffix-admission-20261024/index-diagnosis.md)
及其provenance保留原始seal、脚本和完整重放身份。
