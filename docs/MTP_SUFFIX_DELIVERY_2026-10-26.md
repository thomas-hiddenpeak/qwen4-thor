# 固定 k3 suffix 交付目标与有界推进

2026-10-26，用户要求“按照交付要求配置目标推进”。本轮入口为
`codex/suffix-k3` / `d25a03069e2e74bf7a86ce6c3e73c52cd4ed1b8a`，工作区干净。
目标已配置：按证据修复、完成适用验收，满足条件后直接交付；未满足则
保留现有交付程序，归档明确决定。目标不等于允许连续重测直到通过。

## 交付边界

保持单流、greedy、MTP k=3、独立 suffix 开关；完整 head 默认，65536 行
BF16 子集显式选择。全局 token ID、目标四行 verify、checkpoint 恢复、
接受前缀 draft extend、next_d0/next_g 和请求独立索引生命周期合同不变。
不引入动态 k、PLE 提前读、head 量化或扩容、CUDA Graph、GPU 接受决策
或 SSD 专家策略。

最终交付仍要求完整/子集各自的同配置 suffix OFF/ON、适用五档 HTTP E2E、
普通 decode、TTFT、质量、统一 RAM 和生命周期保护；重复收益不能抵消
其他保护项已确认的回退。已通过且身份/依赖不变的证据按范围复用。
采用性能决策协议，证据有效性、测量完整性、统计结论、工程决定分列；
不虚构统一损失容差，不把 profile 耗时作为正常吞吐，不把统计不足改成通过。

通过后按既有授权保护回退包、替换适用程序并同步发布元数据、执行必要
有界健康检查、更新文档、提交推送。未通过不替换 f356 默认程序。研究
提交与推送不代表优化验收通过。

## 阶段 A：修复已证实的采集生命周期错误

上轮 `nsys profile --session-new` 后调用 `nsys stop`，实际到达正确 session
却被 `RangeCollectionState::Stop` 拒绝。该具体工具缺陷允许 CPU 合同优先。
本阶段只修复工具，不改推理源码或重新构建模型/fixture，不产生运行时
性能候选。Nsight 仍使用本机 2026.1.3，按该版本官方说明和本机静态 help
分别构造 interactive `start`、`launch`、`stop`、`shutdown`。

start 成功后才 launch；应用明确 capture end 后才 stop。控制命令成功、
目标退出、报告生成与完整性各自有收据。shutdown 只清理已证明属于本
driver 的精确 session，保持 kill=none，不冒充 stop 成功。短命 launch
不保证是目标祖先；稳定 step driver 在 start 前设置并验证 Linux subreaper，
以 owner PID/start_ticks、实际祖先链、exe、wrapper 收据及 pidfd 证明
目标归属。任何未证明归属的数字 PID 不得收到信号；Popen 子进程与被收养
进程分别回收，不混用退出状态。

命令选项按实际支持归属拆分；不把 profile 专有选项直接搬入 interactive。
CPU 模拟反例验证非法顺序、会话/目标身份、控制失败、目标失败、收尾与
原 monitor pending 屏障。两文件 `test_run_stage.py` 与
`test_interactive.py` 合计最多 **40 methods**；首 suite 最多 **120 秒**，
最多 **2 suites / 合计 240 秒**，第二次仅修复具体失败。成组实现并冻结
源码后统一运行；无新增原生退出复现、传感器或模型测试。

首 suite 已完成：32 methods（runner16/interactive16），0 failure/error/skip，
执行及收尾0.786225秒，源码未变、无清理错误/临时残留。未使用第二suite。
CPU proof绑定工具及依赖源清单20941项（含evalscope虚拟环境），清单范围
不代表全部测试运行。runner SHA `1787c5db`、interactive `a1370503`、
prepare `b2640a26`；实际Nsight进程关系与subreaper仍由后续控制验证。

## GPU 预算、顺序与停止条件

CPU 通过后仅一次完整阶段驱动持有共享 GPU 独占锁：

| 顺序 | 工作 | 固定上限 |
|---|---|---|
| 1 | interactive direct fixture | 1 次，300 秒 |
| 2 | interactive wrapper fixture | 1 次，300 秒 |
| 3 | 子集 8K OFF、8K ON、200K ON、200K OFF | 仅采集门通过后，4 次启动、6 HTTP，3600 秒 |
| 收尾 | 独立目标/会话清理、监控 flush、锁释放 | 600 秒预留 |
| 整阶段 | 同一 lease、含 export/审核/收尾 | 4800 秒 |

