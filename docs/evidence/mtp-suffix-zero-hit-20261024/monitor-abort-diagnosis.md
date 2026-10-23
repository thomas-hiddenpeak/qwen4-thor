# 阶段A监控失败定位

本次仍为 **ENV_ABORT**。26条HTTP、6次启动均记录完成，但采样器在收尾失败；外层COMMAND_FAILED/child1并释放lease。没有运行成功限定性能分析，没有新增HTTP/GPU，也没有接受候选。

已核对使用的原始文件与run/files.json一致，相关源码与冻结plan一致。原始失败文件不改写。

## 已证明的范围

- 1580次采样尝试，1579条完整flush；最后一次失败没有留下partial row。原异常仅`PermissionError(13, 'Permission denied')`，未保留PID/文件/traceback。
- 最后attempt完成53个固定源读取和ownership扫描（890次文件读、445次readlink）后，计数到9次owned文件读、1次目录扫描。因此不是冻结sysfs源读取失败，也尚未进入q4t task目录扫描。
- 最后服务正常退出时间 `982855.137124128`；失败attempt开始由summary重建为 `982855.167224322`，晚约30.100ms；失败结束 `982855.191947344`。最后HTTP case完成到失败attempt开始约645.308ms。

## 强推断，不能当成历史证明

最符合计数的是：先读owner3102252的5个文件，再读full-off Python worker3114212的stat/status/schedstat，io作为第9项遇到EACCES。该worker正在结束，服务3114215已被它reap。失败attempt未保存具体owned列表/行，不能排除其他退出竞态或权限失败，因此历史PID和io字段仍未被证明。

## 隔离CPU验证与最小原型

`monitor_abort_cpu_mock.py`执行9个mock断言，验证旧代码在假定第一进程5项之后第9次io失败、同身份Z/X分类，以及活进程/身份改变/无法复核/非io权限错误仍失败。另创建一个自有未reap CPU子进程，本机确实返回`/proc/PID/io` EACCES；旧函数抛错，原型能通过同一pinned fd复核身份和Z状态。该子进程已在finally中waitpid清理。新试验只证明内核机制可发生，不证明原历史故障的精确位置。

原型仅位于`telemetry_exit_prototype.py`，没有替换冻结工具或运行时：添加PID、相对文件、操作及traceback；只对io EACCES复核同一已固定目录，start_ticks相同且明确Z/X才记录退出并保留原错误。活进程/不可确认身份/复用PID/其他权限错误仍ENV_ABORT；固定sysfs源错误仍ENV_ABORT。

未来采用前仍需要真实完整生命周期验证；本轮不重开GPU、不补样、不把失败降格。该原型修复的是监控退出竞态，不能称为零命中性能优化。输入、原型和CPU验证SHA均在JSON中。
