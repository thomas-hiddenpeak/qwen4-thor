# 固定生产 GEMM 的 memset 参数诊断

本阶段针对已记录的6381次成功API缺活动问题，只查一个有源码依据的
代表形状，不把缺项先判为零字节或真实丢失。生产Bf16Gemm保持原样，
固定M4/N48/K2560、32MiB workspace、首个heuristic、禁split-K，依次
首次调用和一次cache命中。额外固定count0/512两控制及独立输出核对。

先用独立CUPTI-only进程取得真实参数、返回值及活动。仅控制、输出、
无丢失/溢出且内部memset/kernel模式复现后，运行同一probe的Nsight
模式一次。后者不启用自有subscriber，避免r595单订阅冲突。两个进程
的correlation ID不得拼接；对照只核固定配置、算法与kernel名称。
警告保留，结论只覆盖代表调用，不追认旧6381或N128/prefill，也不
放宽模型完整性门。无模型加载、HTTP或吞吐接受结论。

同时修复实际观察到的日志收据缺陷：早期NVLOG固定前缀保存为独立
不可变快照，最终daemon日志另存；核对前缀、目标字段和新增错误，
不再把早期整文件SHA当最终SHA。旧FAIL/INVALID原件保持。

冻结scope为`.q4t-work/suffix-memset-parameter-20261026/scope.json`，SHA
`38500bf8dc4f32433d2bf1785048e0efc47eeff5c4ab1677c9409b3b1073b663`。
工作墙钟1800秒；构建最多两次/300秒，第二次仅修具体编译错误或警告；
CPU最多64方法/120秒，首suite与仅具体失败后的修复suite共用预算。
唯一完整lease600秒：CUPTI-only一次120秒、条件Nsight含导出一次300秒、
收尾预留180秒。不重试、扫形状或换工具版本。未复现/缺参数/输出错误/
丢失/溢出/预算耗尽均停止；竞争、监控或自有清理失败ENV_ABORT。

当前源入口39512f3；默认f356b2c9、sidecar422993b0、subset manifest
b63b0f25及两head重复收益证据保持。CPU/GPU前另冻结实际源码、探针、
依赖和工具完整身份。完整交付目标仍未完成，最终需回到子集200K/8K
归因及性能保护；本阶段工具机制结果不能替代这些验收项。

## 本轮结果：构建额度用尽，未启动 GPU

55项CPU首suite全通过：runner19、同步收据15、日志快照8、机制分析8、
阶段驱动5；0.675325秒，414源前后SHA一致，零失败/错误/跳过和临时残留。
不可变NVLOG快照模块及新机制驱动完成代码和CPU验证；真实采集尚未验证，
不能称旧模型采集路径已修复。实际目标身份、清理异常ENV_ABORT及分析
INVALID不转成功的失败路径纳入测试，未重跑无关推理证据。

两次必要构建累计10.698530秒（冻结/编译/链接失败窗口），零warning。
首轮cupti.h:88已包含无include guard的generated CUDA元数据头，重复直接
包含导致类型重定义；移除第二次include后，四对象编译成功，最终链接
失败：gemv.o引用HcInjectGev，但独立探针脚本漏了生产gemv_hc_inject.cu。
四个.d共97项实际依赖均属于事前冻结集合，前后SHA一致；.d不证明对象
链接闭包。没有生成可运行探针，不追加第三次构建或进入GPU阶段。

已只读核清下一修正：生产CMake本来包含gemv_hc_inject.cu，其源SHA为
`171d3af4f3e47ef38cca206bf1ece47414e62bfe5669e4769f15376884d30c98`。
只需按同COMMON编译新增对象/.d并加入link，冻结清单同步加入原源并核
五个.d；不使用stub、不改GEMM参数。当前只记录方案，未应用或再构建。

| 决策字段 | 结论 |
|---|---|
| 证据有效性 | VALID_BOUNDED_CPU_AND_BUILD_FAILURE；CPU55及编译失败原件有效，无GPU环境阶段 |
| 测量完整性 | CPU完成；链接未完成，CUPTI/Nsight/模型/HTTP均0 |
| 统计结论 | NOT_EVALUATED_DIAGNOSTIC_ONLY；没有新的memset参数或性能结论 |
| 工程决定 | NOT_ACCEPTED_NO_LINKED_PROBE_OR_GPU_OBSERVATION；保留默认、原运行时NO_GO和两head收益证据 |

完整目标继续active；后续只能以修正后的依赖身份进行另行有界构建及
必要验证，不能把本轮未使用的GPU预算说成已测。详见
[冻结、失败与待修记录](evidence/mtp-suffix-memset-parameter-20261026/README.md)。

574条目/32579684字节独立副本回读通过，seal `07ef90333476`；非自包含，
继续保留父归档及原路径。归档后按项目clang-format整理候选C++源码，
仅格式变化；本轮失败编译所用源码已独立保留，当前格式化候选未再
编译，前后SHA见post-archive-format.json。无部署、健康HTTP或额外GPU。