每个模型组最多 900 秒；两个 200K 各先处理一条不采集的 8K 请求，保留
原请求位置。输入身份及输出期望沿用旧冻结材料，实际输入 8170/204781
token、输出上限 256、单流 greedy。0 重试、0 新预热、0 新正常吞吐或
重复收益请求；自然继承页缓存，不 drop cache、不改时钟/功耗/亲和性。
该阶段全部材料为开发诊断，不转作新的验收语料。

fixture 仍为原 `ad0241d7`：32 kernel、64 copy、32 个固定非零 memset
（512/16384 字节各 16）、33 NVTX、32 独立输出检查。每份新 SQLite 副本
各删除整 launch/kernel 对及末 NVTX，必须被拒绝。NVLOG 每份最多 64MiB，
两份合计 128MiB。仅 wrapper interactive 是预先指定的模型候选。
direct 的完整性警告保留作比较，不改选更有利变体；已知工作、负控、
身份、命令失败立即停止。wrapper 实际目标的 warning/未知错误、未闭合
producer 或 memory coverage 均禁止进入模型。空 wrapper 警告仅按已有
严格 PID/空表合同单独分类，不替目标豁免。

每份模型 trace 仍需反向 API/activity、memory coverage、目标 diagnostics
和 CPU/NVTX 身份审核。旧 6381 次成功 memset 无活动的缺口缺少 API 参数，
次数与 cuBLASLt 小 N 投影吻合不证明 no-op 或丢事件；本轮不增加阈值、
白名单或临时参数观测。新 trace 仍有未解释缺口时记 UNKNOWN_CAPABILITY
并停止，不输出不完整 GPU 并集的性能结论。

竞争、监控失败、无法闭合自有目标/会话清理记 ENV_ABORT；仅停止自己的
进程，保留原始记录，不自动重开 GPU 阶段。轮询不证明没有短于周期的
干扰。CPU/控制局部通过不撤销旧 ENV_ABORT，也不追认整阶段通过。

## 身份与复用

观测程序 `f0ff0fe8`、推理源码 `55363f18`，参考研究程序 `6a4d3d62`，
默认程序 `f356b2c9`、发布 sidecar `422993b0`，子集 manifest `b63b0f25`。
实际完整 SHA、197 模型元数据、fixture、Nsight/CUPTI/驱动依赖与请求文件
由本轮准备工具核验并冻结；准备只收显式输入，不递归冻结自身 stdout。

复用上轮 97 项中的未变监控 64 项及固定 64 原生子进程记录；受 runner
变化影响的原 11 项本轮重验。fixture_audit 14 项与 analyze_gpu 8 项仅在
源码及依赖不变时复用。原 direct-stop/wrapper-stop 两份已知工作及实际
删除负控按已封存 SHA 作为历史局部对照引用，目标 warning 和原阶段
ENV_ABORT 不改，不合并成新采样。

原 37 正常/6 CPU 请求按身份及范围复用。完整 head 重复代码/文本
+13.6774%/+14.4034%，子集共享 B 描述性 GM +6.5335%/+6.7304% 保留，
不等于跨启动统计非劣或完整五档通过。已有数学/HTTP 质量、接受前缀
参考与取消恢复合同按原覆盖范围复用，不凭目标 verify 声称任意输入一致。

## 后续分支与本轮出口

采集门与四模型组全部通过后，统一分析 prefill、prompt extend、proposal、
accepted extend 的 CPU 墙钟、线程 CPU、API 并集、GPU 活动并集及同钟空隙。
不同时间域不强行相减；wall−thread CPU 不命名为 GPU 时间。

只有出现可区分、可操作的具体运行时原因，才选择一个有界可回退候选，
在启动任何新测试之前另写其身份、收益目标、全部保护项、五档/两 head
矩阵、请求顺序及总预算。当前阶段不授权自动消费该后续预算，不允许
以“交付目标”为由继续同身份采样或扫描匹配参数。没有具体原因、工具
能力仍不足或冻结条件触发时，保留现有版本，给出未决/不接受决定并归档；
不要求先找到根因才能保留基线，也不把未决写成因果目标已经完成。

本轮首先完成这一个具体工具修复与条件诊断阶段；通过项、失败项和未
运行项分列，保持少量有效分支入口和所有失败证据。

## 实际结果与决定

