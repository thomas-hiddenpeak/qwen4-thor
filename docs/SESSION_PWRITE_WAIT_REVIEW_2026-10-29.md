# pwrite等待：离线分层复核与下一步有界方案

2026-10-29。结论：**秒级差异仍在同步写调用的非CPU区间，但现有证据
不能把它继续定量拆成ext4、块层队列和设备完成。** 本轮找到两个具体
观测要点：慢值主要分布在许多普通写调用中；KEEP_SIZE预分配仍保留逐块
扩展文件的文件系统路径。后者是待检验机制，不是已证实根因。

本轮只读旧日志、源码、当前系统配置与SMART，没有新模型加载、HTTP、
CPU存储夹具、SSD压测或开启内核追踪；新增写入仅为分析文档和证据。
不改变[预分配NO_GO](SESSION_PREALLOCATE_RESULT_2026-10-29.md)、原正式
发布NO_GO、默认程序或任何旧归档。

## 从旧证据进一步得到什么

新工具`tools/session_cache/review_pwrite_wait.py`绑定原归档manifest
`7f2c81f7ed0c714e1c3e5f8e573f944635fde64414530219cfcc299bffc95e46`，
核对16份原文件与4份受测源码/依赖，重放全部8份主快照。额外用4份
原server.log独立交叉核对计时字段。候选仍为46958b6c、源码锚点73d9792。

每份主快照为4,159,963,136字节、598次写调用；下表按实际执行顺序，
公共开关指该事务的公共prefix共享。平均值没有控制每个调用的字节大小，
只能描述同布局下整组差异，不能直接当作设备单请求延迟。

| head/预分配/公共 | pwrite墙钟 s | 线程CPU s | 平均调用 ms | 单次最大 ms | 超50ms次数 |
|---|---:|---:|---:|---:|---:|
| full/0/OFF | 2.019 | 0.134 | 3.377 | 41.241 | 0 |
| full/0/ON | 1.916 | 0.135 | 3.204 | 40.781 | 0 |
| full/1/ON | 9.896 | 0.153 | 16.549 | 61.802 | 8 |
| full/1/OFF | 11.319 | 0.153 | 18.929 | 57.418 | 6 |
| subset/1/OFF | 9.911 | 0.131 | 16.573 | 61.263 | 4 |
| subset/1/ON | 12.670 | 0.129 | 21.188 | 56.681 | 4 |
| subset/0/ON | 11.677 | 0.189 | 19.527 | 61.100 | 8 |
| subset/0/OFF | 13.098 | 0.190 | 21.904 | 122.754 | 64 |

除最后一份外，5份慢快照至少95.0%～98.2%的pwrite墙钟来自单次不超过
50ms的调用。保守下界用`总时长 − 超50ms次数 × 单次最大值`求得，受日志
毫秒舍入影响，不是逐调用分位数。不能只抓>50ms慢调用或只找一次大卡顿。

覆盖写入的1Hz采样包络内，推定写线程的CPU运行队列等待仅0.055～7.043ms；
系统pswpin/pswpout、直接回收和allocstall_normal增量均0。进程自身读取
计账仅0.078～0.549MB，写入约4.160GB。没有自身大规模PLE读取、交换或CPU
排队解释十秒等待的证据。慢快照写线程D状态采样较多，与阻塞一致。
推定线程依CPU增量识别，最后活跃采样可能早于退出；包络包含尾部与日志，
这些数值不是精确Save全区间或D状态时长，也不能排除未采到的外部I/O。

原64GiB固定预处理同样使用8MiB同步O_DIRECT顺序写，但没有GPU推理和逐块
SHA；16个4GiB段已经有7.898～44.419秒差异。说明大幅变动并非只存在于
MTP/head路径。它同样经过文件系统和设备，不能凭这一点直接认定SSD固件
GC或SLC耗尽；也不作为性能验收结果。

## 三层边界与具体代码机制

| 层次 | 现有事实 | 尚不能得出的结论 |
|---|---|---|
| 文件系统 | O_TMPFILE/O_DIRECT，每次从当前EOF继续写；原/预分配路径都未预设文件长度 | 无法知道extent映射、长度/日志更新或完成转换各等了几秒 |
| 块层 | 应用单线程串行pwrite；单个8MiB调用可产生许多并行块请求 | 应用一次只等一个syscall不等于NVMe队列深度1；无历史insert/issue/requeue事件 |
| 设备完成 | pwrite非CPU时间由1.78～1.89秒升到9.74～12.91秒 | 不等于纯NAND写入，仍包括主机等待、调度、驱动与完成处理 |

代码`SessionCache::Save/Transfer`保持“拷贝→补齐→SHA→pwrite”598次串行。
`fallocate(FALLOC_FL_KEEP_SIZE)`分配空间但保留文件长度0，之后各块继续
扩展EOF；项目Save没有fsync/fdatasync、O_SYNC或O_DSYNC。原预处理最终的
fdatasync在另一个函数，不能移植成每次快照保存都在fsync的解释。

