# Offload RAM预算与缓存协议：修复、有界对照与推进建议（2026-10-02）

本阶段Goal已完成预算/工具修复、固定HTTP质量与两组有界资源对照。
首轮校准和冻结过程保留如下；完整物理RAM仍未知，当前未接受新的
性能策略或完整性能矩阵。GEMM冻结，Phase D off。

## Goal 结果与推进建议（2026-10-02 20:40）

本阶段预算/工具修复、固定质量与两组有界探索已完成；新性能策略未
实施，完整性能矩阵未运行，54GB整体RAM目标未签PASS。完整去重物理
总量仍为INDETERMINATE；旧68.34GB不能恢复为物理峰值。

### 身份、验证与提交边界

工作区binary完整SHA：
`46b3f977b0653c2f317c63df5b9f6669d1f3bb1de17892970b605676dd5434bc`。
入口HEAD为699cf68，构建零警告；budget host与ASan/UBSan各13/13，
工具45/45及原验收工具30/30通过。集成q4t_tests构建零警告，13项新
合同确已链接/注册，未重复运行无关GPU测试。真实1%估计预算启动
在model_load前返回1，原无解回退的反例另存。

固定HTTP质量11/11，含200K，与dc845dd3参考逐字一致。两组各3条
45056输入/256输出、finish=length，首组与旧全驻留输出摘要一致，
受限组与首组一致；所有组requested/effective均262144/1/8192。
三服务与monitor退出0、独立unit清理完成，226文件device/inode/
size/mtime及binary摘要前后不变。没有复用旧身份的HTTP资格。

该binary仍含入口原有且关闭的Phase D及统计改动。阶段提交只纳入
本Goal预算/工具与文档，原7处未提交内容保留；提交树与完整工作区
binary不是同一源码身份，不冒称干净提交树已获本轮HTTP发布资格。
源码副本及manifest、入口diff、命令、工具副本和SHA均在证据根。

### 两种资源条件的观测

两组开始时201个payload与全部226个模型文件cache均验证为0，加载
本身会重新暖缓存。服务连续，客户端每发重启，swap=0，请求间不清
cache/改限额。不限组加载16.577s，16GiB组19.092s。下表GB为十进制；
存储计数覆盖服务全部读取，包含PLE/元数据等，不称“专家SSD字节”。

| host/cache约束 | 次序 | TTFT s | decode token/s | PID read_bytes GB | cg rbytes GB |
|---|---:|---:|---:|---:|---:|
| 不限 | 1 | 135.823 | 9.870 | 27.906 | 27.906 |
| 不限 | 2 | 131.830 | 9.971 | 0.769 | 0.769 |
| 不限 | 3 | 131.570 | 9.821 | 0.174 | 0.174 |
| 16GiB | 1 | 223.080 | 7.280 | 438.850 | 174.392 |
| 16GiB | 2 | 224.114 | 7.253 | 441.757 | 38.862 |
| 16GiB | 3 | 224.331 | 7.312 | 441.614 | 0.223 |

三发汇总：TTFT算术均值133.074→223.841s（+68.2%），decode调和均值
9.887→7.281（−26.4%）。这是固定顺序的局部资源对照，非完整性能
验收；其中后两发不是“暖态三重复”，不推断长期稳态或尾延迟。
两组rchar每发都约1.142–1.144TB，逻辑读取不能替代存储读取。

| 独立观测（峰值不可相加） | 不限组 | 16GiB组 |
|---|---:|---:|
| memcg kernel peak bytes | 70,152,597,504 | 17,179,885,568 |
| NVIDIA driver bytes | 56,237,228,032 | 56,237,228,032 |
| 模型文件cache扫描窗口峰 bytes | 65,341,304,832 | 15,510,540,288 |
| memory.events.max | 0 | 10,410,280 |
| OOM / OOM kill / swap | 0 / 0 / 0 | 0 / 0 / 0 |

受限配置17,179,869,184B，实际kernel峰高16,384B，原样保留。
内核memory.max语义允许暂时越限，故配置值也不等于严格瞬时实测峰；
见[官方memory接口说明](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files)。
CUDA缺charge边界仍在。质量组继承cache时cg峰仅18.126GB，不能把
它与冷态70.153GB的差值解释成节省了完整物理RAM。

### 本轮新发现的计量边界

