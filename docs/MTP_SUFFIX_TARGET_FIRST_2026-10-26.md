# 固定k3 suffix：报告绑定后目标优先退出

本阶段CPU修复通过，GPU阶段在首8K OFF清理时ENV_ABORT；没有有效GPU配对。
工程决定保持PERFORMANCE_NO_GO_UNCHANGED，默认程序及发布元数据不变。

旧日志证明报告绑定后shutdown与模型process-exit flush重叠，随后出现连接
shared-pointer/ProfilerApi错误；这是可检验的重叠原因假设，未证明内部唯一根因。
固定Nsight 2026.5.1文档SHA `f45d5e5fb4823cb5d339e917f005d418384ca08d9befe8999100f67a98c08a1e`
及[官方说明](https://docs.nvidia.com/nsight-systems/UserGuide/index.html)表明
shutdown断开CLI与应用、取消活动采集；命令返回本身不是应用注入线程已退出的证明。

仅修改已绑定报告后的清理顺序为目标退出/独立收据→shutdown；未绑定失败仍
先取消。Nsight参数、六条受限错误规则、运行时与数据不变。24项CPU首suite
通过，0.326371秒。事前冻结7200秒工作、4200秒GPU、4启动/6HTTP/4capture、
单步900秒/清理60秒、0重试/额外预热/fixture；首失败停止后续组。

唯一阶段81.084873秒，首8K HTTP输入8170/输出256，输出SHA
`66bb6d8c428668a991608e634c39eabebd85918b0cdd400c7b22919214ad436e`，
74 T4 verify、148 neural T1、74 extend/252行、22 restore符合原合同。
报告SHA `484888610ee63acb8db224a245860e97168aacb416cc7f6c1803f1ccbe566c37`。
实际目标退出收据1194878.691610早于shutdown开始1194878.755052，目标与
launcher均rc0、没有强杀。shutdown rc1，完整输出“There are no active sessions.”。
现有控制器将此判作失败，后续日志封存被取消；不能宣称本轮修复了旧四条错误。

原state STOPPED、guard COMMAND_FAILED、postflight ENV_ABORT保持。0导出、
0分析、0有效配对；压缩原件没有EOF/尾帧/逻辑SHA收据，只保存现有字节。
监控82/82落盘、pending0、无监控失败；记录的自有身份终审无活动，锁释放。
轮询不能排除采样间隔内的干扰。全机MemTotal−MemAvailable峰113420873728
字节、swap峰2147479552字节，PSI三源不可用；这不是模型增量RAM。

证据保存完整性PASS（24178检查，2.615203秒），测量有效性
ENV_ABORT_CLEANUP_CONTROL_REJECTED，测量不完整，统计
NOT_EVALUATED_INCOMPLETE_PAIRS。527文件75270882字节独立增量归档，manifest
`b039a98848c11be2787d74c12f3465ad56f2afa8da010ad291c9379cd3735eb3`；
[证据索引](evidence/mtp-suffix-target-first-20261026/index.json)绑定原始路径和SHA。
归档依赖既有工具包与原路径，不是发布回退包。

两head重复收益、正确性/质量直接复用；原250 HTTP/18启动五档已测齐但保护
未通过，见[完成矩阵](evidence/mtp-suffix-contract-20261026/completion-matrix.md)。
完整目标未完成。下一步只修已绑定报告、目标独立正常退出、已知进程回收完整
条件下的自动结束终态分类，保留原始非零返回码，其他失败继续ENV_ABORT。
