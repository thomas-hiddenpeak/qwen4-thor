# R09 跨根服务实例保管 CPU

10 项首过：6 项新实际 Guardian/锁交接，加 4 项直接原行为回归。
CPU 2.934 秒，必要 Guardian 构建 10.545 秒、零警告，0 GPU。

父层保留实际 guardian.lock 描述符，子 Guardian 退出后实例仍独占；
父层关闭不解锁活子代副本。resume 保持原 STOPPED 和零角色启动；
未持锁/错 inode/daemon detach 负控在控制器启动前拒绝。
原前台停止、父死清理、daemon 握手和冷探测拒绝行为通过。

首次完整host配置因该隔离工作区未配置非目标文档codec头文件而
中止，原记录保留。改用只include原生产GuardianProcess.cmake的
临时CMake项目构建Guardian，未安装依赖，未替换源构建规则。

仍未接自动外层入口或slot退休，不把配置v5原语等同完成R09；
未封包、未发布。原工作区R07候选和rev64默认保持，继续全部任务。
