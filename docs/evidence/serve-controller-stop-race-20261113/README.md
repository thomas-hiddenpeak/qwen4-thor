# 存储控制器停止丢失唤醒修复（2026-11-13）

PR23远端43252603019的registry先在Responses API出现控制器不可用，
随后190条同类错误及部分未检查失败的fixture崩溃；原始完整日志保留。
源码审查发现ObjectStore和两种CacheIo控制器均在等待条件互斥锁外
写stop并notify，可能在worker读到false与实际睡眠之间丢失通知。
这样100ms析构返回后，空闲控制器永远保留唯一槽，后续存储不可用。

保留原生产锁顺序，用只存在于CPU测试构建的调度钩子固定该窗口：
ObjectStore、session cache和prefix cache三项负控均实际得到
`early_notify=1 owner_released=0`。原锁顺序及复现补丁一并归档。
这确认了真实代码缺陷；远端没有完整线程栈，不能据此声称所有远端
失败都只有这一个原因，仍须以修后完整CI结果确认。

修复将stop写入和通知移入原等待条件的mutex；没有放宽100ms析构
等待、请求IO、重启预算或未回收worker/卡住控制器的唯一所有权保护。
针对性16项CPU首过（3.587秒）：三个固定竞态、CRUD、故障恢复、
预算、真实卡住spawn/未回收域、Responses持久历史与HTTP复用。
源码清单及5种遗漏负控通过，两个工作副本的6个实现/测试文件一致。
0GPU，默认rev94保持；新二进制业务与实际PR合并继续，不宣称已交付。

同类检查还覆盖Responses/Batch/Vector停止与HTTP heartbeat/scheduler。
后三者在通知前经过相同mutex，Responses异常捕获路径使用有限时等待；
没有将它们改成无界等待或扩大本次测试范围。下一步当前候选重建、
必要业务/封包、真实CI与合并，全部Serve master继续。
