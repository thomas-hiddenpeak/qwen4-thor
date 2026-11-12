# Legacy文档编解码的x86系统调用修复

实际CI的strace定位到LibreOffice使用旧`mkdir`，被只覆盖`mkdirat`
的沙箱拒绝；创建既有profile也返回EPERM，随后exit77。两轮完整
失败日志压缩保存且可无损解压，未以诊断覆盖失败。

生产沙箱与IO代理补齐x86 pathname ABI：`open`/`mkdir`与`openat`/
`mkdirat`共用累计额度，目录创建尝试同样计费；旧stat/rename/unlink/
rmdir与已有at形式接受相同Landlock约束。未放开网络、任意进程或
写入绕过通路。x86测试直接交错调用两种ABI，要求第三次创建在执行
之前被额度拒绝；Thor验证mkdirat及既有文件/线程额度、取消与清理。

本机16项针对性CPU全部通过，2.536秒，零跳过、零GPU。真实doc/xls/
ppt转换、损坏输入隔离、资源拒绝、取消、工作区清理包含在内。
首条驱动误写`--require-list`导致0测试、exit2，保留后用正确
`--required-list`执行；未把它算通过。构建零警告。默认rev94未变。

x86分支仍以本次真实远端检查为准；尚未声称PR23合并。继续完成CI、
实际合并与命名空间业务短验，再推进全部master任务。
