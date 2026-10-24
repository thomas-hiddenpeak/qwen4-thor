# 固定 k3 suffix：fixture 退出监控修复与 Nsight 收尾复核

2026-10-24，用户授权的新一轮有界工具修复及观测。父提交 `4e01cac`，
原阶段 `ENV_ABORT` 不改；不产生运行时优化候选或新的收益验收。

## 测试前冻结

失败进程 stat flags `4194316` 含本机内核定义的 `PF_EXITING=0x4`。
该标志表示进入退出路径，不能当成 pidfd 已终止。旧错误时同身份 R、
vsize/RSS=0、FDSize=0、无 Vm 字段，而 49.064ms 等待未获得 pidfd 终态。
先核对本机头文件与 Linux 退出/权限/通知顺序；不把缺字段或 R 单独当成退出。

唯一监控候选：仅 pinned 同身份 io/EACCES、已绑定非终态 pidfd，且初读及
复查均有 PF_EXITING/vsize=0、初始 status 明确非内核线程且无 Vm 字段，
转为显式 `EXITING_IO_UNAVAILABLE`。保留原错、原始字段和缺测，不填零，
不重读 io、不称已结束。复制并持有原 procfd/pidfd，每个正常采样周期仅
poll(0)及 pinned 身份核对；终态证据必须是同 pidfd ready，且 pinned stat
为同身份 Z/X 或 ENOENT/ESRCH。队列上限 16，每项从原错误起 5 秒绝对期限；
这是资源/失败检测上限，不是已测得退出时间。无此明确退出标志的错误仍按
原严格规则，非 io、稳定源权限错误仍中止，不扩大旧 50/100ms 同步等待。

pending 行正常保留采样原值及待确认账本，但不刷新完整采样心跳；步骤与
最终 fresh barrier 要求 pending=0。超限、过期、身份矛盾、坏 poll、停止时
仍 pending 或监控失败均 ENV_ABORT，释放自持描述符，不补造终态。

CPU：固定一次 64 个自有子进程、单线程/八线程各 32、总 60 秒；只用 CPU、
自身 proc 和 pidfd，不读外部进程或传感器。实际调用新监控，记录是否撞中
新分支、所有正常/错误轨迹及回收；未撞中不扩样。原失败轨迹另作离线回放，
不冒充原生成功。39 项原监控、30 项 Nsight 及新增边界测试合计不超过
100 methods；最多两次 suite、总 240 秒，第二次仅为具体缺陷修复。
必要构建不算测试；复用 fixture `ad0241d7`，不重新构建模型。

通过 CPU 合同及子进程清理后，仅一次共享 GPU 独占 lease 执行四控制：
direct-stop、wrapper-stop、direct-external、wrapper-external；每项 300 秒，
共 1200 秒。沿用 32 kernel、64 copy、32 固定非零 memset、33 NVTX 和独立
输出合同，每份实际采集副本删整 API/kernel 对及末 NVTX 两个负控。相同
Nsight/CUPTI/驱动、公开选项、fixture NVLOG 和日志上限；不扫描其他策略。

唯一模型候选仍是 wrapper-external：全部 known-work/负控及所选实际目标
diagnostics/终态/memory coverage 通过后，最多六个 HTTP、四次模型启动、
3600 秒，子集顺序 8K OFF、8K ON、200K ON、200K OFF。两个 200K 各保留
一个不采集的 8K 前驱。每组 900 秒，整体 5400 秒含 600 秒收尾，0 重试，
不预热、不改缓存/时钟/亲和性。竞争或监控失败仅停止自有进程并保存现场。

完整性警告与 known-work 分列；固定 fixture 完整不证明任意模型完整。
所选目标仍有完整性 warning 或未知错误则不进入模型。模型各 trace 仍须
反向 API/activity 和 memory coverage 核对；旧 6381 次 memset 缺口没有
参数解释，不白名单放行。首具体失败停止，不自动补测。

