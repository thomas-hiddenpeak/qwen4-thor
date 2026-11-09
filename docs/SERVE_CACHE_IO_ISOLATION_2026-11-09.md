# R01可选缓存文件I/O隔离

2026-11-09，R03覆盖复核发现。属于辅助依赖隔离修复，非性能优化；
不修改模型计算、缓存字节校验、前缀/布局合同或SSD容量卸载路线。
全部Serve总计划继续，之前日志/观察隔离交付仍保留其实际通过范围。

## 发现与必须修复的行为

`session_cache.cpp`在缓存锁下执行open/pread/pwrite/fstat/close，
调用者还持有model_mu_。取消只发生在I/O调用之间，单次调用阻塞
可使后续模型工作等待。可选会话/前缀缓存不能成为健康LLM的单点依赖。
缓存中的GPU复制仍由模型进程完成，已补独立执行进度；文件故障不能
假称CUDA坏了，也不能靠重启健康模型来恢复可选缓存。

## 原生实现与所有权

1. 复用本项目exec-only隔离进程和有界IPC/回收原语，增加独立的
   session-cache和prefix-cache容量域。缓存worker不占grammar或
   application-object槽位，不加载模型、不持有CUDA或GPU租约FD。
2. worker独占目录、O_TMPFILE/O_DIRECT文件和所有文件系统调用；父
   进程只持代次及逻辑文件ID。仍在原私有磁盘目录中建立匿名临时文件，
   原文件/模型目录只读边界不变。每个缓存最多8条，创建替换时有固定
   余量；磁盘额度继续按实际分配块计量，不能把稀疏EOF当占用。
3. 一域一个持久控制线程及至多一个在途任务。spawn/文件/回收不能
   阻塞请求无限等待；单次文件操作IPC等待最多2秒并响应已有取消。任务缓冲和
   生命周期独立，不让超时线程引用请求栈、模型指针或可复用staging。
4. 每块最多8MiB，父staging、IPC任务缓冲、worker staging和库/进程
   开销分别计量。它们仍属于Thor统一RAM，绝不宣传CPU容量offload。
   初版用有界字节传输，不为追求零拷贝引入新的共享所有权漏洞。
5. 超时/坏帧/worker退出后丢弃未确认缓存状态，不重放读写或模型
   请求。新代只在旧worker实际回收后启动，旧文件ID不能进入新代。
   有界退避及每分钟最多3次启动，失联期间缓存miss/保存失败，普通
   生成继续；半开成功从空缓存重建。未回收仍占原槽，不能增殖。
6. 缓存控制线程创建/执行失败同样有界；停止意图终止恢复。公开状态
   区分配置、可用、回收未确认、退避与失败，不把缓存未知投影成模型
   不健康。诊断只含固定原因/计数，不含会话ID、Key、提示或缓存数据。

## 分组验证和交付

先冻结隔离存储原语8类CPU：真实exec只继承通信FD；私有目录及磁盘
要求；真实多块读写和实际块占用；逻辑ID/代次/越界/坏帧拒绝；暂停
自有worker后期限/取消有界；未回收不增殖且不同域不互相占槽；恢复
预算/退避/停止意图；控制线程异常或spawn停滞不留请求引用。

随后接入现有SessionCache并冻结直接受影响的字节级、校验、增量/
前缀/取消/发布合同及降级负控，复用未改的模型数值证据。只有具体
失败才补验受影响项，不循环跑完整矩阵。与R03资源进度一起执行
一组真实缓存保存/恢复及worker故障后同PID业务短验，具体请求与
GPU预算在驱动实现后冻结；已过的主循环恢复不再重做。

CPU通过、源码提交和缓存miss都不能代替这项隔离实际完成。全部
实现及业务通过后做可回退交付，再继续R04/N04等全部计划余项。

冻结首组实际CPU名称（8项；先必要构建，非完整套件）：

```text
cache_io_exec_inherits_only_owned_channel
cache_io_private_disk_directory_contract
cache_io_multiblock_bytes_and_actual_sparse_allocation
cache_io_bad_frames_generations_and_bounds_rejected
cache_io_stopped_worker_deadline_and_cancellation_are_bounded
cache_io_unreaped_worker_retains_only_its_own_domain
cache_io_restart_budget_and_stop_intent
cache_io_controller_exception_spawn_stall_and_thread_failure
```

接入组冻结：4个新增SessionCache CPU（坏目录保持会话租约、真实半恢复
丢代/重建、取消不发布/不重放、无私密内容的可选健康状态）；3个现有
实际磁盘字节合同（私有、共享前缀、增量）；扩展目录合同覆盖既有相对
路径兼容；2个受影响grammar容量域合同、2个既有target-only布局缓存合同。
实际跨代关闭补验与扩展目录各1项。另验证实际C++健康输出与
config/schema负控、来源及声明一致性。只新增缓存相关检查，不跑全套。

RAM说明：每域父staging/一个IPC payload/worker staging各8MiB，初始化
路径另至多4096字节。buffer上限不包含ELF/共享库/元数据；公开实际
worker RSS观察值及是否已知，不能以32MiB旧预留称为整个进程硬上限。
匿名文件按已有额度、实际块计量；恢复后的空缓存保持原请求租约，
只有新保存及成功HTTP完成才能发布。已有相对目录语义由worker
继承工作目录解析，不在模型请求线程执行realpath/getcwd。

多块正常保存/恢复不宣称整份缓存只需2秒；每次文件等待有界，任一
无回执的操作立即废弃该代，不继续后续块或自动重放。请求取消在
每块及IPC等待中持续检查；必要清理仍占原容量，旧逻辑ID不能关闭
新代文件。真实请求业务验收仍未完成前不宣称整项交付。
