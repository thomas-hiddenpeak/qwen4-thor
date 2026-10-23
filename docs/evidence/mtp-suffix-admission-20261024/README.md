# 固定k3 suffix长匹配准入：阶段证据

2026-10-24。运行时源码`cb0fa73`，续验工具提交`e9e7623`；候选SHA256
`6a4d3d62c09d814a9342cc698e1523e1029d1d3aa3e36e1b80cf7a625a30b090`。
最终决定为`NO_GO_PROTECTION_NOT_CLEARED`：证据VALID、测量COMPLETE，
统计仅作描述，未证明总体非劣，也未宣称已证明总体因果退化。
证据有效、正确性通过或重复场景获益均不自动构成优化接受。

默认程序保持
`f356b2c96330101abf1e5fa3fced495a37f4ea5a270ed4457037e0d55735d313`，
发布元数据保持
`422993b0aba5d21462ac3a7e44a78cbb8fb1721b41ca961bd476c679581e7e96`。
未部署候选，未使用条件健康预算；已交付子集仍显式选择。
完整结论见[阶段报告](../../MTP_SUFFIX_ADMISSION_2026-10-24.md)，
v1工具失败与单次续验见
[续验记录](../../MTP_SUFFIX_ADMISSION_CONTINUATION_2026-10-24.md)。

## 收尾入库副本

收尾已将以下工作目录产物按原字节复制到本目录；表中SHA256绑定原始产物。
这些小型副本提供决定及核验入口，不替代大型原始记录。
为保持原字节身份，自动生成数据表末尾的一个空行也原样保留。

| 文件 | 用途 | SHA256 |
| --- | --- | --- |
| [decision.json](decision.json) | 最终四字段决定及保护项结论 | `85a675b61b60e65bb29ce20c237fdd1ddd0b734387d675c6575eb4594ede1b32` |
| [stage-results.json](stage-results.json) | 阶段结果、预算与身份摘要 | `15834771c33621acbe1852d11eb5c928a4c2a3870c2a1093aec883ecae9d7d4c` |
| [report-tables.md](report-tables.md) | 两head、两块及普通decode全部原值表 | `fe82f33c03b38cfa0d688d4be1780bb139082e31b1b97f820cd878fd6c5fc938` |
| [protection-review-root.json](protection-review-root.json) | 最终保护审查 | `4a256d0b7afac03aaca04b03890c7c614532d6c2c5d894110e9d8525970209c4` |
| [protection-review-runtime.json](protection-review-runtime.json) | 独立运行时保护审查 | `7e0098d13154031fecef60e7585aca180773b25c9d53fe5adbfa71f2ed3de9bc` |
| [protection-review-index.json](protection-review-index.json) | 独立索引/成本侧保护审查 | `a9647585a8979db62a20f51bc3156906d8f64166b753d228225d3ac79964bd14` |
| [audit-costs-findings.json](audit-costs-findings.json) | 目标、神经proposal、extend及资源工作表 | `9f6947eebf444681baaf6e5011178e0fc58ad6cace8edbc30a667b3ad44eb0a8` |
| [archive-no-go-receipt.json](archive-no-go-receipt.json) | 独立复制与封存核验回执 | `9e7ba2fb11dac7662b0a0369b28ecb4cca48e3c5fd2e34290941651b298b3abe` |
| [owned-process-final-check.json](owned-process-final-check.json) | 已记录本阶段进程身份的终态检查 | `ac6fca064b32ca4422011366b9b61894b7e3f5de8e36c752ad16f0c25fd64215` |

既有[index-diagnosis.md](index-diagnosis.md)、
[replay-provenance.json](replay-provenance.json)、
[replay-summary.json](replay-summary.json)、
[min32分类](min32-old-path-classification.json)、
[原生旧8K步序](native-old8k-step-alignment.json)、
[运行时因果说明](runtime-causality.txt)和
[依赖复用审计](reuse-dependency-audit.json)是开发诊断材料，不能冒充本轮
新验收数据或新轨迹预测。