## 身份、复用与结论边界

复用 f0ff 观测程序/55363f18 推理源码、f356 默认程序、原发布 sidecar、
b63b0f25 子集 manifest。新 plan 记录工具依赖白名单变化和原/新 SHA。
原 37 正常 HTTP、6 CPU 观测及未变数学/质量合同按原范围复用；监控代码
改变后的合同本轮统一重验。完整 head 重复代码/文本 +13.68%/+14.40%，
子集共享 B 描述性 GM +6.53%/+6.73% 直接引用，不增加重复收益请求。

GPU 对照分别报告 CPU 墙钟、线程 CPU、API 并集、GPU 活动并集和同钟空隙，
按 prefill、prompt extend、proposal、accepted extend 分组；不强制对齐
不相容时钟，也不把 profile 时间当正常吞吐。无具体证据不提出运行时修复。
分别记录证据有效性、测量完整性、统计结论、工程决定。预算或停止条件触发
后归档，不改变旧 NO_GO/ENV_ABORT；不替换默认程序，不追加健康请求。

## CPU 验证与开测身份

97 methods 首 suite 全部通过，0 failed/errors/skipped，外层耗时 1.065500 秒。
没有使用第二 suite；其中原监控合同含两个各 1 个自有子进程的内核边界测试。
另一次固定 64 个原生子进程在 0.217599 秒完成且全部回收：
26 次立即同身份 Z/X，23 次旧有界 pidfd 确认，15 次新 PF_EXITING pending，
后者全部有生产 resolve 终态事件。原生循环只读自身 proc，无 GPU/传感器；
它没有模拟 GPU 驱动清理或证明所有退出都在 5 秒内。原 fixture 失败记录的
单独 CPU 回放只证明新准入，不虚构那次事后的 pidfd 时间或撤销 ENV_ABORT。

原生证明 SHA `80df75c8055a`，完整原始行、测试源码快照及日志保存在
`.q4t-work/suffix-nsys-rerun-20261024/`。本机内核合同 `954bf3320ffa`，
匹配 sched.h `187221043ac0`；上游 stable v6.8.12 源码支持退出顺序解释，
未取得完整 Tegra 补丁源码，不声称证明单次 EACCES 的唯一内核原因。
新心跳的陈旧限制仍独立生效，实际等待可能短于 pending 的 5 秒上限。

## 实际执行与停止点

只执行了一次 GPU 阶段，持有同一共享独占锁，耗时 **10.026332 秒**。
原始 driver 为 `STOPPED`；第三组失败清理需要强制终止自有目标，故收尾
`postflight` 为 **ENV_ABORT**。0 模型启动、0 HTTP，未进入子集 GPU 对照。
锁已释放；独立只读审计确认 15 个已知自有 PID/start_ticks 均消失，
相关进程组无残留。未停止他人进程，未重试或用剩余预算增加控制变体。

| 控制 | 已知 kernel/copy/memset/NVTX | 实际副本删除负控 | 实际目标 diagnostics | 结果 |
|---|---|---|---|---|
| direct-stop | 32/64/32/33，32 输出均正确 | 整 launch/kernel 对、末 NVTX 均被拒绝 | CUDA、NVTX 各一条完整性警告 | 已知工作局部通过 |
| wrapper-stop | 32/64/32/33，32 输出均正确 | 同上，两项均被拒绝 | 目标两条警告；另有空 wrapper 四条 | 已知工作局部通过 |
| direct-external | ready 收据有 32 成功 step；无完整 trace 审核 | 未执行 | 外部 stop 命令失败 | 强制清理后停止 |
| wrapper-external | 未执行 | 未执行 | 未执行 | 唯一候选门未通过 |

前两份 trace 中非零 memset 为 512/16384 字节各 16 次，关联、字节数和
顺序闭合。负控来自实际 SQLite 副本，证明验证器能拒绝明确删项；它们
不是原生事件丢失实验。第三份原始 `.nsys-rep` 留存，未补导出、审核或负控。

