# PR38 对象回执恢复夹具修复

最终d4e76618的一组CI通过，另一组在故障恢复后的正常payload GET
遇到250ms ObjectStore超时。丢失回执由worker完成持久提交后暂停
确定触发，不靠250ms制造；本次恢复该夹具默认2秒I/O预算。单条与
批量均保留commit_unknown、精确原数据/revision和worker重建次数断言。

本机隔离零警告构建及该1项直接用例通过，4.242秒，0GPU；运行时代码
及默认rev214未改。当前工作树其他Stop源码不属于本次提交，对象存储
实现与原head一致。原CI日志和误用--filter被入口拒绝的尝试均保留；
修正为--required-list后才实际执行。本修复不追认失败CI，等待新head
检查通过后实际合main，其他master任务继续。