## 大型原始证据与独立归档

原工作目录为
[`.q4t-work/suffix-admission-20261024`](../../../.q4t-work/suffix-admission-20261024/)，
独立副本为[archive-no-go](../../../.q4t-work/suffix-admission-20261024/archive-no-go/)。
两段raw HTTP/SSE、SQLite、服务日志、lease/NVML/RAM记录、候选程序、
源码快照、构建身份及分析/审计工具均保留原路径并进入归档；模型权重未复制。
归档3582个封存条目、341555928字节复制payload，
[files.json](../../../.q4t-work/suffix-admission-20261024/archive-no-go/files.json)
SHA256为`7cef262f473d1b991efdec7cdb794683855281f77a2c133e4adfa1c4ac15f461`。

| 大型产物 | SHA256 |
| --- | --- |
| [v1 raw seal](../../../.q4t-work/suffix-admission-20261024/acceptance-v1/run/files.json) | `c56b48adafb8957fb7611783e3f4f7c5ff3b9d04cbaefe4bd3e5030bd9023650` |
| [v2 raw seal](../../../.q4t-work/suffix-admission-20261024/acceptance-v2/run/files.json) | `6cea30d12983739f12dbcae87dbe04e8bb3d6617e65e1c3c45453678ce7ef3a1` |
| [analysis.json](../../../.q4t-work/suffix-admission-20261024/analysis.json) | `4584ed7493f2fd9208047d76cf603cb4fab33ff31cfd6d5d0502045276a8cdf2` |
| [audit-evidence.json](../../../.q4t-work/suffix-admission-20261024/audit-evidence.json) | `3a296afa13e68638f114154327aae47f7257f93cd69f325817f58a7495e83c2e` |
| [audit-costs.json](../../../.q4t-work/suffix-admission-20261024/audit-costs.json) | `bcffcf07929824b79c67d50fe4f5feb321f8747b0ec4953a70ab6ff9282df69f` |
| [static-review.json](../../../.q4t-work/suffix-admission-20261024/static-review.json) | `93a35ff2fc8b2e2829f1e866a2726bd570b2b28a1cb730f0d92d35aba154e6fc` |

## 证据边界

累计v1 11/1加v2 239/17等于250HTTP/18启动，墙钟10941.64256秒。
成功合同实测2.660103秒，保守合同账62.660103秒另含失败suite完整60秒
额度。两个lease及两段资源样本分别保留，驱动间710.902秒修复间隔
没有连续监控；全部性能配对块在v2。原失败日志及未重跑的继承来源
不能改写为一次完整成功执行。

复用的直接GPU接受前缀oracle使用两层目标；层0、1均为GDN，未直接
验证目标full-attention/QSA长上下文状态。真实草稿的full-attention
KV/raw/compressed index有效前缀、page/RoPE、next_d0/next_g另有独立
重放核对。该合同通过显式proposal调用Multi，CPU准入由host合同验证；
两者均不能推广为任意整模型输入输出完全一致。

本轮八条独立timing请求均在旧8K/45K上得到零suffix命中；分相计时
描述的是神经回退路径，不能称为长重复suffix命中的直接GPU成本测量。
固定32仍允许部分接受；全阶段suffix分布为a0..a3=`[0,0,4,1106]`。
命中率、目标调用量相等和重复收益均不替代各档decode、TTFT及普通
decode保护审查。统一RAM采样也不能将逻辑payload大小直接等同于
进程或物理内存增量。

封存后的[措辞补充](closure-notes.json)限定decision中“剩余保护耗时回退
零命中”的表述范围：它指导致本轮不放行的主要新8K/45K/200K观测；
并非全部TTFT。子集新/开发重复文本有suffix命中且TTFT两块偏高，已在
原根审查面板和最终报告单列。补充不改变数据、决定或原seal，也不新增
测量；候选未部署，补充时再次核验默认程序/发布元数据身份保持。
