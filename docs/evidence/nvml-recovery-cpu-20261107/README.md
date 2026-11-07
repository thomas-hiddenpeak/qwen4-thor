# NVML分类与有界恢复CPU证据

`result.json`给出168项不同CPU用例及未运行范围。`source-bindings.json`
绑定最终实现、旧v7包、默认程序与两份视觉ELF，保留纯测试登记变更。

`raw-records.tar.gz`无损封存命令、首轮/定向复验日志、语法/来源记录、
CMake测试登记及“shutdown超时遮蔽设备故障”的修复前失败。
`manifest.json`列出成员长度、SHA256及本地来源目录。

真实CPU子进程与私有测试锁不等于真实模型/GPU验证。旧v7包的只读
验证只证明封存完整，不能将它的业务接受转给新v8。未知PID不记空闲，
监控恢复不清除缺口，也不自动启动或重启模型。
