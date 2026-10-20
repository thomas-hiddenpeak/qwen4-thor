# 启动资源计划：临时锁页准备合同

2026-10-21，可靠性任务。配置推导计划已完成有界验收，默认19ea不变。
本文件冻结上一轮具体准备失败后的修订，不追改任何旧证据。

**最终结果：PASS_BOUNDED_RELIABILITY。** 候选1d660365在规定的
完整热门准备条件下，默认MTP两请求、同配置重启、no-MTP容量边界
全部通过；4条1K HTTP精确匹配，280次NVML全部正常。计划内设备
资源全部交付和释放，默认程序未替换。旧中断保留，完整内核时间轴
不属于本次接受证据。细节见文末最终验收。

## 已知事实与修改理由

上一轮完整顺序读之后，9个早期文件缺2868621312 bytes，尚未启动
排序候选1d66。封存内核记录有729313页文件回收，misc reclaimable
下降2.5GiB，NVIDIA shrinker也返回过回收对象；不能说驱动完全没有
回收，也不能将misc或对象数直接换成NVIDIA池物理字节。

原预热的64GiB参数只限制选定文件总量，不是预热进程memcg限制。
本轮增加真实helper及父进程的PID/出生身份、cgroup及祖先观测；
未知接口保留未知。当前观察不能补写历史进程归属。

本机CONFIG_SHRINKER_DEBUG关闭，没有可用的generic shrinker scan
接口。NvMap属于另一模块，不能拿其接口替代NVIDIA sysmem页池。