- 受限组PID read_bytes与cg io.stat严重分歧；不能因第三发cg仅
  0.223GB就宣称几乎不读SSD。分区259:1按请求边界前后样本夹逼，
  首发全分区增量[438.849851392,438.882922496]GB包含PID值；第二发
  分区[441.828883968,441.880460800]GB，比PID多72.35–123.93MB，
  量级一致但不是严格相等或专属归属。第三发分区夹逼
  [441.807130624,441.830604800]GB，比PID多193.16–216.63MB。
  背景设备不与父盘相加。773线程归属检查无逃逸；具体内核
  低计原因UNKNOWN，未用写回文档推断读取因果。
- 口径：PID计数作为服务存储读取主观察，分区边界作为独立交叉
  核对，cg I/O单列且标metric closure PARTIAL；不强行使三个数相等。
- 首发server `nvme_mb`仍包含InitHot启动读取：prev初值为0、仅请求
  结束后更新（chat_generation.cpp:849/902），多约33.974GB。本轮
  使用明确请求前后端点，不用该首发累计值作为请求SSD流量；此既有
  可观测性边界留后续修复，不为日志改动重跑已通过的完整模型请求。
- quality有6次GPU缺测、最长扫描12.666s；不限组无GPU缺测，退出期
  一次/proc/PID/io EACCES；16GiB组也有一次退出期PID io缺测，
  无GPU缺测、最长扫描3.064s。
  PSI不可用，缺口保留。逐文件扫描不是原子快照，不能声称捕获所有
  瞬时物理峰值。各组退出cg可读，辅助sleep与模型PID分开记录。
- 全模型cachestat扫描的中位耗时不限组0.487s、受限组1.806s；上述
  延迟均在监控开启条件下取得，不把差额全归因于未插桩运行时。正式
  性能协议须先固定采样成本与频率，不能把本次仪器开销忽略掉。

### 下一步建议

1. 先明确部署预算合同：54GB整体RAM与16GiB host/cache是不同约束。
   当前C256启动估计约66.63GB（仍含未启用权重估计），不限组单个
   memcg计数已超过54GB；受限组也不能因此签整体54GB通过。
2. 将本轮16GiB条件作为可复现的资源研究基线，采用PID+设备的IO
   交叉口径；暖缓存结果单列。不要继续用旧C3百分比证明同预算性能。
3. 用实际GPU/L2/mirror与forward/sub-chunk时序做有界离线回放，分开
   估计同层重复补载和跨层/跨forward文件cache淘汰的收益上界。当前
   每发约1.14TB逻辑读取值得优先研究prefill重复补载；单凭相邻70%
   overlap不能证明某驱逐策略有效。留出语料不得自行选本组热点。
4. 只在收益上界与内存约束明确后选一项在线策略，保持GEMM冻结、
   Phase D off，再做质量与五档+目标档每档至少3次的正式验收。
   当前没有新策略接受结论，原60% decode/TTFT−30%目标仍未验收。

复核入口：证据根的`analysis-final.json`、`post-run-integrity.json`，
各组`final-resource-audit.json`及受限组`io-accounting-discrepancy.json`。
工具合同与原始失败、质量输出、每发数据库/响应、全部资源采样均保留。

## Goal 实施冻结（2026-10-02 20:03）

用户已显式设置Goal推进。先修预算/采样合同，再统一HTTP，不实施新
性能策略。新工作区binary `46b3f977`（完整SHA见证据）构建零警告；
旧`dc845dd3`和入口diff已封存。预算host及ASan/UBSan各13/13，工具
45/45、既有验收工具30/30通过。真实1%预算启动在model_load前拒绝。
当时固定质量11题运行中；最终结果见上节，不以过程快照替代终判。

预算器现在明确feasible/requested/effective；auto无解不制造1024，
不再将0容量回退原请求。预算与loader共用热点容量规则，空/缺失层
维持全局C；MTP条件项、PLE16heads与页池、KV页取整修正，workspace
调用现有尺寸函数。索引权重仍含未启用的MTP/vision，动态allocator、
加载期host副本等未精确归属；估计与2GB余量不等于物理上限。

实验参数在启动前冻结于`frozen-experiments.json`：

- C=256/hot-final-12288、L2=16、K=8、maxopen200，单流、8192分块、
  容量262144，MTP/Phase D off。运行时改动后重验固定11题HTTP质量。
- 两组同输入45056×3、每发256输出；一组host/cache不限，一组
  `17,179,869,184 bytes`（16GiB），两组swap=0。16GiB只约束memcg
  charge，是探索配置，不是整体RAM上限或54GB目标的替代。