唯一GPU阶段运行 **7.042834784秒** 后触发冻结停止条件；原始状态STOPPED，
postflight ENV_ABORT，外层guard COMMAND_FAILED。只启动首个direct-interactive，
32个host输出校验通过，正确start/launch进入RangeCollection；同名stop已连接
正确代理，却在 `RangeCollectionState::Stop` 被拒绝，rc=1。强制清理导致目标
退出、报告生成及会话自动Shutdown；后续shutdown因无活动会话返回1。

这纠正了上轮的充分因果解释：正确interactive同样复现拒绝。随后官方
支持实例表明按命令分类将profile/stop一般判为不兼容也过强，见
[后续合同复核](MTP_SUFFIX_COLLECTOR_DEPENDENCY_2026-10-26.md)。公开资料未明确验证本机版本的
cudaProfilerApi + capture-range-end=none + manual stop组合。日志已定位
内部拒绝状态，尚不能区分未说明的组合限制、实现缺陷或其他内部条件；
没有选出有证据保证的本地补丁，不追加选项扫描或采集。

报告78991字节、SHA `523303e836f4`保留；它在失败清理后生成，不代表stop
成功。未导出SQLite，未做本轮GPU已知工作/负控/完整性审核，实际目标退出码
也无独立收据。launcher143和nsys-launcher的−15不能代替fixture退出码。
wrapper、四模型组、全部6HTTP均未运行，warning分类及8K/200K耗时差仍未决。
详见[生命周期法证](evidence/mtp-suffix-delivery-20261026/lifecycle-forensics.md)。

监控8/8、424项source观察完整，无监控失败；两次同身份Z态io EACCES保留
缺值，未填零，pending分支触发0次。失败后新采样屏障仅覆盖step PGID，
不冒充正常完整收尾屏障；外层guard清理及释放、独立/proc残留核对单列。
7个已记录PID/start_ticks均消失，已知进程组无成员；不据此倒填目标退出码。

独立离线审计核对23314冻结文件、21297源码快照、34raw及197模型元数据；
全部身份相符。新32项CPU首suite与未变86项、原生64证据分别闭合。准备时
静态capability元数据漏写performance_acceptance被拒绝，原scope已非空；
保留首次失败和说明修正链，v2仅补静态声明，不改源码、不重跑CPU或GPU。
CPU依赖清单含虚拟环境，不冒充20941项测试。审计与精简结果见
[audit](evidence/mtp-suffix-delivery-20261026/audit.md)和
[summary](evidence/mtp-suffix-delivery-20261026/summary.json)。

| 字段 | 本轮结论 |
|---|---|
| 证据有效性 | 文件完整性PASS；阶段ENV_ABORT（控制/清理失败，监控本身完整） |
| 测量完整性 | false：0/2新控制完整通过，0/4模型启动、0/6HTTP |
| 统计结论 | NOT_EVALUATED_NO_MODEL_DATA；无新的GPU或正常吞吐结论 |
| 工程决定 | NOT_ACCEPTED_TOOL_VALIDATION_ABORT；保留默认，不部署，不扩样 |

本轮不是新的运行时性能NO_GO，也不撤销原min32候选的保护项未通过决定。
两head重复收益仅按原范围复用，不能抵消缺少的可信GPU对照或替代适用五档
验收。当前交付缺口仍为采集支持合同/完整性门、子集8K/200K归因，以及只有
出现具体运行时候选后才有意义的新验收矩阵。暂不具备交付条件，按预定负向
出口归档；没有更换程序或发布元数据，因此不新跑部署健康检查。

## 负向收尾

`.q4t-work/suffix-delivery-20261026/archive-closed/` 独立封存42353条目、
607180719字节，逐项独立inode、复制前后SHA及回读一致。manifest
`e98dfe6258236e6cf17e3a6a412406086c14a828dbb371b9a0a0bb6104711e52`，
seal `50d40526d7a6357199d8eff078f97a9a6b398720a865cc2f3b8ac30a0c9c5702`。
保留原始目录，独立保存f356默认程序、sidecar、f0ff观测程序、6a参考程序、
子集manifest、CPU/GPU失败材料与源码；工具回退锚点为进入本轮的d25a030。
先前archive seal按原SHA关联，未修改历史证据。公开文档/summary的封存
快照位于追加本段及archive字段之前，最终收尾由Git提交记录，避免自引用。

本阶段按用户既有授权提交推送codex/suffix-k3。没有更换默认、发布候选、
新建分支或清理证据；不把研究提交作为性能验收通过。
