# SRV-12l 托管截止与停止优先：限定交付

2026-11-04。生产8790c8e1，定向驱动03ee447b。
结论为`ACCEPT_SUPERVISOR_DEADLINES_SCOPED`：监督器的阶段绝对期限、
停止优先及可取消GET已接受；保留首次验证驱动的ENV_ABORT，不将分段
证据合成为一次连续成功的两代托管验收。

## 交付变化

- 启动和失去观测的期限覆盖整个探针及轮询；返回后、分类后再次检查，
  迟到READY不能清除超时或恢复健康状态。
- 显式stop在等待和探针返回时优先，不再发布随后READY/DEGRADED；
  真实内存/host故障保留失败状态。等待每100ms检查文件/信号停止。
- 运维GET可取消连接、发送和读取，EOF/解析后仍受同一单调截止约束；
  固定loopback、路由、凭据、大小和编码边界不变，没有新增重试。

新托管v6包：`.q4t-work/serve-supervisor-deadlines-20261104/backend-package`，
manifest `fcff7218de3b8616d641d3e8780077ca577d3ba9e012350a7723390031428240`。
与旧包51ccb951相比，仅封装的`tools/serve_service/supervisor.py`和
`tools/serve_ops/transport.py`变化；旧包/Key/三份day计划保持。接受入口
见同阶段`accepted-package.json`，未安装常驻服务或自动启动。

默认ELF `61abe09f`、sidecar `ce925d51`、协议 `15d7ccb1`保持，223项
运行来源逐项一致，不改模型数学、模板、缓存或调度。无需重新构建C++。
回退保留旧完整封装包
`.q4t-work/serve-startup-listeners-20261104/accepted-backend`；不要修改
已经固定身份的旧包来更新监督器，应显式选择新包。

## 验证及首次失败

原3个普通CPU子进程复现均失败：迟到首次READY、迟到恢复和stop后
READY发布。原代码/失败记录保存。修复后114项唯一CPU检查通过，
驱动6项、定向归因/清理3项通过；没有重复运行同身份114项。

首次正常生产CLI完整加载1模型，经两代TLS网关和两个客户端取得4个
SSE成功响应、2次真实只读资料工具。实际读取`docs/MODEL.md:1–24`，
两答案均含`Qwen4ExpForConditionalGeneration`及对应引用；512/64输出
预算核对通过。无错误/取消，grammar启动4/回收4。136个公共慢头占满
连接时，生产监督器的新READY样本、独立运维诊断及TLS发现正常；
实际capabilities与封装声明一致。2个错误运维上游配置均在child前拒绝。

这段生产guard为COMPLETED，60次观察、64.541067秒。随后原锁由
custody继续持有，三个无模型真实HTTP探针通过：

| 探针 | 实测耗时 | 结果 |
|---|---:|---|
| 无响应时请求取消 | 0.100376秒 | `observation_stopped` |
| 慢滴响应的150ms绝对期限 | 0.150095秒 | `http_deadline` |
| 后续正常请求 | 0.000415秒 | HTTP 200 |

首次第二代加载时，验证监控报ENV_ABORT。驱动把自身PID传入要求PGID
的监控接口，自己并非进程组长，导致新独立组不在初始所有权集合；
CPU复现此错位及正确单代根的行为，并确认外来/复用PID仍被拒绝。
原失败记录没有conflict列表，因此该归因来自源码、进程关系和隔离
复现，不冒充直接捕获了被误判PID。第二代自身guard为COMPLETED，
3次观察无竞争；整组custody的ENV_ABORT仍保持，不能记整轮成功。
首轮清理确认GPU/三监听/原锁释放，默认和恢复服务状态保持。

## 定向补验

仅补未完成的加载期停止，不再次生成上述4业务响应。使用同一封装包
和正常生产CLI、单代独立PGID，无验证桥或监控根切换。实际取得
supervisor拥有的ESTABLISHED运维连接、双监听身份、未READY状态后
显式停止，1.284830秒内整个controller退出；未发布READY/DEGRADED，
1attempt/0restart/0完整模型/0生成。停止后观察10秒，无复起。

此项guard/custody均COMPLETED，7次guard与3次custody重叠观察，
原锁连续持有到清理；记录覆盖6.955842秒，驱动含停机观察18.289054秒。
全阶段实际3次backend尝试、1次完整加载。首轮到ENV_ABORT的观察跨度
66.786330秒与补验6.955842秒合73.742173秒，属于两个分开的租约，
前者含失败，不是一次成功连续全流程或精确GPU占用时长。

最终14个登记自有身份均退出，三监听空、GPU计算上下文空；原锁
dev66305/inode3294067可获取，未删除/替换。容器空，恢复SOAR unit
仍masked/inactive/dead，MainPID 0。高级TLS/独立Key/CIDR/部署地址
按实际需要启用，本阶段只验本机可选组合，没有实际LAN部署或长验。

## 可核验证据与限制

冻结合同见[原范围](SERVE_SUPERVISOR_DEADLINES_2026-11-04.md)与
[定向补验](SERVE_SUPERVISOR_DEADLINES_RECHECK_2026-11-04.md)。
阶段原件在`.q4t-work/serve-supervisor-deadlines-20261104/`。
[机器结果](evidence/serve-supervisor-deadlines-20261104/result.json)及
[索引](evidence/serve-supervisor-deadlines-20261104/manifest.json)封存256份
记录、407个本机引用；压缩454175字节，SHA256
`6b30961ba6a50b5fad0354f26336bff263a0c5c447cf758f2cba53520991b451`。
新进程`python3 -B tools/serve_supervisor_deadlines/closeout.py verify`
独立重建通过；凭据和TLS私钥只保留本机，不在归档导出。

轮询不排除采样间隔内不可见干扰。加载期停止和业务质量各按实际
证据接受，不声称物理RAM硬保护、性能优化、长期稳定性或工业化全部
完成。小时/天级长验由用户执行，旧三份day均未运行。下一步复核
当前15d7ccb1协议的持久任务交接及剩余基础服务组合，持续目标active。