- 每组启动前仅对只读模型fd做定向cache advice，再逐文件cachestat
  确认所有safetensors/bin payload驻留为0；失败保存并停止，不重试
  直到“冷态通过”。tokenizer等metadata可能由组外client重新缓存，
  如实记录，不能宣称整个文件集合均为独占charge。
- 每发使用独立evalscope client，服务连续；前后保存准确PID/组IO与
  内存端点、HTTP时钟/输出；客户端重启间隔与旧C3批次不同。请求间
  不清cache、不改限额。失败不自适应加额度，不将部分矩阵签成验收。
- 服务出生于独立systemd unit；client和monitor在外。小进程测试
  证实RemainAfterExit不保留空cg，已改10s ExecStopPost采尾部；该
  sleep的小额host charge计入unit终点，另保留最后模型存活样本。
  launcher正常0/错误7/超时/清理与退出cg保留合同均通过。

证据根`.q4t-work/offload-budget-goal-20261002/`；前期host合同在
`.q4t-work/offload-budget-20261002/{budget-contracts,resource-tool-tests}/`。
shared parser未解决既有InitHot同批重复专家缺陷；当前热点48层各256
个ID有效且无重复，独立复核确认不影响本轮。GEMM与原Phase D内容未改。

## 本阶段出口

1. 明确预算的字节数、计入范围、覆盖阶段与swap规则；旧68.34GB
   不再作为实测物理上限。当前建议先探索C=256的可运行资源范围，
   54GB是否仍为最终目标待用户选择，不自行放宽或替换目标。
2. 验证CPU、pinned、CUDA及文件缓存分别落在哪些计数/约束内；未
   覆盖的部分明确列出，不把一个配置参数当成完整物理RAM上限。
3. 冻结缓存与实际存储IO协议、准确binary/模型/热点/输入身份。
4. 完成一组有界HTTP探索，再选定预算与候选；性能接受仍需五档
   加目标档矩阵。质量、容量、资源和性能分开判定。

## 首轮校准已否定“直接用memory.max限制全部RAM”

环境：Thor、kernel 6.8.12-1021-tegra、systemd255、cgroup v2。
探针出生于独立system级transient服务；采集器在组外，逐阶段核对
MainPID、ControlGroup和/proc/PID/cgroup。隔离限额1GiB、swap=0，
仅是小探针保护条件，不是模型预算或已证明的物理总上限。

14阶段各3次观察，CPU/CUDA/pinned每种64MiB，两份任务文件各32MiB。
编译-Wall/-Wextra/-Werror零警告，运行exit0，约5.8秒。未触发OOM，
探针、unit/cgroup、两份任务文件均已清理；模型与共享session未改。
本轮未主动触限；limits与events只证明配置/无触限，不证明所有路径
都被限制。memory.pressure/io.pressure不可用，保留unknown而非0。
证据根：`.q4t-work/offload-budget-20261002/probe/`，源码、采集脚本、
binary与原始记录可复核，独立审查在run-01/analysis/summary.json。

| 动作（阶段中位差） | cgroup charge | 其他对应量 | 结论范围 |
|---|---:|---|---|
| CPU匿名64MiB实际写页 | 约+64MiB，另有管理开销 | 释放后回落 | 普通匿名页进入计费 |
| 读取外部预热32MiB文件 | file项+0，current仅少量波动 | read_bytes+0 | 外部已有cache不会自动迁入此组 |
| 新文件驱逐后重读32MiB | current/file各+33,554,432B | PID read_bytes与io.stat rbytes各+33,554,432B | 本组新缓存与真实读IO可观察 |
| CUDA device64MiB分配并触碰 | current中位+0，peak不增 | NVIDIA+67,108,864B，KReclaimable−同量 | 当前driver池路径未完整计入调用方memcg |
| 再分配/触碰pinned64MiB | current+67,141,632B | file与shmem各+67,108,864B | file包含shmem，不能相加 |
| 释放device64MiB | current中位+0 | NVIDIA−64MiB、KReclaimable+64MiB | 与分配阶段一致 |

这证明本机当前路径上仅设memory.max不足以限制全部q4t物理RAM。
不能从一次64MiB增量推广所有CUDA分配或推导完整物理总数。后续会
分别记录设备分配、host/pinned与文件cache约束；完整去重总量未
闭合时仍判INDETERMINATE。不得简单把这些分项峰值相加。

共享页的计费归属及进程迁移不迁移旧charge的规则见
[内核memory ownership文档](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-ownership)。
本轮外部预热/内部新文件对照给出了当前机器的直接证据。

