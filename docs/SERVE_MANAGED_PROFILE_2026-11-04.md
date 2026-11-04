# SRV-12i：声明、独立运维与host资源组合短验

2026-11-04。业务可靠性验收，补齐SRV-19新包声明、SRV-12h独立运维
与SRV-02k资源域的交集；三项分别通过不能代替组合证明。本轮不改
模型实现、默认pair或旧包，不安装常驻服务，长期验证仍由用户执行。
高级部署配置仅在执行时显式选择；正式LAN参数不阻塞本轮本机短验。

## 冻结范围

- 从当前7eff020d/cc83bf3a默认pair新建v6包，同时选择独立运维18133、
  主端口18132、两个客户端及host high16GiB/max20GiB、pids512。
  control max512MiB/pids64，swap0；新包必须绑定九份发布声明副本。
  这些是本次短验配置，memcg不等于Thor全部物理RAM的硬保护。
- 当前生产controller/guard/worker与两代托管TLS网关（18452）运行。
  使用同一原flock，复制租约由已有custody保管至自有资源退场，
  竞争或监控失败记ENV_ABORT，只清理自身进程，不删除或替换原锁。
- 复用12h固定驱动的两类错误运维上游启动拒绝、136公共慢头满额下
  新监督样本READY/诊断HEALTHY/TLS发现、两代网关与双Key工具闭环。
  组合验收新增实时capabilities与包内声明逐字段一致性、实际host
  限额与control/payload成员检查；四个grammar子进程须观察到继承payload。
- 两客户端各2成功响应/1真实资料读取，alpha512/beta64输出预算，
  正确架构值与文档引用；同后端PID、grammar4/4回收、资源计数归零。
  先停网关再停后端，核对payload空、scope消失、双监听/GPU/原锁释放，
  10秒无复起。默认pair、222运行来源、原Key/旧包/三份day计划保持。

## 验证预算与证据

先运行新增组合CPU合同及驱动故障检查；复用未变来源的既有内核OOM、
pids/high/漂移等接受证据，不重新制造内存压力。随后至多1模型、
2成功网关启动、2拒绝网关启动、4生成尝试、80父控制、GPU240秒、
总300秒。监督器HTTP额外上界204、两次成功网关绑定共8调用另列。
资源采样每250ms，上限1024条/2MiB，只读本次scope。

失败原件保留，未通过不自动重跑整轮。生产源无变化时不重复构建、
五档性能或已验任务迁移；本轮没有性能优化、长稳、正式LAN或整机
物理RAM保护结论。完成后封存原件与精确包身份，独立离线核对。

入口为`tools/serve_managed_profile/stage.py`的`cpu/prepare/check/run`，
以及`report.py`的`postflight/archive/verify`。`run`从包内生产service
启动guard，不在外层再取第二把锁；准备和CPU通过均不代表实机接受。