生产监控 **11 attempts / 11 flushed**、无失败，41 个进程观察中 39 OK、
2 个同身份 Z 的旧退出分类；缺失 io 保留，未填零。pending 准入/解决/poll
均为 0，不能把 CPU 的 15 次真实命中写成此次 GPU 生命周期覆盖。采样
平均 21.285ms、最大 22.599ms，flush 平均 0.253ms、最大 0.316ms；仅描述
该短阶段的监控成本，不外推推理性能。失败步骤清理后 fresh barrier 完成，
其范围是 owned-step PGID；外层 guard 的 detached 清理单列。正常成功
barrier 未执行，不补造成功记录。轮询不能排除短于周期的环境干扰。

## EACCES 修复的证据边界

本机 `PF_EXITING` 定义与旧原始字段一致。Linux 退出路径先设置退出标志，
随后释放 mm/files，再通知退出；proc io 的权限与所有权逻辑支持“未到
pidfd 终态时已经无法读 io”的解释。完整 Tegra 补丁实现未取得，不能将
这个解释升级成旧单次 EACCES 的唯一内核原因。

修复将这个已明确进入退出路径的窗口保留为有界 pending，仍要求后续
原 pidfd 与 pinned 终态共同闭合。它没有放宽为忽略所有权限错误。
25 项新增 pending 合同覆盖准入反例、身份矛盾、期限边界、容量和描述符
释放、错误 poll、停止时未闭合；原 39 项监控合同与 33 项 Nsight/runner
合同一并通过。64 个原生子进程全部回收；15 个 pending 均在单线程组，
初读/复查 R、后续同身份 Z，确认延迟最小/中位/最大
**0.530939/0.571171/0.625392ms**。这是紧循环调用生产 read/resolve 函数，
不是生产 1Hz 采样器的等待延迟，也不证明 GPU 退出在该范围内。

## Nsight 控制命令的具体原因与剩余未知

第三组实际使用 `nsys profile --capture-range=cudaProfilerApi
--capture-range-end=none --session-new=<owned name>`，再向同名 session
发送 `nsys stop`。stop 返回 1：`The request was not completed properly:
StopRequest.`。NVLOG 在成功路由/订阅后，明确记录
`RangeCollectionState::Stop` 抛出 **`Range collection stop is not expected.`**；
该拒绝发生于 SIGTERM 强制清理之前，不能归因于后续清理或监控 EACCES。

