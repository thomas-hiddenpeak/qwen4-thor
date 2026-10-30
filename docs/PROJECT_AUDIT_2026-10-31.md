# 项目全面审计（2026-10-31）

审计锚点：main `c26e97a3549965a7a52b6928880153e7c64b030c`。
任务类型是代码、可靠性、验证与交付审计，未实施运行时修复或性能优化。

本轮确认 **8项P2问题**，涉及权重读取、SSD专家驻留、容量配置、发布
监督和测试工具；另纠正1组P3文档身份漂移。没有发现足以撤销当前默认
版本既有验收的证据，也不能把本轮CPU通过解释为全模型数值或质量通过。
全部运行时问题仍待修复，优先级与触发条件见下文。

## 身份与审计边界

- 默认程序：`037328bb5c8788920ef44d1dfac2df94f908760f5626a95f1a674568c0dac4f7`。
- 默认sidecar：`3200266ea8778507b9e9acdfc0cf0a8b2bb97ae02aa8262cd572fd0a9b060d06`。
- 实际构建源码：`9b9931193ff8a9abe9107a6d700dc2a8728a8464`。
- 发布清单的169项运行时文件与当前工作区全部一致。二进制、源码清单、
  构建缓存、接受合同、决定与审计收据的声明SHA256均核对一致。
- 前版程序`27a483a3`与前版sidecar`af6b3a44`匹配，可核验路径仍存在。
  本轮未执行回退、安装或启动常驻服务。
- 冻结了1537项源码/工具/测试文件身份；这是可追溯清单，不是逐行审查
  或测试覆盖率声明。未修改模型、reference、历史证据或其他工作区。

覆盖构建与主机门禁、HTTP输入/输出、请求取消与调度、会话与公共缓存、
模型/MTP状态、PLE与专家SSD读取、统一RAM预算、分词、实验媒体入口、
发布与监控工具。模型算子/MTP采用关键路径检查和未变依赖身份核对，
未逐个重证全部CUDA内核。

**新增真实模型HTTP、GPU数值、profile、性能bench均为0。** CPU HTTP
生命周期测试使用实际socket和模拟后端；专家复现链接CPU CUDA替身，
没有链接CUDA运行时库。真实tokenizer检查仅访问只读分词文件。
已有37HTTP/12启动及其继承证据继续受原接受范围约束，未被扩大。

证据入口：[归档说明](evidence/project-audit-20261031/README.md)、
[验证汇总](evidence/project-audit-20261031/validation-summary.json)、
[发布身份核对](evidence/project-audit-20261031/delivery-identity.json)、
[运行时与回退身份](evidence/project-audit-20261031/runtime-identity.json)。
完整构建和临时文件位于`.q4t-work/audit-20261031/`。
封存附带可执行CPU重放入口，在独立新目录验证一次，四个探针结果
均与原观察一致；原失败记录保留。

## 发现

P2表示需要修复的有条件正确性、资源或验证问题；P3表示维护问题。
排序优先考虑静默错误，再考虑失败回收和操作可靠性。

### A1 · P2 · Safetensors未统一验证张量区间与整数溢出