顺序读取没有持续驻留合同。本轮尝试普通用户helper只读mmap并mlock
精确选定的192个专家文件，准备期间保护这些页。Linux说明mlock页
不被回收、但可以迁移；不能据此宣称优先回收NVIDIA池或消除规整。
参考[Linux 6.8 unevictable LRU](https://www.kernel.org/doc/html/v6.8/mm/unevictable-lru.html)。

## 冻结范围与所有权

- 使用已构建1d660365候选及原编译输入，不修改候选或默认19ea。
- 选定文件逐页向量绑定旧seal，总67987791872 bytes。非选定payload
  仍要求0；不减少完整热门、不放宽额外驻留、不补读重试。
- 精确限额仅属于独立helper。启动链先给自身设置memlock限额，再
  降为调用者UID/GID、清capabilities并设置no-new-privs后执行helper。
  不跨PID提额，不修改用户或系统长期限额，模型和HTTP父进程限额不变。
- helper完成全部mlock/mincore后持有；父进程核验完整向量，再要求
  逆序munlock/munmap、VmLck=0、正常退出并回收。所有操作只读模型。
- helper退出后重新核验原完整向量，随即创建模型scope。锁不能跨越
  模型启动。任意缺页、未知归属或释放不明即停止，保留失败现场。
- 新准备会改变LRU/回收/物理布局；相同向量不是相同物理碎片状态。
  若通过，仅接受“新准备合同+排序预留+受测配置”的可靠性，不能
  单独归因排序收益，也不宣称驱动根因修复。

## 有界验证与停止条件

先完成≤2文件、≤8MiB的CPU真实锁页合同及runner合成门禁；包括实际
受限提权后降权分支、部分失败、中断/EOF/父死和正常释放。复用未受
影响的110项原计划/owner/wrapper/runner合同，不重建模型。

CPU合同通过后顺序执行以下矩阵，每阶段完整GPU lease，原NVML完整
查询5秒时限及1秒轮询不变。每阶段只准备一次、只启动一次。

| 阶段 | 配置 | HTTP | 验证重点 |
|---|---|---:|---|
| mtp-lock-v1 | 默认MTP k3、C256、单槽、prefill8192、len262144、L2=16/mirror=8/threads=16 | 2 | ready前计划全部交付、两请求不重分配持久块 |
| restart-lock-v1 | 同上重新准备及启动 | 1 | 派生计划SHA相同、退出回收后可重启 |
| boundary-lock-v1 | no-MTP、C128、双槽、prefill2048、len8192、L2=1/mirror=0/threads=1 | 1 | 资源/容量边界与真实计划匹配 |

每请求复用冻结1K文本，要求1024输入、7输出、stop、精确710003。
这只是受测文本的HTTP质量记录，不扩大为完整模型质量结论。

仍在原累计5次模型启动/5条HTTP上限内：历史已用2/1，本轮最多3/4。
新增准备上限3次，按表顺序且任何失败即停止后续，不以失败准备未用
模型预算为理由无限重试。每helper获取120秒、每阶段总300秒；超时
清理不能保证D态立即退出，清理不明须保留recovery标记，禁止下阶段。
原错误、ENV_ABORT和NOT_RUN全部保留。

零丢失内核时间轴、资源观察、NVML全查询、scope退出、helper无遗留、
默认SHA及证据seal均检查。非性能验收，不启动五档性能矩阵或提出
统计提速结论。当前仅冻结方案，尚未执行全量锁页或新增模型加载。

## 证据与代码

新产物统一`.q4t-work/startup-cache-20261021/`；旧startup-plan目录
4004文件最终seal SHA为
`2f2fa5979bdb61c2a4b88d20c4ca1183629d8fb81ae999b27e90c7639e65a4d3`，
只读保留。研究子目录为research-pressure和research-kernel。

入口为`tools/verify/startup_cache/run.py`，是旧阶段runner的明确冻结
派生，保留旧source以便复核历史；不属于产品常规启动路径。
锁页owner与cgroup观察分别为locked_warm.py和cgroup_observer.py。
最终执行结果另行追加于本文件，不用提交/封存代替运行时接受。

## 首次全量结果与一次采集容量修复

mtp-lock-v1已按停止条件结束，后续两阶段没有执行。原完整向量
67987791872 bytes在HELD及解锁/退出后均通过，192文件全部锁页、
解锁和unmap；helper退出0，实际UID2002、全部capabilities=0、
no-new-privs=1，真实归属session-5.scope。默认与候选均未改变。

阶段因采集器达到536870912-byte文件上限停止；supervisor随后
终止guard/model，不能将guard的ENV_ABORT误记成NVML超时。
49次NVML查询全OK；1次模型启动、0HTTP、594/1462笔物理分配完成，
总63671519232/64489419144 bytes（98.732%），尚无完整handoff。
其中trace最后时间704243.754574前为482笔、62937516032 bytes
（97.594%）；其后服务器又完成112笔才被终止，不能把这些笔的
应用日志当作已有完整内核覆盖。
scope已inactive/dead、helper无遗留、trace实例移除且全局配置还原。
环形缓存loss计数0，但至少15432 bytes未写与最终455 entries未排空，
因此内核时间轴明确INCOMPLETE，运行时仍不接受。

只读分析确认：锁页期间仅2.389MiB trace；物理预留9.690秒已写
495.701MiB，占文件96.817%，均速51.157MiB/s，峰值68.076MiB/s。
不是准备失效或已观测cudaMalloc错误。旧406文件seal为
`6348d946242c5d4c74809d8a7b59460a231b0eb261b2d539d0556551558c97ff`。

据此冻结唯一工具修复：仅将已支持的trace字节上限增加到2GiB，
不改事件选择、NVML5秒、helper、模型候选、配置、精确向量或质量门。
比首次多1536MiB，按已观察预留均速约容纳额外30秒，按峰值22.56秒；
不保证未知后续加载/退出事件必然足够。再次触及上限仍停止，不继续扩容。

替代矩阵为mtp-lock-v2、restart-lock-v2、boundary-lock-v2，配置及
请求数沿用原表。仅为工具中断增加一次启动：累计模型上限从5修订
为6，HTTP仍5；历史和v1实用3/1，剩余仍3启动4HTTP。新准备总上限
4（含已停止的v1），v2任意失败阻止后续，不自动重试。封存失败身份
必须匹配；只有这份明确的collector截断允许进入修复后矩阵。

CPU证据：15项observer、15项锁页owner（含实际sudo启动链和父死）、
22项runner门禁通过；时序记录单项补验通过，未受影响合同直接复用。
采集容量/失败历史预算改动只补受影响的CPU门禁后才开始v2。

## v2结果与可靠性门禁分离

v2再次通过完整热门准备，但kernel ring overrun5501，trace仅
207554304 bytes，未触及2GiB上限。supervisor因此停止模型；42次
NVML全OK，最长0.363512秒，177笔设备分配完成54791309312 bytes，
没有handoff/ready/HTTP。helper退出0、VmLck0、scope已移除、资源
采集完成且trace实例/全局配置清理还原。旧397文件全部复核，seal：
`8cad08ff5964c8c429167ec26f4e498b490a00424808b9c6f98f7cc28675fe48`。
v2仍为FAILED/INCOMPLETE，不算运行通过，也不是NVML超时。

两次停止证明采集器自己的容量/消费吞吐边界阻断了后续验证，不能
证明trace让分配变慢或去掉它就必然通过。本目标是配置/张量启动
资源计划的可靠性，完整内核因果归因并非必要的产品正确性合同。
因此停止扩展采集容量，按两个独立审查的相同建议，将高频全系统
raw trace移出本矩阵；不静默忽略丢失数据，也不追认v1/v2通过。

v3明确记录`collector_profile=resources_nvml_application_ledger`、
`kernel_timeline=NOT_COLLECTED`。保留driver身份、resources中的
vmstat/buddy/cache/cgroup、NVML完整查询5秒/1秒轮询、GPU独占、
逐笔应用预留/交付/释放、完整热门、ready、精确HTTP及重启合同。
resources或NVML失败仍停止，任何数值/资源/质量门均未放宽。
不再提供逐PID规整/shrinker完整因果时间轴，不宣称驱动根因闭合。

最终矩阵为mtp-lock-v3、restart-lock-v3、boundary-lock-v3，配置
与请求仍沿原表；两次collector中断分别固定seal保留。一次性累计
预算修订为7次模型启动/5条HTTP，新准备共最多5次（含v1、v2）。
当前累计4/1，剩余3/4。v3任何失败停止，**本阶段不再追加GPU尝试**。
唯一变化是去掉附加诊断采集，候选、helper、资源/NVML监控和质量
要求均不变；先核验受影响的profile/历史预算门，再执行矩阵。

## 最终验收与交付边界

最终累计使用7次模型启动、5条HTTP；其中本轮5次准备、5次启动、
4条HTTP，均已达到最终冻结上限。前文“剩余预算”只记录当时决策，
当前没有剩余GPU尝试，不再追加采样。

独立审计`audit-final-v1.json`为PASS_BOUNDED_RELIABILITY，五个新
阶段共2167份封存文件核验通过；v1/v2仍为未接受的中断，只有v3三
阶段用于本次运行时接受。所有实际启动前都有同一完整热门向量，
helper全部解锁并退出，无补读重试。MTP命令未传开关或k值，实际
采用候选默认k3；请求窗口内确有MtpDraftExtend及MtpSpeculativeStepMulti
调用，并非只检查loaded标记。

| v3阶段 | 计划内设备块 / 字节 | HTTP | NVML | 结果 |
|---|---:|---:|---:|---|
| 默认MTP | 1462 / 64489419144 | 2 | 100/100 | 同实例指针保持、全交付/释放、exit0 |
| MTP重启 | 1462 / 64489419144 | 1 | 92/92 | 同计划SHA、全交付/释放、exit0 |
| no-MTP容量边界 | 1397 / 28433536796 | 1 | 88/88 | 双槽容量配置生效、全交付/释放、exit0 |

每条请求均1024输入/7输出、stop、精确`710003`。边界是双槽配置的
启动资源和单请求验证，不是双并发测试；262144/8192是配置容量，
不代表本次实测了这些长度的输入。仅这一冻结文本的质量，不扩大
为全模型语义质量。两次MTP计划SHA均为
`f9dce3b36ec7cdb1bfe09425d4be8accea0be7ed40a7dc66a1a0aff30771afbe`；
边界计划为
`fbd1c2cced9524839c36f4dade92a99b2c0c5e3ec751a7e83e9f0c3fee928f00`。

观测计时原值如下，只作可靠性任务的有界记录；配置/热度不同，
没有对应五档性能对照，不作优化、统计持平或非劣结论。

| 请求 | 启动到ready / 秒 | TTFT / 秒 | 请求总延迟 / 秒 |
|---|---:|---:|---:|
| 默认MTP首请求 | 25.555693 | 17.878927 | 18.206090 |
| 默认MTP同实例复用 | 同上 | 7.845909 | 8.069124 |
| MTP重启 | 31.167915 | 18.782054 | 19.075666 |
| no-MTP边界 | 9.090929 | 32.436832 | 33.474893 |

日志startup_resource_plan仍为元数据推导与物理预留的合计，未单独
测量纯元数据时间，保持UNKNOWN。280次完整NVML查询最大0.811842
秒，仍执行原5秒时限。连同v1/v2共371次新查询均成功；这不能证明
历史驱动等待根因已解决。资源采样有按调度跳过的间隔、PSI不可用
等限制，不能把抽样或缺失解释为连续无干扰/无压力。

三阶段计划内块均显式cudaFree成功，handed_live、unhanded_remaining、
release_unknown均0。完整应用逻辑轨迹的MTP两阶段device/host存活
字节为0；边界另有两笔各993280 bytes的ArgmaxBf16Rows静态缓存
未显式free，共1986560 bytes，属于本目标事先排除的首次argmax
进程级缓存。它不在启动预留计划内，进程退出结束其context；不能
写成所有运行时分配逐笔释放，也不证明驱动物理页池已经归还OS。

本次接受覆盖：配置/真实张量形状推导、严格计划/实际分配合同、
ready前默认MTP scratch/checkpoint、受测容量配置、既有CPU失败
回滚合同、真实正常退出与重启。复用原110项CPU合同；新增15项
cgroup观察、15项锁页owner、22项runner合同及受影响的时序/预算/
profile增量检查。元数据/构建输入身份与此前候选一致，未重建或
重复未受影响的模型验证。

收尾将仓库内runner测试夹具同步到v3阶段名、两项中断历史及最终
预算，当前22项CPU替身合同一次全部通过；仅更新测试，不改driver，
未重新执行真实锁页、模型或HTTP。旧各版测试和结果原样保留。

不接受或不主张：未经上述准备的任意继承缓存直接启动、任意模型
或任意配置、任意loader中途失败的进程内恢复、物理RAM总限额、
所有运行时临时分配纳入启动计划、完整内核因果时间轴、驱动根因
修复和性能收益。准备helper位于真实session cgroup，其可读祖先
memory.max/high为max；模型64GiB/swap0 scope是另一边界，不将
它称作整个设备或准备过程的物理RAM限制。

默认`build/q4t`仍为19ea4e57，候选仍1d660365。实验开关
Q4T_STARTUP_RESOURCE_PLAN默认OFF，产品默认没有替换或部署操作。
候选、原构建输入、各版runner及所有失败和成功证据保留；源码
实现阶段为e3bf608，本次完成准备工具及验收收尾，可通过Git回退
工具改动，未要求更换默认程序。当前无常驻模型、残留scope或锁页
helper。本目标完成，不再追加GPU测试。

## 最终封存

可移植[审计摘要](evidence/startup-cache-20261021/audit.json)记录候选/
默认身份、逐阶段结果、CPU复用与补验、计时原值和接受边界。完整本机
证据为`.q4t-work/startup-cache-20261021/final-evidence.json`，包含
2593文件（其中五个运行阶段2167文件），全部重新计算SHA一致。
清单自身SHA256：
`9125af44cbf5379eb43094e4abfb55805e5e7fbb37b0022a37f800adede77c27`。
旧4004文件封存目录保持只读；本轮失败和成功记录均未删除或覆盖。
收尾核验默认SHA、当前模型进程与scope退出、最终源码语法及diff，
均通过；无部署或额外GPU活动。
