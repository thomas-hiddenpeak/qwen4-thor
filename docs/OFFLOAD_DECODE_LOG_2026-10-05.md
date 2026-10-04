# Decode 分区日志单因素候选 — 冻结计划

更新：2026-10-05。实现已成组完成，静态复核收尾；尚未构建或执行测试。
入口为 ed75d60；上一请求长度策略的完整性能 NO_GO 保留不变。
本阶段只实施一个默认关闭、可回退的日志候选，不改 GEMM、精度、
权重格式、缓存容量、驱逐或请求长度选择；MTP 和运行时 Phase D 关闭。

## 问题与候选

上一轮长请求 TTFT 有收益，短档 decode 未过门槛。完整矩阵先执行
1K/4K/8K，之后才有长请求，所以长→短缓存继承不是全部短档回退的
必要条件；混合历史中的继承确实存在，其耗时贡献仍未知。

全局分区 on 的真正 decode 每层多一条 partition fprintf；其他 flush、
diag 记录与相关集合构造两臂都有。本轮只关闭这一条 partition 日志，
不从既有速度中扣除估算时间，也不承诺它足以消除全部回退。

Q4T_MOE_DECODE_PARTITION_LOG_QUIET 严格为 0/1，默认 0。
只有显式 single-decode、T=1 且无 prefill context 时，1 才抑制该行。
独立日志阶段沿模型层传播；scheduler 单序列 decode 和 fallback step
显式标记，未知直接 API、批量 B>1、prefill/continuation 保留原日志。
长输入的 singleton prefill 尾块不能因 T=1 被误判为 decode。
一次配置记录及所有其他错误/请求/prefill/diag 证据保留。

## 冻结执行范围

| Arm | 全局/请求分区 | quiet | 用途 |
|---|---|---|---|
| A | 0/0 | 0 | 同新 binary 的 legacy 基线 |
| B | 1/1 | 0 | 原日志策略，仅作日志干预对照 |
| C | 1/1 | 1 | 本轮唯一候选 |

必要构建之后第一项测试为 C 的固定 11 题真实 HTTP。通过后执行 157 项受影响
host/工具合同（含新增日志边界）和三项真实权重数值合同，再执行以下冻结组：

1. History A→B→C，每组 [16385,8192,8193,1024,45056,4096,8192]×3，
   共 63 请求；每组同服务保留请求间缓存，两个 8192 位置分开。
2. Matrix A→C，每组 [1024,4096,8192,45056,204800,261887]×3，
   共 36 请求。常规输出 256，目标档 257，总容量 262144。
3. 对所有实际执行组做资源与清理核对，阶段归档、独立复核及提交推送。

质量组不限制 host/cache；五个性能组均为 16GiB、swap0，C256/L2=16/
mirror8、max_open200、单流、max_prefill8192、max_len262144。
每个性能组启动前最多两轮定向 cache advice，完整 payload resident=0
才启动；组内不清缓存。原输入、参考输出与失败证据只读。

B/C 只改变 quiet，用于描述日志策略干预后的总变化；固定组序仍有
时间漂移和调度/缓存时序混杂，不把差值称为纯 fprintf CPU 时间。
A/C 承担正式性能门槛：每位置/每档首轮 TTFT 不增，后两轮最大 TTFT
不增，三轮最低 decode 不降。所有位置与档位都须通过。

有效 history 速度 NO_GO 仍继续本来就冻结的六档覆盖，但永久否决
整体 GO；输出、容量、身份、冷态或清理缺陷则停止依赖工作并保留失败。
仅具体缺陷可定点修复，不追加有利样本、不事后改门槛、不启动第二候选。
若正式性能全部通过，再冻结业务/生命周期检查后才讨论运行时接受；
本阶段不默认启用、不合并或部署。

## 证据与限制

quiet 模式不能把 decode partition 缺行当成零执行或自动通过。
按真实 HTTP 请求、所有 prefill chunk 和实际 output−1，核对保留的
每层 diag 记录与完整层序；prefill candidate/singleton partition 行仍须
严格匹配。旧模式解析保持严格，新增边界/缺失/错序/泄漏合同一起检查。

TTFT 是客户端首 token 等待，不是纯 prefill 时间；decode 单独报告。
PID、cgroup、设备及软件计数分列，内存重叠/峰值不相加。
54GB 整体物理 RAM 继续 INDETERMINATE；16GiB 只是 host/cache 条件。
模型保护只做元数据快照，reference 全树未变缺少入口证据，范围明示。
原工作区七处改动与主 binary 单独保护，不纳入本分支。

冻结计划位于本机 .q4t-work/offload-decode-log-20261005/plan.json，
SHA256：ecfcd41ef3e6dd5ff9387f34a06c43e1c1f524cf01e3cc2f120ad8a519f0fd01。
构建后另冻结被测源码、binary、工具和完整命令身份；在模型执行期间
不改冻结来源。当前无新质量、数值、性能或内存验收结论。
