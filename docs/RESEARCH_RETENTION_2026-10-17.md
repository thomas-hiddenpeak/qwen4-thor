# 剩余研究分支与未提交内容留存评估（2026-10-17）

> 2026-10-17后续执行已完成：path-c本地/远端引用已删除，main包含其tip；
> mtp-perf与offload研究分支已改用远端annotated标签
> `archive/mtp-perf-20261009`、`archive/offload-mirror-recycle-20261006`
> 持久保留。已从独立空bare仓库取回并核对commit，后者覆盖全部旧offload
> 阶段。两份未提交内容归档于e1383e1后已清理，工作区保留原HEAD detached。
> 下文原清单/建议是执行前记录；恢复offload历史时使用归档tag而非已删分支。


按用户要求，仅检查现有代码、历史记录和证据；不测试、不采样、不新增
分支。本阶段形成清理建议并归档两份未提交工作，未删除研究分支或工作区，
未reset/stash原始修改。main仍为28a3783，不引入旧研究实现。

## 处置结论

| 对象 | 已有证据与独有价值 | 建议 |
|---|---|---|
| codex/moe-residency-path-c-20261001 | 本地887a3fe、远端df02f60均已被main包含；未提交内容只补mirror统计，main已有对应实现 | 补丁归档后可清理分支和工作区的tracked修改；无需另保活跃研究入口 |
| codex/mtp-perf-20261009 | d802fce仍有5个main未包含提交；包含旧sequential归因、logits诊断和独立状态测试；未提交故障夹具最终摘要失败 | 保留研究内容，停止作为活跃开发分支；完整历史有持久归档锚点后可删除分支；夹具保留为未完成参考，不直接移植 |
| codex/offload-mirror-recycle-20261006 | 7672b59有23个main未包含提交，还是刚清理的10个offload阶段的共同保留锚点；有完整否定结果和工具 | 保留一个只读研究归档入口，不继续候选优化、不整分支合入；若转归档tag，确认远端可恢复后才能删分支 |
| 当前工作分支 | main之后的验收规则、工具与调查记录尚未合入 | 继续作为唯一当前工作入口，不为本评估建分支 |

“有独有提交”不等于值得进入生产，“候选NO_GO”也不等于研究历史可删除。
本轮建议保留内容、减少活跃入口，尚未创建tag或删除剩余引用。

## path-c：功能已被主线覆盖

工作区`.q4t-work/wt-c1`只有两个tracked文件变化：
`include/q4t/model/model.h`在SumResidencyStats累加mirror_hits/writebacks/skips；
`src/server/chat_generation.cpp`输出三项请求差分。main对应代码已经累加
这三项，日志已有mh/mw/msk及更完整的L2/partition计数；旧补丁使用ms，
不应为保持旧字段名倒退当前接口。本次没有发现值得重新合入的独有算法。

[原补丁](evidence/research-retention-20261017/path-c.patch)与基准HEAD已归档。
可以按精确文件清理tracked修改，再把工作区留在原commit detached并
删除本地/远端分支。工作区目录是否删除仍取决于ignored构建/证据依赖；
本次没有全面清点ignored内容，不能据此执行git clean或递归删除目录。

## mtp-perf：保留测试意图，旧实现不作当前基线

按merge-base计算的5个独有提交涉及旧MTP性能归因、logits dump与状态检查。
不能只按提交未合入判定所有改动都缺失：bench_full_attn.cu与main当前
字节相同，其清理已被覆盖。旧分支把InvalidateVerifyCheckpoints暴露给
测试；main仍为内部函数，不能将旧测试直接复制后宣称接入有效。
main没有旧logits_dump.h、mtp_t4_state_test.cpp和mtp_sequential.cpp，
当前默认MTP和数值准入也已演进，应按需求移植单项诊断意图而非整分支合并。

旧docs/log/2026-10-10.md记录状态比较四步通过，同时明确：阈值曾根据
run5分布校准（rel_bf16=0.25、rel_fp32=0.50），并保留多次前置/诊断失败。
它仅是旧身份下结构精确、浮点有界的局部研究证据，不替代当前N=4合同，
也不能靠删除该分支抹去先前失败或改写为当前版本通过。

未提交CMake增加一个EXCLUDE_FROM_ALL链接变体；未跟踪的
mtp_t4_prefault_server.cpp通过wrap注入draft上传返回错误，检查排空、
输出失效、调用者状态不变、不回退普通decode及后续请求恢复。
这项测试意图值得保留，但其强绑定旧C++符号/内部字段，需要当前接口
适配与合同覆盖审查；目前不把它安排成新测试任务。

实际存在step3-prefault/run3历史记录：summary为passed=false、
failure=AssertionError()、server_exit=0。日志记录注入、drains=1、
输出失效以及三请求结束，但局部日志不能推翻最终失败；摘要没提供
具体失败断言位置，本次不运行checker或追加诊断来猜测。也不能由
旧夹具失败推断main存在同一缺陷。当前main存在其他checkpoint、
多序列、PLE和生命周期合同，本次未做逐项覆盖证明，不声称全已替代。

[归档目录](evidence/research-retention-20261017/)保存原CMake补丁、完整
未跟踪源码、原runner、失败摘要与服务器日志，以及SHA256和原路径。
这足以恢复未提交内容；不声称复制了全部HTTP响应、模型或构建产物，
原始step3目录继续保留。建议历史提交转为持久只读归档后再清理分支；
不为了分支治理启动故障修复、重跑或重新验收。

## offload：保留否定证据与离线工具，不保留为优化待办

7672b59报告记录17服务/131 HTTP，质量、所选真实权重合同和干预发生
检查通过，性能按原冻结规则NO_GO：六档仅8192/45056全部门通过，
history的14个位置都至少失败一项。不得用今天的新规则倒推旧候选通过。
整体物理RAM≤54GB仍INDETERMINATE；PSI等资源观测缺项不改写为零。

候选新增默认关闭的GPU覆盖mirror回收优先级，并带来选槽与计数复杂度。
现有结果不支撑推进或默认启用。研究链中的cache replay、supply分析、
retention上界及observer工具保留为未来明确问题的素材，不整体迁入main，
不把旧性能数据用于当前全驻留MTP基线。

该分支也是此前10个已删除offload阶段引用的唯一保留远端分支锚点。
现在直接删掉会破坏上轮承诺的恢复路径。合适方案是把7672b59完整历史
保存在持久远端归档tag或经校验的bundle，再改清理清单的恢复锚点并
删除分支；不是再建一个研究分支。本次先保留现有引用。

原报告说明246份存储压缩映射及payload只在本地保存，并依赖原路径恢复；
因此源码分支归档不等于工作区和证据目录可以删。目录清理前必须另外
核对这些映射、ignored内容和实际依赖，不能按Git干净状态推断无数据价值。

## 本阶段交付边界

两份未提交工作均已复制归档并记录哈希，原工作区状态保持。
状态/当天日志更新并提交当前分支。没有删除源文件、清理构建目录、
创建新分支、运行测试或改变任何验收结论。下一步若执行引用归档清理，
优先path-c；mtp-perf/offload先建立持久历史锚点，最后才评估磁盘目录。