上游Linux v6.8的ext4 DIO路径把扩展写与普通覆盖写区分；扩展可能涉及
inode长度及journal更新，unwritten extent在完成时转换。KEEP_SIZE成功
不能排除这些路径。此处以固定版本机制解释本项目代码；本机是NVIDIA
6.8.12-tegra，未把上游文件声称为该厂商内核逐字节源码或已执行trace。
参见[Linux v6.8 ext4/file.c](https://raw.githubusercontent.com/torvalds/linux/v6.8/fs/ext4/file.c)。

本轮只读盘点：快照、模型和日志均位于根盘`/dev/nvme0n1p1`，ext4
`data=ordered,commit=5,nodiscard`；设备WD PC SN5000S，固件34430000。
当前scheduler为none，max_sectors_kb及max_hw_sectors_kb都是128，write cache
为write back。在此上限下，8MiB至少拆64个请求；全快照数据至少31738个
请求。**这是按当前配置的推算，不是旧试验已采得的请求数量。** none仍有
资源不足/dispatch等待，不能解释成无队列。[Linux blk-mq说明](https://docs.kernel.org/block/blk-mq.html)。

当前SMART critical_warning、media_errors、num_err_log_entries均0，温度约
39°C，累积温度阈值计数0；这不支持已有介质报错/阈值告警，也不能证明旧
慢区间没有GC、写缓存变化、固件节流或外部竞争。当前挂载、剩余空间和
SMART均独立标注采集时间，不补填成旧测试时的状态。

旧遥测只有进程io/stat/schedstat、内存/CPU/频率/温度；没有diskstats、
设备队列生命周期、等待栈或当时SMART。`write_bytes`不是物理NAND字节；
后续diskstats的写时长也包含请求生命周期，不是独立的设备执行时长。
参见[Linux I/O统计定义](https://docs.kernel.org/admin-guide/iostats.html)。

## 已核对的采集能力

本轮未挂探针，仅读取配置和事件format。sudo非交互只读可用。

- 可用：BPF_SYSCALL/BPF_EVENTS、raw_syscalls enter/exit、sched switch/wakeup、
  ext4映射/转换/journal事件、jbd2、block insert/issue/requeue/complete、
  NVMe setup_cmd/complete_rq/sq；clang-18和libbpf.so.1在机。
- 不可用：KPROBES、FUNCTION_TRACER、FTRACE_SYSCALLS、BLK_DEV_IO_TRACE、
  PSI、BLK_WBT；没有vmlinux BTF，也没有libbpf开发头。不能直接使用
  常规kprobe/bpftrace、function_graph、CO-RE或blktrace方案。
- 使用raw_syscalls按本机aarch64 syscall编号识别pwrite64（68）；不能
  直接引用不存在的`syscalls:sys_enter_pwrite64`。
- 普通block事件format没有request指针，不能只用sector进行无歧义配对。
  下一阶段拟用raw tracepoint的request指针＋生命周期序号，按本机匹配
  headers校验结构布局；完成可能部分完成，CID、fd和request地址会复用。
- **nvme_sq不是提交时间戳**：上游v6.8在处理CQE时触发它；setup_cmd也
  在DMA映射/实际送入队列之前。下一轮只报告“主机可见设备完成路径”，
  不把setup→complete或issue→complete称为纯NAND编程时间。
  参见[NVMe PCI实现](https://raw.githubusercontent.com/torvalds/linux/v6.8/drivers/nvme/host/pci.c)
  和[NVMe事件定义](https://raw.githubusercontent.com/torvalds/linux/v6.8/drivers/nvme/host/trace.h)。

## 下一步提案：一次60分钟分层定位，不含新优化候选

本节是下一阶段方案，**本轮未实施或执行**。目标是获得可操作的等待
归属，或在预算内明确“无法归属/未复现”；不再次证明预分配收益。

| 预算 | 固定范围 |
|---|---|
| 总墙钟 | 60分钟，含工具准备；20分钟工具/CPU能力检查，最多35分钟采集，末5分钟退出封存 |
| 程序 | 冻结46958b6c同一程序，PREALLOCATE=0，SAVE_TIMING=1；不重建模型程序 |
| GPU/HTTP | 全阶段同一共享独占锁，2启动、20生成HTTP，无新GPU数值fixture |
| 顺序 | full：公共OFF→ON→恢复OFF→ON；subset：ON→OFF→恢复ON→OFF |
| 每服务请求 | 每事务主131071＋子33274/49150/59986各256输出；两次主恢复各16上限；合计10 |
| 配置 | 208896长槽＋3×65536短槽，max-prefill4096，greedy MTP k3、suffix、两级缓存；subset仍65536行BF16 |
| 冷态 | 每服务空缓存池、新ID/scope；复用原64GiB固定一次预处理，不追加循环、不声称SSD稳态 |
| CPU工具自检 | 最多2个小型夹具，合计直接I/O写入≤64MiB；只校验事件和恢复字节，不报SSD性能 |
| 资源 | 总应用逻辑写入≤128GiB（含预处理/夹具/日志），采集新增统一RAM≤256MiB，原始trace≤2GiB/1000万事件 |

不改变scheduler、mount、SSD缓存或电源参数；不做discard、清全机页缓存、
固件操作或全盘压测。复用旧40HTTP输出、8恢复、68CPU合同，以及A112五档、
S32重复收益和未变目标/草稿状态合同；新20HTTP只是观测载体，不算第二次
完整验收。恢复仍须槽位被别的主请求覆盖后命中131323并回答44。

工具先用固定格式的tracepoint/raw-tracepoint eBPF方案，不依赖BTF/kprobe。
headers、事件format、syscall ABI及加载器均在20分钟能力检查中绑定。
小夹具验证fd复用、两文件关联、拆分/完成字节及时间顺序；解析器用离线
样例覆盖重排、merge/requeue/partial completion、丢事件与地址复用。
若加载/关联/栈采集不能闭合，记TOOL_UNAVAILABLE并在加载模型前停止，
不临时重编内核、不换第二套采集路线。现有只读盘点不冒充BPF附着成功。

采集输出按以下合同闭合：

1. **调用边界**：记录每次pwrite的TGID/TID、fd代次、offset、count、return、
   enter/exit、运行/可运行等待；以ext4事件的dev+inode、连续文件offset和
   HTTP/Save次序关联快照。不能关联的并发请求标unknown，不猜测归属。
2. **文件系统**：配对ext4映射enter/exit；记录unwritten转换、inode dirty、
   journal及jbd2提交。写线程睡眠时记录可符号化内核栈，用于区分DIO完成、
   journal/锁、块层资源等待。只有起点的事件只计次数，不能伪造耗时。
3. **块层**：用request指针＋代次关联insert→issue→complete与requeue，
   保留读/写/元数据、扇区、拆分/合并、部分完成。没有insert的直发请求
   单列，提交前/tag等待也不能默认为零。IRQ或worker完成不按当前PID丢弃。
4. **设备完成**：关联NVMe ctrl/qid/cid＋代次及request；分别报告准备、
   下发至完成、完成至syscall返回。记录目标盘其他来源读写/flush/discard，
   1Hz diskstats及SMART开始/结束差值作为旁证，不能替代逐请求关联。
5. **时间核算**：全部使用可对齐单调时钟。并行块请求的耗时不相加当墙钟；
   报每次syscall时间线、区间并集/重叠、已解释与未解释秒数。enter→首issue
   的空白不能全部记ext4，末complete→return也不能漏掉CPU唤醒排队。
6. **观测自身**：内存有界，尽量在自有快照写入窗口外落盘trace，并标记
   采集器自身I/O。保留丢事件、map满、配对缺失和最终flush/退出屏障。
   追踪开销尚未校准，本轮不由被追踪吞吐宣布优化或统计性能保持。

### 固定出口与后续选择

- CPU能力检查失败不消耗GPU额度；采集中监控/竞争失败记ENV_ABORT，仅
  停自有进程、detach自有BPF link，不重置全局tracefs、不停止系统进程。
- 20请求、截止或资源上限任一到达即停。到达日志上限先封存并审查已经
  完整的窗口，不因“文件较大”否定全部日志；丢失/截断影响哪些窗口需明示。
- 事前以`主快照pwrite总墙钟≥8秒`识别本轮待定位的慢窗口。未复现就记
  NOT_REPRODUCED，不追加预处理或请求直到变慢；存在慢窗但无法解释就记
  INCONCLUSIVE，保留未知秒数，不把残差自动归到文件系统或SSD。
- 只有同类长等待同时得到调用栈及生命周期事件支持，才提出一个修复方向：
  ext4扩展/转换占主导时评估一次性设置文件长度；主机块层等待占主导时
  根据归属处理自身I/O提交或明确竞争源；设备完成段占主导时停止盲改推理
  计算，保留设备层限制。单次采集不能直接区分SSD内部GC和SLC，不能承诺
  软件一定可修复。此次不同时实现这些候选。
- 无论归属成功与否，到点记录证据有效性、测量完整性、统计限制和工程决定。
  当前NO_GO、默认程序和已通过收益保持；定位成功也不等于发布通过。

## 本轮证据与收尾

只读复算：`.q4t-work/session-pwrite-review-20261029/review.json`，
当前系统盘点：同目录`host-inventory.json`。版本化副本和独立交叉核对
见[证据目录](evidence/session-pwrite-review-20261029/README.md)。没有修改
运行时代码、旧计划、旧decision或原归档。默认程序SHA256保持
`6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965`，
发布元数据保持`5fdca879f84da6795b66113833de7167df6edbcff0d97e09d474113e47a67de2`。
