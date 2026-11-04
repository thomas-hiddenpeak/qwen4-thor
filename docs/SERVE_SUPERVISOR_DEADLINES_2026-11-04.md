# SRV-12l 托管观测截止与停止优先

2026-11-04，服务可靠性修复。当前原生61abe09f在加载前保留监听，
TCP尚未提供HTTP；审核其托管接线时发现监督器先接受响应再检查期限。
三项无模型/无HTTP复现均失败：迟到首次READY逃过startup deadline，
迟到恢复清除observation deadline，stop文件已出现仍发布READY。
原实现与失败原件保存在`.q4t-work/serve-supervisor-deadlines-20261104/baseline/`。

## 冻结修复范围

- 每次backend尝试从启动调用前取得单调时钟deadline。未首次READY前
  使用startup_seconds；已就绪后失去观测，使用第一次失败观测开始时间
  加unavailable_seconds。期限跨完整探针及轮询等待，不被迟到健康响应
  重置；在发布状态之前再次核对。超期停止自有子树，不自动重启。
- 明确stop请求在等待/探针返回时优先于新的READY/DEGRADED/观测失败
  发布与期限判断；停止后不启动新尝试、不记成成功恢复。内存/host真实
  故障仍保留原失败处理，不被正常stop洗掉。
- 默认探针接收阶段deadline和停止回调；GET保持loopback、固定路由、
  凭据限制与响应大小/编码合同。停止可打断连接、发送及等待响应，
  可取消路径以至多100ms轮询检查，单调期限约束整个网络过程。已有
  不可取消调用接口保持；不新增重试、后台遗留线程或socket owner。
- poll等待同时受阶段deadline与stop文件/信号约束。生产新包冻结
  当前supervisor/transport；旧封存包不修改。引擎ELF、发布声明、
  模型协议15d7ccb1和三份用户day计划保持。

## 有界验收

先成组完成状态合同与传输检查；使用真实普通子进程的CPU监督器负例，
以及假socket的连接/写/读/EOF/停止/超期检查。原失败不可覆盖，修正
后只重验本组及受影响现有合同。代码/依赖未变的原生与模型证据复用。

真实loopback HTTP与模型全部同原GPU flock：独立无模型传输探针验证
不响应监听的取消/绝对期限；最多2次backend尝试（1次加载期显式停止、
1次完整模型就绪），完整模型4成功响应/2次真实项目资料工具。包括
可选独立运维/TLS双Key预算与公共满额后健康访问、grammar回收和正常
停机。总墙钟300秒，业务/GPU定时器240秒，余下时间用于清理与
10秒无复起观察；默认stop/kill仍30/5秒，不延长服务配置。
已通过部分不因报告修正重复模型。高级部署选项按需，不安装常驻服务，
不做小时/天级长验，不声称物理RAM硬保护或性能优化完成。

114项唯一CPU检查通过；原三个失败及旧源码保持。驱动另核对真实
租约FD传递、启动期TCP归属、失败清理及来源冻结，不重跑该114项。
固定命令：`python3 -B tools/serve_supervisor_deadlines/stage.py`
依次`prepare`、`check`、`run`，辅助`report.py preflight/postflight`。

完整模型由封装的生产CLI启动并持有gpu_exclusive原锁。进入READY
后驱动通过pidfd_getfd复制同一flock open-file-description，独立
监控延续到第二代退出和最终GPU清理。第一次正常停止后，运行3个
真实loopback请求：无响应时取消、150ms慢滴绝对截止、后续正常请求。
第二代仅通过验证桥向封装service.run现有guard调用传入原lease_fd；
controller/worker/supervisor/backend仍为封装源码，不添加生产部署
参数。验证监控根明确切换为持有FD的驱动，限定自己的后代树；其他
负载和观察失败仍ENV_ABORT。原锁dev66305/inode3294067不变，不
重新获取独立锁，不在代际间释放。桥接边界不能称为原CLI多代部署验收。

第二代最多观察20秒，必须取得真实supervisor拥有的ESTABLISHED运维
连接、双监听身份以及未READY/DEGRADED的启动状态，再请求显式stop。
从请求至完整controller退出不超过5秒，仍只有1次attempt/0restart，
不得发布READY/DEGRADED。两个controller结束后共同确认进程、三监听、
GPU与原锁回收，保留10秒无复起观察。父控制最多80，监督器HTTP上界
216，业务4次生成/2次只读工具，TLS正常2代/配置拒绝2次，公共慢头136。