[NVIDIA 2026.1 指南](https://archive.docs.nvidia.com/nsight-systems/2026.1/UserGuide/index.html)
把 `profile` 定义为完整的非交互采集，`stop` 用于交互模式，相关例子使用
`start`/`launch`/`stop`。本地已保存官方文档身份。**公开合同与状态拒绝
共同支持本工具混用两种生命周期的判断**；专有内部条件未公开，不声称
读到了全部实现。此前“profile 可命名就可接受 external stop”的设计假设
在此纠正，旧报告作为历史证据原样保留。命名正确只保证路由，不能改变模式。

前两组已知工作完整，直接采集仍有目标警告，故不能只用空 wrapper 解释。
producer 的普通/forced flush、CUDA Done 与 NVTXFinish 已出现；这些不等于
consumer 终态确认。显式 drop 数未知，produced/imported 两个计数域未证
可比，不作相减。**目前既没有证明 warning 全是收尾误报，也没有证明
它们对应实际缺失。** 旧模型 6381 次成功 memset API 没有 GPU 活动的问题
仍缺参数解释；本次非零 fixture memset 完整不能为旧 trace 放行。

后续最小修复方向是文档支持的 interactive start/launch/stop：分别确认
各命令选项归属、精确 session 所有权、采集准备完成时点、目标和控制命令
终态，再冻结有界验证。本轮不实施第三策略、不重复 stop 或放宽 warning，
不新增采集。子集 200K/8K 的 CPU 提交、等待和 GPU 执行差分仍未补齐，
没有形成新的运行时根因或修复候选。

## 身份核验、准备修正与最终决定

独立审计首轮 rc0，**1914 冻结文件、68 原始文件、457 snapshot 文件**身份
一致，197 模型元数据项核对通过。监控源码 `f93f79314d3c`、runner
`a9dbc5a03dde` 与唯一 CPU suite/原生证明/实际阶段快照一致。未改推理源码、
f0ff 观测程序、f356 默认程序、发布 sidecar 或 BF16 子集产物。

准备目录 `diagnostic-v1` 没有运行或 lease：最初将 prepare stdout 重定向
到被自身冻结的 `prepare.log`，事前哈希核对发现仅该日志发生变化。
保留首版及核对记录，以同一准备源码和同一预算另生成 `diagnostic-v2`，
直接返回 stdout，plan SHA `f6951b0c65b2`。这不是第二次 GPU 尝试。
离线 monitor 分析首版少解析一个含换行 docstring 的 unittest 结果；仅
修正已证实格式并核对 97 个方法身份、summary 和 proof，保留 v1，最终
引用 `monitor-rerun-analysis-v2`。没有重跑测试或修改原始测量。

| 维度 | 决定 |
|---|---|
| 证据有效性 | 身份完整性 PASS；CPU/已完成控制可在各自范围内使用；整阶段 ENV_ABORT |
| 测量完整性 | 未完成：2/4 控制完成，0/6 HTTP、0/4 模型启动 |
| 统计结论 | 无新增模型 GPU/性能结论；不是性能 NO_GO，也不能追认优化通过 |
| 工程决定 | 监控修复通过有界 CPU 合同，短阶段监控正常；采集候选 NOT_ACCEPTED_TOOL_VALIDATION_ABORT，不部署 |

完整 head 重复代码/文本 **+13.6774%/+14.4034%**、子集共享 B 的描述性 GM
**+6.5335%/+6.7304%** 继续复用原 37 正常请求中对应开发材料证据，另 6 个
CPU 请求按原范围引用；新增重复请求为 0。该复用不构成独立启动统计
非劣性或五档性能验收，不改变旧 suffix 优化未通过的工程决定。

最终机器可读摘要见
[summary.json](evidence/mtp-suffix-nsys-rerun-20261024/summary.json)，
只读报告见 [collector](evidence/mtp-suffix-nsys-rerun-20261024/collector.md)、
[monitor](evidence/mtp-suffix-nsys-rerun-20261024/monitor.md)、
[audit](evidence/mtp-suffix-nsys-rerun-20261024/audit.md)。完整原始记录、分析脚本、
准备/解析修正链及前后源码保留在 `.q4t-work/suffix-nsys-rerun-20261024/`。

## 阶段收尾

`archive-closed/` 独立封存 **1085 条目、58051422 字节**，每项独立 inode、
复制前后及回读 SHA 一致；manifest `680b6859cb1b`，seal `4e366aef2a4f`。
包括 CPU 冻结源码/测试日志/原生轨迹、两版准备、完整 raw 与失败采集、
只读分析及修正链、审计、当前/旧监控源码，以及 f0ff/6a/f356 程序、
默认 sidecar 和子集 manifest 回退身份。原证据路径保留，未修改旧归档。
完整 seal/manifest SHA 见公开 summary 的 archive 字段。封存的公开文档为
加入本收尾段之前的快照，最终 seal 描述由本阶段 Git 提交记录，避免自引用。

研究分支继续使用 `codex/suffix-k3`，不另建入口。提交推送只表示代码与
失败证据已保护，不表示采集工具或 suffix 优化通过；默认程序保持，不执行
部署健康请求，不自动补齐未运行项。
