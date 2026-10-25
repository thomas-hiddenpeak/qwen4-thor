# 回调身份修复与固定GEMM机制对照

本阶段局部机制有效；完整k3 suffix交付仍未通过。原五档250HTTP已测齐、
保护决定仍为NO_GO_PROTECTION_NOT_CLEARED。两head重复收益、未变正确性
与质量/资源证据按原身份复用。本轮不改推理或默认程序，不将工具通过
替代200K/8K的GPU对照及正常HTTP性能保护。

## 具体缺陷与冻结范围

原日志首失败：同一cudaStreamCreateWithFlags内六个顺序driver回调共用
correlation119，旧解析器错误要求domain/correlation在全日志唯一。
此外，探针callback记录SYS_gettid，CUPTI默认activity线程字段在Linux用
pthread_self（已冻结cupti_activity.h:11166–11182）；两者不能直接数值比较。

现以原producer记录的enter_seq、PID/TID/domain/CBID及严格栈匹配回调，
仅允许同一已核runtime父内顺序、不重叠的driver共享父correlation。
API活动须通过唯一PID/domain/correlation/CBID/返回值对应，再建立双射
线程映射；映射明确标注为推导，未直接采pthread值。歧义、非双射、
跨父、重叠、缺EXIT和重复活动仍拒绝；GPU活动仍要求唯一API归属。

新scope事前SHA `0406cfc176edecbde729530e8af5f86f442b0d119b98b361e6535bd07089e557`。
工作1800秒、CPU≤24方法/120秒（最多两suite，只因具体失败修复）；
单次原日志离线分析≤60秒；0构建/0新CUPTI，有效代表复现后唯一Nsight
阶段≤600秒、采集导出≤300秒，清理预留180秒。0模型/HTTP，无参数或
版本扫描。原失败和原raw均不覆盖。

## 已执行证据

23 CPU（analyzer16/stage7）首suite通过，0.325263秒、416源无漂移、无临时
残留。未变runner19/synchronous15/snapshot8共42方法明确复用。
CPU proof SHA `6e38954edabd5cf82348531c08212e7a83daf5c48b1b9eb93963959d0190a3b0`。

唯一离线分析0.053997秒，922回调对、832API活动，5组合法共享driver
correlation；OS3430351↔CUPTI2367240384由832个唯一API关联推导。
4固定输出逐元素精确；drops/overflow/errors均0。7copy包含3次初始化
及4次读回，另有2kernel/1memset。原始0/512字节控制和首次/缓存生产
GEMM的memset参数分别为0/512/0/0；正控制GPU确有512字节，其他三项
无memset活动。32MiB是可用workspace容量，并非实际memset字节数。
分析SHA `53c5441c066adee5aa9f9d77d61f89b7a829177b1d8d7affc7aaa26165363664`。

唯一Nsight阶段10.472697秒正常结束：同一binary、M4/N48/K2560/32MiB、
算法raw/key属性及代表WMMA内核对应，505API/2kernel/4读回copy/1正控制
memset，5个预期NVTX区间齐全。实际target3433839；与CUPTI进程3430351
不混接correlation。Nsight没有每次memset参数，明确保持UNAVAILABLE，
不从另一个进程反填。判定LOCAL_MECHANISM_OBSERVED，非模型完整性通过。

plan SHA `34975398c0117dcf5c91343705ac904c795ffd76f4c98b42c16c3481adf1fe31`；
SQLite SHA `7018da3f7cf8fd1acbe1599e6b6c1d7d30dbe1ccbb474bc3ac83c5a880538fa1`；
report SHA `6da42dc2c2044751132070f49caa4abf6ab323b94dc2c76d69e35967cd9fa06d`。
探针显式owned27,279,360字节+配置CUPTI池5,242,880字节≤64MiB；
CUDA/CUPTI/cuBLAS不透明分配不在此硬上界，不能将其称总RAM。

## 警告及下一步边界

实际目标CUDA/NVTX通用完整性warning仍保留。新NVLOG中producer确有
ReportCUDADone和NVTXFinish；consumer也读到两类profiling finished服务
标记，却因其转换后时间晚于session stop而过滤。此为具体收尾边界证据，
支持warning来自终止标记被过滤的解释；不能证明任意模型所有工作完整。
531 produced与512 collected的计数口径不能直接相减为丢失19事件。

本固定GEMM的零字节解释成立，不能反填旧模型6381次缺活动API的参数。
下一步须有限取得实际8K模型调用的参数账本，再按已冻结数据与配置补
200K/8K GPU对照；没有因果依据不改suffix或扫准入参数。任何新运行时
候选仍按HTTP优先、适用五档、质量/TTFT/普通decode及统一RAM保护验收。
只有满足条件才替换程序和发布元数据，完整目标仍active。

## 阶段收尾

独立终审核2963输入及1765工具文件一致，11次周期monitor加1次preflight/
12次已存查询无冲突，20个已记录PID均已退出、锁释放。38原始文件封存；
最终NVLOG1015207字节、目标515行与driver快照相同、无追加，原manifest
和收据不改。首次两次独审分别在SQLite SHA字段名、注入库间接manifest
表示处停止；修正明确的审计适配后在原180秒内完成，失败源/收据均保留，
无重跑analyzer或GPU。最终审计1.554秒、含归档2.168秒；原两个失败不覆盖。

1145文件/38709468字节增量归档、2299外部路径/SHA依赖，非自包含；seal
`b33862096924dbb2d9fa2cc13e0a92ab7aa17081dff2434ef17aac05393aa289`。
证据有效性VALID_LOCAL_MECHANISM_ONLY；测量完整性仅局部诊断true；统计结论
NOT_EVALUATED_DIAGNOSTIC_ONLY；工程决定LOCAL_MECHANISM_OBSERVED_NO_MODEL_OR_PERFORMANCE_ACCEPTANCE，
模型资格、
全trace完整性及运行时性能接受均false。默认程序f356b2c9、sidecar422993b0
和子集manifest b63b0f25保持，无部署/健康检查需求。
小收据见[evidence目录](evidence/mtp-suffix-callback-pairing-20261026/sources.json)，
原raw与增量包保留在`.q4t-work/suffix-callback-pairing-20261026`原路径。
