# SRV-11e真实短验清单

业务/可靠性验收，不作为性能优化。原88应用CPU首轮通过，23次文件
操作SIGKILL，其中新增备份/恢复12次；原411/263/468/402证据复用。
本清单与驱动冻结提交后才启动模型，失败保留现场，仅重验受影响项。

固定原后端/TLS包与alpha512、beta64 Key，loopback；1模型/1网关。
新source store max_tasks=3/max_keys=3/归档128MiB/测试保留期0；
快照字节上限2GiB，实际完整原件按清单核对，不删除备份或源库。

1. alpha known任务真实生成一次，读取MODEL第1..24行，保存工具
   结果后SIGKILL。beta unknown任务真实预检通过，持久化请求意图
   后、传输前SIGKILL；该边界虽未实际提交，也必须保持REQUEST_UNKNOWN。
2. beta archived任务用64输出预算完成2生成/1真实工具，答案及引用
   正确后归档，正文保留。这时后端累计3成功，未知意图不计实际生成。
3. 全库一致性备份，在复制完成、seal前SIGKILL，按同源继续封存。
   完整文件、原事件、未知及永久键核对。
4. 计划恢复到新路径，准备目标后持久封源，再SIGKILL。源旧任务
   resume及新键submit各拒绝；原计划继续，只发布一个目标库。
5. 目标unknown resume保持原状态/预算，零HTTP/工具；known显式
   resume只做1次最终生成、0新工具，完成真实答案并引用原结果。
   重复known submit零执行；archived读取原答案、重复submit退出2。
6. 显式清理目标archived正文，再重复submit/resume，永久键仍在且
   零执行；unknown重复submit也不执行。重复restore不能覆盖新结果。

预算：实际HTTP生成4次全部成功，账本请求意图5次（含1未知未提交），
真实工具2次，preflight5次，capabilities4次；4次实际SIGKILL（2应用、
1备份、1恢复），2源库拒绝，4重复submit、2非执行resume，1重复恢复。
父控制最多64，GPU420秒/总480秒。源/目标/快照完整原件、真实wire、
未知边界、操作日志和模型指标分别证明，不以计数声明代替实际事件。

全程同一后端内置生产GPU guard，不叠加外层锁；应用及网关先退出
再停止后端。验证9秒以上无复起、原锁/GPU释放、SOAR masked、容器
空、默认pair/Key/三份day保持。未开展小时/day、实际LAN或源盘丢失灾备。
