# 固定参数探针：生产链接依赖修复

源入口dd6a7af。上轮四对象成功编译，但gemv.o引用的HcInjectGev没有定义；
生产CMake已将gemv_hc_inject.cu与gemv.cu一起编译。本轮只补该原生产
源/对象及第五个.d，不改模型、M4/N48/K2560/32MiB、两次GEMM、两个
memset控制、收尾选项、CUPTI/Nsight版本或判断规则。

新scope在执行前冻结，SHA
`b8c29a40c995e082c88cb0381a89a9b7aba3a02e0c64dc9d31b5a64ecc4dd437`。
工作墙钟1800秒，唯一构建及依赖取证≤300秒；不追加构建。
CPU55按原proof `dfe7aae9e80e9e3f712977042cf57f6127b900e496b299d461a1d9f4734a1cfd`
复用，414源码/输入及6运行收据逐项身份不变；新wrapper只选独立阶段
输出目录。C++格式化候选不在CPU证明范围，必须单独取得新构建身份。

通过构建及身份准备后，完整共享GPU lease一次≤600秒：CUPTI-only
120秒，控制/参数/活动/输出有效且代表路径复现后才有条件Nsight含
导出300秒，收尾预留180秒。最多两进程，0模型/HTTP；无重试、形状
或工具扫描。失败/未复现保留现场；环境或监控/自有清理失败ENV_ABORT。
两进程correlation ID不混接，零字节解释不反填旧6381调用，不授予
模型完整性或性能资格。

实际目标中的CUDA/NVTX警告和长模型memset缺项仍须分别解释。此探针
是通往200K/8K GPU归因的有限机制工作；完整head/子集重复收益和直接
正确性证据按未变身份复用，默认f356b2c9、sidecar422993b0及子集manifest
b63b0f25保持。完整交付目标active，原运行时保护NO_GO不变。

本阶段实际结果：唯一构建及依赖取证11.551410861秒，98个实际依赖含原HC
源/5个.d，零warning。binary SHA
`16b3303c505ea9fabe82e3289f6f647dbfd533b13da4b049d4a7aa035fec63b3`。
plan SHA `79d753afcf59bd074917803deada4ce193c6be9129b5af6ac404e8ca07412ab2`。
唯一CUPTI阶段4.555056秒，4固定输出精确、正常释放，无强停或环境失败；
原分析`INVALID: duplicate/bad callback ENTER`，因此0Nsight/模型/HTTP。
源日志929500字节/SHA `3cc033f1437d8cedc48a4b2dc7694ab2cf6411bdda2648aa9c295514589a2425`。

独立终审2940输入/1765工具文件一致，7/7既有监控无冲突，原PGID无成员、
锁已释放。两个scope间接原件未入plan.files，已单独按scope中的SHA核对
并归档；该限制原样记录。774文件/53956074字节增量包，seal
`12fe481116b0097db68cfa1c3dd8ab81519b3f54e970c78d3fb4a94b6a7853dc`；
2196外部依赖仅SHA锚定，非自包含。原INVALID不被后续解析修复覆盖。

后续仅用原日志修复具体回调关系，结果见
[回调身份修复与有限机制结论](MTP_SUFFIX_CALLBACK_PAIRING_2026-10-26.md)。
