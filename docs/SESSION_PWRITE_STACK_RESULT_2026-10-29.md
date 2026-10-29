# 栈采集修复、异常保留验证与子集补测结果

2026-10-29：**栈冲突修复与异常收尾取得实机证据，但子集补测未完成，
阶段结论仍为 ENV_ABORT**。本轮仅启动一次子集服务，8 请求准入、5 请求
有完整客户端及输出核对证据，0/2 覆盖后恢复；完整 head 旧 10 HTTP/2
恢复直接复用。没有重复整套验收、追加服务或更换默认程序。

## 栈错误的证据与修复

原事故尾部已丢失，原 errno 无法追认。Linux `bpf_get_stackid` 在哈希桶
被不同栈占用、未启用覆盖时返回 `-EEXIST`；表未满也会出现。参考
[Linux v6.8 stackmap.c](https://raw.githubusercontent.com/torvalds/linux/v6.8/kernel/bpf/stackmap.c)。
本机 6.8.12-tegra 的最小复现将测试栈表限定为一个桶，捕获实际 helper
返回 **-17/EEXIST**，并非在用户态伪造返回值；不能据此称原事故 errno
已实测。本轮生产配置仍是 4096 桶，HTTP 中栈错误计数为 0。

修复只在 EEXIST 时使用 `bpf_get_stack`，将完整栈存入独立、只追加的
2048×256 字节有界表，ID 从 4096 起；不覆盖旧栈、不用只比较哈希的
快速选项。其他 helper 错误或容量耗尽仍失败。首个/最后 errno、次数、
回退次数独立存在 map，异常后和栈表一同保存。

- 冲突失败夹具：1250 条 emitted/received/persisted 完全一致，负 errno
  事件、原栈表、program counters 全部保存，状态保持 FAILED。
- 修复夹具：一次真实冲突成功保存新栈；6 次 pwrite、17,047,552 字节
  及 NVMe 生命周期闭合，原 ID 与新 ID 对应不同栈。
- CPU 栈探针：nanosleep/futex 两种实际内核路径发生 16 次冲突；新 ID
  全部独立，原栈不变。在 pending=4352 字节时注入用户态异常，最终
  142 条事件、17 份栈完整保存，状态仍 FAILED。
- 3 项新离线合同通过；原 28 项生命周期合同的各解析函数 AST 未变，
  原 5 项 finalizer 合同的实现未变，明确复用。两存储夹具合计
  34,095,104 字节（32.516 MiB），三次 BPF 会话，无 GPU 数值夹具。

CPU 栈探针初次审计误把字典事件当元组，原 KeyError 保留；只修离线
审计后重放同一记录，没有重新采集。上述门禁于 09:10:32 UTC 完成，
早于冻结的 09:30:28 工具截止。

## 子集 HTTP 中的新失败与具体原因

唯一 HTTP 阶段 09:10:33～09:19:53 UTC，完整驱动全程持共享 GPU 锁。
公共 ON 四请求和 OFF 主请求完成，第二组三个子请求中止，恢复未执行。
原 state 的 requests_completed=0 是“整个 step 未完成”的计数，不能据此
抹掉 5 个已完成客户端窗口，也不能将其写成 10/10 通过。

失败不是再次发生栈错误：最终 counters 为
`[3,1171097,0,0,0,40,245]`，read_fail=40，stack_fail/ring_lost/map_fail=0。
**1171097 条、149900416 字节全部落盘**，pending/partial/finalization
errors 均为 0；30 个被引用的非空栈 ID 全能解析，栈表与辅助计数保存。
这在真实 HTTP 异常中验证了上轮丢尾部和丢栈表的问题已经修复。捕获末端
仍有在途调用：完整保存“已经发出的事件”不等于补出卸载探针后的完成事件。

原始记录显示失败窗口内后台 PID 10506 `udisksd` 发出四条 NVMe 管理
命令（一个 Identify、三个 Get Log Page）：qid=0、disk 为空，每条有
setup/CQ/complete 次级事件。管理队列的 `q->disk` 可以为空；旧探针
无条件读取其 major/minor，把正常管理请求误当内核读取失败。40 次错误
与该路径相符；原 raw 代码在读失败后没有记录这些请求，不能声称已从
raw 逐条证明其全部五个回调。内核同样显式允许空 disk，见
[NVMe trace 定义](https://raw.githubusercontent.com/torvalds/linux/v6.8/drivers/nvme/host/trace.h)。

已补两侧过滤：raw 在成功读取 q/disk 后排除空 gendisk；formatted 同样
排除空名称的管理事件，避免没有 raw setup 的次级事件破坏关联。真实
非法读取仍计 fatal。对**实际探针 C 代码**的 host mock 验证了管理队列、
正常 namespace I/O 和非法读取三条分支；BPF/host 均零警告编译通过。
这项事后修复没有再次 attach、加载模型或发 HTTP，不能记为实机通过。
另将异常栈表导出按已分配槽位遍历，避免失败槽位后仍有成功项时漏导。

host 编译首次发现原有误导缩进；旧 shell 曾继续写出不可靠检查元数据，
现已在 `check-first-invalid.json` 显式标记 invalid/failed。修正缩进后用
逐命令 check=True 重新编译和运行，真实通过记录为 postfailure/check.json，
时间 09:25:27，仍在工具预算内。原失败没有覆盖。

## 可用的子集慢写窗口

只使用已经完成的 5 个响应及其 6 个完整文件（含公共 seed）。文件代次、
offset 连续性、精确写字节、调用数、HTTP 时间边界逐项独立核对。捕获末尾
首次不闭合调用之后不归因，保留原错误及不完整请求。两主文件均
4,159,963,136 字节、598 次 pwrite；分段如下，单位秒。

| 子集主请求 | pwrite | issue→CQ 并集 | ext4 map 并集 | 直接 I/O 等待栈 off-CPU | pwrite CPU |
|---|---:|---:|---:|---:|---:|
| 公共 ON，auto-0 | 8.329 | 8.248 | 0.079 | 7.731 | 0.230 |
| 公共 OFF，auto-4 | 16.520 | 16.453 | 0.129 | 16.009 | 0.232 |

直接 I/O 栈为 `blk_io_schedule → __iomap_dio_rw → iomap_dio_rw`；位图
等待主要为 `ext4_wait_block_bitmap`，两主窗口约 62.6/117.9 ms。慢增量
集中在在途 I/O 完成等待，CPU 执行没有同量级增长。文件系统与 I/O 区间
重叠，不能相加；issue→CQ 仍包含设备队列、驱动/PCIe/IRQ 延迟，不能
称纯 NAND 写入或认定 SSD GC。管理命令出现在两个主窗口之后，不把它们
当成上述慢主保存的原因。插桩开销未做无插桩校准，不宣称净性能收益。

另核对本机 sched_switch 的 256 为被抢占标志，不是等待唤醒的睡眠。
原保守解析把这些区间留作 missing wake；本轮独立离线复核按原始 state
区分：两个主窗口真实阻塞均有 wake，唤醒到运行约 5.944/7.166 ms，
被抢占离 CPU 约 0.244/0.253 ms。未追改旧解析结果或旧失败状态。

## 资源、身份、复用与决定

560 次资源采样全部 flush，采样器无错误；推理步骤 ENV_ABORT、外层
COMMAND_FAILED 均保留，原缺失退出屏障不伪造。自有 driver/collector/
server 均退出，没有停止 udisksd 等外部进程。collector HWM 120,119,296
字节，加 maps 22,228,640 和 4 MiB 输出缓存的保守上界为 146,542,240
字节（可能重计共享项，不是精确独占物理 RAM）；最低 MemAvailable
10,237,341,696 字节，模型及 collector swap 为 0。模型进程 RSS 不代表
CUDA 统一物理内存总量。预处理仅复用旧记录，跨时段 SSD 状态不认为相同。

736 份测量依赖身份逐项复核；实际 HTTP 工具单独 frozen-tools，事后
修复另存。程序仍为 46958b6c538d4410bea204bce1437c433b7a1ec87ea02855fb9417bece1ae9a6；
默认仍为 6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965，
发布元数据仍为 5fdca879f84da6795b66113833de7167df6edbcff0d97e09d474113e47a67de2。
完整 head 旧 10HTTP/2恢复、A112/S32及未变状态合同直接复用。

- 证据有效性：异常保留和闭合文件窗口有效；整个 HTTP 阶段 ENV_ABORT。
- 测量完整性：新增 subset 5/10 完成、0/2 恢复；合并旧 full 证据也未齐。
- 统计结论：不作优化收益或非劣结论；旧推理发布 NO_GO 保持。
- 工程决定：有界停止，工具修复及部分证据归档提交，不安装推理候选。

[冻结预算](SESSION_PWRITE_STACK_PLAN_2026-10-29.md)为 60 分钟；没有追加
服务、请求、存储夹具或 BPF 会话。下一步仍需先验证管理命令过滤的实机
路径，再按剩余缓存状态合同决定补测范围，不能把已取消请求或未执行恢复
写为通过。本轮不自动启动。完整原始记录见
[证据与独立归档入口](evidence/session-pwrite-stack-20261029/README.md)。