位置：[ReadTensor](../src/io/safetensors.cpp#L219)、
[ReadRange](../src/io/safetensors.cpp#L247)、
[Open](../src/io/safetensors.cpp#L143)。

`ReadTensor`按shape计算读取长度，却只用`data_end`判断文件边界，没有
要求`data_end - data_start == byte_size()`。合成文件中a声明shape=[4]、
区间[0,1]，b占[1,4]，读取a仍成功返回整个`ABCD`，把相邻张量字节纳入。
`ReadRange`的加法边界检查还可被uint64溢出绕过，成功读到文件头。
header_len=`UINT64_MAX`也绕过`8 + header_len`判断，抛出未转为Status的
`std::length_error`。

影响损坏/替换检查点和直接IO调用。支持的默认启动计划会额外校验已列
张量的shape和extent，缩小普通启动暴露面；这不是底层IO合同的替代，
也无法保护更早发生的头部解析。没有证据显示当前模型文件存在该错误。

建议在Open统一验证非负整数维度、checked乘法、offset顺序/范围和
字节数一致性；读API用减法边界或checked加法，并明确off_t上限。

证据：[生产IO复现源码](evidence/project-audit-20261031/io_probe.cpp)、
[结果](evidence/project-audit-20261031/io-probe.log)。

### A2 · P2 · 专家连续读取快路径缺少同分片检查

位置：[描述符构造](../src/quant/moe_residency.cpp#L291)、
[按单分片读取](../src/quant/moe_residency.cpp#L745)。

`d.fast`只检查张量大小与相邻offset，随后把down.weight的分片用作
全部weight/scale读取来源；`sc_fast`也未验证标量与该分片相同。
offset属于各自分片的坐标，跨文件数值相邻不能证明物理连续。

合成合法布局将gate/up放在另一分片，保留满足快路径的offset。实际
`MoEResidency::Init`和`InitHot`均返回成功，gate首字节却是161而非34，
up是162而非51。此复现使用实际WeightLoader与驻留代码，仅把CUDA分配、
复制、事件替换为CPU实现，无GPU计算或模型权重读取。

只读检查当前索引的24,576个专家组，跨分片组为0；因此未证明当前固定
检查点受影响。重新分片、转换工具或其他同架构合法布局会触发此缺陷。
建议所有被合并读取的张量必须具有相同ShardOf，任一不同即退回逐张量
路径；标量块须独立验证。

证据：[生产驻留复现](evidence/project-audit-20261031/residency_probe.cpp)、
[错误字节结果](evidence/project-audit-20261031/residency-probe.log)、
[当前检查点布局](evidence/project-audit-20261031/expert-shard-layout.json)。

### A3 · P2 · Safetensors头部解析失败泄漏fd和映射

位置：[Open错误返回](../src/io/safetensors.cpp#L152)、
[成功对象的析构](../src/io/safetensors.cpp#L87)。

mmap成功后，多处错误分支直接`delete impl`；Impl没有负责fd/map的
析构器，而资源释放仅位于尚未构造的SafetensorsFile析构函数。
12次打开非法JSON合成文件后，进程fd实际增加12个；映射泄漏由同一
未执行munmap的路径可确认，本轮未单独计量其物理驻留量。

启动最终退出进程时OS会回收，不能据此认为函数失败回收正确；进程内
重复打开坏分片会持续耗用资源。建议把fd/map所有权放入RAII对象，覆盖
Status失败和分配/解析异常。不要仅在已知错误分支手工补close。

证据：[IO复现结果](evidence/project-audit-20261031/io-probe.log)。

### A4 · P2 · MoE驻留初始化失败后Free跳过已有资源

位置：[逐项分配与早退](../src/quant/moe_residency.cpp#L200)、
[Free入口](../src/quant/moe_residency.cpp#L1705)。

Init在多个分配成功后仍可失败，`inited_`只在末尾设置。Free首先检查
`!inited_`并返回，导致失败对象无法通过公开清理API释放部分资源。
在CPU分配替身中令第二次cudaMalloc失败，Init返回失败，随后调用Free，
第一次成功分配仍保留。正常初始化/Free对照剩余分配为0。

受影响的是SSD驻留模式的启动失败与进程内重试。进程退出可能掩盖泄漏，
不能把现有正常启动、运行时payload故障回收通过推广为部分初始化安全。
建议Init使用事务式RAII回滚，或让Free按各资源实际所有权清理；线程创建
也要能逐个停止/join，避免依赖完整初始化标志。

证据：[驻留失败与正常对照](evidence/project-audit-20261031/residency-probe.log)。

### A5 · P2 · 普通decode接受超过forward行容量的并发配置

位置：[预算支持条件](../src/runtime/memory_budget.cpp#L227)、
[启动计划auto回退](../src/server/chat_server.cpp#L365)、
[decode硬限制](../src/model/model.cu#L1192)。

`--no-mtp --max-prefill 1 --max-seq 2 --max-len 8192`通过参数检查和
内存预算，得到max_seq=2、forward行数=1。只有MTP预算显式约束并发
验证行数；启动计划发现不支持时，默认auto模式会回退旧分配路径。

普通调度按所有active请求组成批次，最终ModelDecodeBatchMulti明确
拒绝B>max_prefill。因而可启动的配置在两请求同时decode时会失败。
CPU复现证明参数/预算矛盾，调度至失败的链路由源码确认；本轮没有
启动真实模型复现两请求HTTP。当前已交付的1槽/8192及4槽/4096配置
满足该关系，不受此配置边界影响。

建议在有效配置确定后统一强制`max_seq <= max_prefill`，使参数、
自动预算和启动计划的接受范围一致，并补普通decode边界合同。

证据：[配置探针](evidence/project-audit-20261031/capacity_probe.cpp)、
[实际接受结果](evidence/project-audit-20261031/capacity-probe.log)。

### A6 · P2 · 发布监督器误把结束时统计当作实时生成进度

位置：[supervisor看门狗](../tools/release/release.py#L264)、
[指标更新点](../src/server/chat_generation.cpp#L1627)、
[请求deadline范围](../src/server/chat_generation.cpp#L179)。

supervisor以`q4t_generation_tokens_total`不变1260秒判定执行停滞，
但生产者在生成循环结束后才一次增加该计数。当前API允许调用者设置
最长10800000ms，超过1260秒的正常长请求可能持续输出token，却仍被
监督器强杀。旧text-v1的20分钟上限已不能保护当前API的显式长deadline。

使用实际supervise函数、健康200/请求未结束的指标快照及可控时钟，
在1261秒记录到backend.kill；全部子进程与网络为替身，未实际等待
21分钟或启动服务。触发范围是使用这个发布监督器且实际运行超过
21分钟的请求，直接运行`q4t serve`没有这层看门狗。

建议改用真实执行进度信号并覆盖prefill/排队/生成阶段，或明确限制
发布profile的请求deadline。不能仅把阈值加大后宣称已检测GPU停滞。

证据：[监督器复现](evidence/project-audit-20261031/watchdog_probe.py)、
[结果](evidence/project-audit-20261031/watchdog-probe.log)。

### A7 · P2 · 独立IO合同入口无法链接

位置：[tests/io_host/CMakeLists.txt](../tests/io_host/CMakeLists.txt#L15)。

同一q4t_io_contracts可执行文件同时链接io_read_contract_test.cpp与
weight_loader_contract_test.cpp，两者各自定义全局`__wrap_pread`和
`__wrap_close`，产生multiple definition错误。该入口在本机g++14
干净构建可稳定复现，当前公共host工作流没有覆盖此独立入口。

将两组现有源码分别临时链接执行后，7项和6项均通过；这只证明测试
本身可执行，不能把原CMake入口记为通过。建议拆成两个CTest目标，
或抽取能共享且保持各组隔离语义的syscall替身。

证据：[原失败日志](evidence/project-audit-20261031/io-build.log)、
[拆分命令与退出码](evidence/project-audit-20261031/split-io-results.json)。

### A8 · P2 · GPU guard未前置检查pidfd能力

位置：[signal_identity](../tools/evalscope/gpu_exclusive.py#L119)、
[启动子进程](../tools/evalscope/gpu_exclusive.py#L318)。

本机CMake自动选中uv Python3.13.14，其os没有pidfd_open。公共CTest
20组中的guard组因此失败：停止脱离进程组的自有子进程时抛AttributeError，
还出现“subprocess is still running”警告。guard在Popen前没有验证
`os.pidfd_open`和`signal.pidfd_send_signal`的可用性。

实际guard若遇到相同环境，会在清理阶段记ENV_ABORT/cleanup_error，
但内部持有的锁仍随后释放，无法完成已识别后代的清理保证。普通退出
未必触发此路径，不能以一次正常命令通过证明解释器兼容。

换用`/usr/bin/python3`（3.12.3，两个API均存在）定向执行原27项合同
全部通过，初始失败保留。建议启动前明确拒绝不支持的解释器，并在
公共构建入口记录/选定支持解释器；不退回存在PID复用竞态的裸kill。

证据：[初始CTest](evidence/project-audit-20261031/host-ctest.log)、
[解释器能力](evidence/project-audit-20261031/environment.json)、
[系统Python定向结果](evidence/project-audit-20261031/guard-system-python.log)。

### A9 · P3 · 当前状态与发布指南夹杂过期身份（本轮已更正文档）

审计开始时STATUS顶部是037328bb，但后面的“当前交付”和“已接受生产
基线”仍以90f24f49为当前；release README还写MTP默认关闭/sequential、
快速T4实验和必需host检查30项。当前代码默认MTP k3，发布清单为72项。

本轮统一当前身份入口，标明旧阶段记录；发布指南修正默认行为和清单
数量，明确text-v1冻结合同不能作为当前长deadline/API的接受声明。
历史日志、原阶段结果和原验收数字保留。

证据：[修改前摘录](evidence/project-audit-20261031/documentation-drift.txt)。

## 验证结果

| 检查 | 结果与限制 |
|---|---|
| Thor独立Release全量构建 | 通过；g++14/CUDA13.3/C++23/实际生成SM110a；无warning/error；构建了bench目标但未运行 |
| 公共host CTest | 初始19/20组通过；唯一失败为A8；原配置的全套结果仍记失败 |
| guard定向重验 | 系统Python下27/27通过，仅修正解释器前提 |
| Thor发布host清单 | 72/72通过，包括真实tokenizer；无SKIP |
| 测试框架反例 | 16/16通过 |
| HTTP生命周期 | 4/4通过；CPU模拟后端，无模型推理 |
| 独立IO入口 | 链接失败A7；临时拆分后7/7及6/6通过 |
| 私有/公共SSD存储 | 私有49检查；公共入口复跑私有49并增加公共38检查；均通过，实际O_DIRECT合成文件 |
| 显式公共种子 | 7项通过 |
| 私有增量存储 | 基础55＋增量137检查通过；CPU字节oracle和短写/取消/损坏路径 |
| 15组离线Python合同 | 共145项；首次144通过、1项因未提供native worker失败；补构建并设置Q4T_NVLOG_STREAM_BINARY后只重验相关6项，全通过 |
| 新缺陷复现 | 4个CPU探针覆盖A1～A6；A5/A6的真实长请求/并发HTTP链路未重跑 |
| 发布与回退身份 | 默认二进制/sidecar/声明依赖、169源码、前版程序与sidecar一致 |

各组之间有重复检查，不把上述数字相加当成独立覆盖数。原失败与修正
环境后的结果分别归档。native worker缺失是这次审计调用的前提遗漏，
不是新增生产缺陷；没有因为该失败重复其他已通过测试。

## 后续处置与未覆盖边界

建议分三个修复边界推进：

1. 权重/驻留正确性与资源所有权：A1～A4一起建立合成异常元数据、
   跨分片字节对照和逐分配失败合同，再做受影响真实加载/HTTP验证。
2. 服务配置与执行监督：A5～A6先补CPU容量和生产者/消费者指标合同，
   再按修复范围冻结有界并发与长deadline HTTP验收。
3. 验证入口：A7～A8修复后只重验独立IO和guard相关入口，保留本轮
   初始失败。文档A9已修正，不触发推理重验。

这是一份审计交付，不是上述修复的接受决定。没有性能优化结论；后续
若包含性能优化仍必须执行HTTP E2E优先和适用五档规则。

仍未由本轮证明：全体GPU算子数值、真实CUDA分配失败恢复、持续SSD供给
下的严格整机RAM上限、实验媒体质量、长期负载/SLO，以及检查点所有
权重字节的摘要一致性。统一RAM中的L2、mirror、staging和页缓存继续
计入RAM；既有SSD合同和历史NO_GO/统计保留均保持，不恢复CPU容量卸载
路线，也不因本轮审计自动重开已接受的历史性能阶段。
