# 请求级诊断审计与有界交付

状态：验收通过并已部署3eb9b2a0，任务收尾完成。审计基线484d749。
用户授权只补实际缺失的关联与等待原因，按非性能任务验收并直接收尾。

## 审计发现与冻结范围

已有请求ID、取消原因日志、/metrics聚合、内存快照和可选router trace。
缺少成功/失败统一终态、seq槽复用的请求生命周期关联、按请求区分
slot/model mutex/tokenizer mutex/scheduler completion/stream write耗时。
现有queue_seconds统计arrival到slot acquired，包含验证和准备，
不能当成纯槽等待；保持旧指标口径，新增独立slot_wait_ms。

本次只在HandleChat添加轻量诊断：每个handler独立instance，合法
request_id注册后关联，slot号分配/释放记录；validation、slot_admission、
encode、prefill、decode、response阶段与单调时间，RAII一次终态。
不记录prompt、生成文本、cancel_token；注册前错误使用unregistered，
不把未校验用户输入写入日志。instance为进程内序号，跨启动结合日志
文件/服务身份识别。每请求最多9行，不逐token输出。

等待桶精确含义：slot为AllocSeqId调用（含锁/检查）；model/tokenizer
为handler获取对应mutex；scheduler_completion为提交/条件等待至结果
完成，包含排队、CPU调度、GPU执行和唤醒，不冒充纯排队或GPU时间；
stream_write为流式输出调用时间（包含取消检查），非所有HTTP写入。
桶可能嵌套，不相加为总耗时。没有增加CUDA同步或修改调度策略。
未区分scheduler内部的锁等待、PLE I/O和kernel时间；这些仍需已有
专用诊断，不用host时间虚构设备归因。HTTP解析前拒绝不在HandleChat
范围；非流式成功仅表示现有SendSimple路径完成，不保证客户端收取。

## 有界验收与收尾预算

零警告构建；新增2项日志/ID复用/等待累计host合同进入必跑清单。
同一GPU lease内顺序验证：53项host；普通/MTP质量各3条，分别
1024、8192、45056-token，复用同模式已验收文本/长度；普通模式
额外一个服务验证成功/注册前后错误/排队取消与ID复用诊断闭合。
不运行新的数值kernel矩阵或性能矩阵，模型实现身份未变证据复用。
只有具体失败修复后重验受影响项，不因小幅波动追加测量。
通过后备份默认binary/sidecar/cache，部署并以默认MTP和普通模式
各1条1024-token健康请求收尾。默认不会提前替换。

## 验收结果

候选3eb9b2a0，完整身份见[审计](evidence/request-diagnostics-20261020/audit.json)。
53项host无跳过；普通/MTP各3条HTTP输出文本/长度一致，响应ID与
新终态日志逐项对应，全部slot释放，两模式均实际覆盖scheduler等待。
普通额外服务7个handler覆盖非法JSON、注册后超容量错误、长请求、
排队显式取消、排队deadline、外部ID复用及非流式成功；每handler
恰好一条end、时间单调、最多9行。取消ID复用分别显示cancelled和
success且instance不同，日志无cancel_token；3个服务均exit0。

单一NVML lease约218.89秒，guard COMPLETED/child0，258文件
封存复核，一次通过，无重试、无新增性能矩阵。GPU模型源码未改，
复用已有数值合同；本轮重点证明请求诊断与真实HTTP行为。
[历史同夹具计时观察](evidence/request-diagnostics-20261020/timing-observations.json)
仅用于有界工程接受，不作配对统计或优化主张。按非性能任务接受，
不因小幅波动补测；后续直接部署，见
[部署记录](REQUEST_DIAGNOSTICS_DEPLOYMENT_2026-10-20.md)。

部署完成：两模式短健康检查通过，默认3eb9b2a0，旧d4232b63回退包
保留；部署lease约75.44秒、86文件复核。所有验证服务已退出。
后续使用stderr中的`[q4t][request]`记录，先按服务启动实例及instance
分组，再关联已注册id、seq和end；累计等待桶不当作GPU执行剖析。