## 执行计划

### 1. 固定基线与预算语义

初始测量继续使用已验证工作区binary dc845dd3，完整SHA和来源见
[前阶段修复报告](MOE_OFFLOAD_REPAIR_2026-10-02.md)，封存副本与来源
后再接新runner。C=256、hot-final-12288、L2=16、K=8、maxopen=200、
max_prefill8192、max_len262144、单流、MTP off、Phase D off。
不因整理协议重复已有身份未变的质量证据。

若后续改为clean2b0100c或新运行时代码，另建完整binary身份并进行
受影响验收，不能把dc845dd3证据改记到新身份。预算必须明确：
确切bytes、设备/host/pinned/可回收文件cache是否计入、加载至请求
切换是否覆盖、是否允许swap、缺测与超限如何处理。

### 2. 补齐约束与工具合同，再启动模型

- 服务出生前进入独立实验组；monitor和client在组外。C3若包含
  probe和矩阵两进程，应放入同一实验父组，保持文件cache计费连续。
- 内存端同时记录实际分配配置、memcg charge/peak/stat/events/PSI、
  NVIDIA/进程分项和逐文件cache。设备分配未受memcg完整覆盖，须
  保留独立覆盖边界；不能将host/cache额度称为整体RAM额度。
- IO端新增准确PID read_bytes、组io.stat及设备背景统计；现有
  nvme_mb/pread仍只称逻辑文件读取。包含PLE/metadata的服务IO也
  不能全叫作专家IO。父盘与分区统计不得相加。
- runner增加显式选档/重复数的有界入口，部分结果永久标记非完整
  矩阵。每请求前后记录边界、实际容量、成功/输出、时间及IO计数。
- 冷态必须逐文件确认残留；不把“调用过清理”当作冷态建立。目标
  文件缓存清理只在独占实验边界执行，按预设规则记录失败；请求间
  不反复清缓存来冒充持续受限场景。

存储计数语义见[proc文档](https://docs.kernel.org/filesystems/proc.html)
和[设备统计](https://docs.kernel.org/block/stat.html)。本机模型位于
nvme0n1p1，io.stat观察到父盘259:0；共享session缺io控制器，不能
拿它作为独立实验组或给它设置限额。

### 3. 先处理会破坏预算实验的具体代码边界

只读审查发现下列问题，尚未修复，按有界清单处理：

- 预算无可行容量返回max_len=0时，服务回退使用原请求容量；auto
  路径无余量仍强制最低容量。应明确不可行并拒绝，记录requested/
  effective容量；任何缩容都不能算原容量的成功。
- 空热点层在预算端按0槽计费，加载端仍分配全局C；应共用解析容量。
- MTP off仍使用默认has_mtp=true及无条件draft预算；workspace/PLE
  也存在旧尺寸估计。先与实际分配函数核对，避免把估算高计或低计
  当作真实占用，或靠改估计值制造“省内存”。

这些是预算/工具合同工作，直接host或最小缺陷测试可先行；改运行时
后再做相应HTTP。新性能策略本阶段不实施，遵守EVALUATION的顺序。

### 4. 区分缓存状态，有界探索后统一验收

| 状态 | 固定流程 | 可支持的结论 |
|---|---|---|
| 冷启动 | 验证初始cache→加载→首发；无生成预热 | 加载与首发成本；加载本身也会暖cache |
| 同服务转暖 | 同输入连续第2/3发，不重启 | 该请求序列的转暖变化 |
| 历史C3 | 45K×8 token probe→退出→新服务矩阵 | 暖文件cache、冷服务，保留历史对照口径 |
| 持续受限 | 约束贯穿加载与全部请求，不中途清cache/改额度 | 指定资源与时间窗口内是否持续读存储 |

首组建议45056输入×3、每次256输出，保存完整HTTP/缓存/IO时间线；
三次分别观察，不能把其中两次暖请求说成暖态三重复验收。随后只选
一个事先冻结的host/cache约束配置，再做同样45056×3。当前不擅定
该额度，更不把它当全部物理RAM上限。固定超时/失败出口，失败保留，
不自动加额度反复试到成功。工具合同先成组验证，性能实验以HTTP开始。

身份、约束和缓存协议都明确且选定单个候选后，才做五档+目标档，
每档至少3次。C=0若不能放入同预算，只作资源不同的全驻留参考；
受限优化的公平基线应为同预算、同协议的上一候选。三次观察不能
证明长期稳态或稳定尾延迟。
