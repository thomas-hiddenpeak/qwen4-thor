# SRV-11i：租约调用路径定向修正

2026-11-04。原真实短验入口以相对路径调用gpu_exclusive，而历史
require_guard按绝对路径识别祖先进程，因此在begin/模型启动前拒绝。
原guard为COMMAND_FAILED，0模型/HTTP/自有服务，lease已结束且原锁
dev66305/inode3294067可取得，GPU空。原计划/命令/stderr/lease完整保持。

新驱动增加`guard`入口，固定guard、worker和output绝对路径，先查
冻结计划再spawn。新尝试独立`retry-01`；原23项CPU和4文件SIGKILL
复用，仅新增2入口检查。生产两个注册表和应用执行逻辑保持，旧测试
源码快照/原导入失败保留。新计划绑定原失败全部摘要和入口修正收据。

首轮锁窗口约1.27秒，新窗口265秒，总GPU租约仍≤原270秒；2顺序
模型、4响应/2真实工具、80控制的业务预算不增加，未开展过的真实
交接仍待执行。按下列入口，避免调用者手工拼接guard路径：

```bash
python3 -B tools/project_tasks/operations_retry_cpu.py
python3 -B tools/project_tasks/operations_stage.py prepare
python3 -B tools/project_tasks/operations_stage.py guard
```

源码提交并通过针对性CPU后才prepare；失败不会自动覆盖目录或重试。
不得把原租约记为COMPLETED，也不把23原检查和2入口检查混成重复通过。
