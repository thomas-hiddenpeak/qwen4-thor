# 可选部署检查交付，内存退场组合未接受

2026-11-05。离线部署profile检查工具可以交付；本批完整资源组合结论
为 **NO_GO_MEMORY_STOP_COMBINATION**。原4条完整TLS响应、2次真实资料
工具通过；用户授权的额外1次启动已执行，但压力先达到夹具安全边界，
未触发服务内存退场。两轮FAILED均保留，不再扩展启动或压力预算。

## 本轮交付与使用边界

[部署工具](../tools/serve_deploy/README.md)固定后端、可选TLS网关及回退
组合的manifest，检查端口、客户端策略、显式CA和来源/解释器身份。
prepare/check不启动服务；工具校验通过不代表选中包已完成业务验收。
当前继续使用[已接受的托管包41dc8fdb](SERVE_CURRENT_MANAGED_RESULT_2026-11-05.md)。
本批带host域和30GiB退场门限的c1653470包保留为未完整接受的候选，
不写accepted-package收据，不替换默认或安装常驻服务。

原生默认a56bd448、sidecar914e067f、协议cc321239及223运行来源保持。
profile摘要为6532435b，候选完整manifest与回退引用见下方机器结果。
旧包、客户端Key及三份用户day计划保持；无性能优化或长稳结论。

## 两轮原始结果

| 阶段 | 实际工作 | 退出与可用证据 |
|---|---|---|
| 原执行 | 1模型、4完整响应、2真实资料工具；71.538秒 | CUDA扩展指标被运维白名单过滤，驱动KeyError；原始metrics正常，生产实现未改 |
| 授权补验 | 1模型、2生成尝试、4控制请求；57.261秒 | 活跃内容与已准备grammar排队均成立；压力工具拒绝下一块分配，保护退场未触发 |

第一轮alpha/beta均读取真实docs/MODEL.md并回答架构，引用、实际wire和
512/64输出预算独立核对。原13项CPU证据按未变实现复用，补验前4项
定向驱动检查通过（含3项复用测试，不能算17个唯一用例）。本次新增
8项离线报告回归通过：拒绝伪成功、伪内存信号、篡改失败原因、截断流
伪终态及凭据/PEM泄露。没有因报告或文档修改再启动模型。

补验加压前MemAvailable约32.378GiB、MemFree约14.211GiB。每次128MiB，
最大实际加压2.125GiB；末次观察MemAvailable为30.258GiB，仍高于30GiB
退场门限；MemFree为12.090GiB，已不足再分配128MiB并保留12GiB。
工具按约拒绝并全部释放，未降低安全余量、修改生产内存读数或制造OOM。
因此没有保护原因、server_shutdown终态或退场时延的新增通过证据。

失败清理先关网关，捕获到的活跃/排队SSE分别只有29/1个envelope，
均缺少完整终态；客户端应判REQUEST_OUTCOME_UNKNOWN。原生后端记录
活跃请求client_disconnected、排队请求completed，不能据后者把客户端
截断流算成功，也不能将两条截断流视为预期的内存退场响应。

## 清理、封存和停止条件

两轮分别由生产CLI取得原共享GPU锁，custody保管同一flock至owned
清理完成；guard/custody均COMPLETED。累计受监控锁106.274秒、驱动
128.798秒，共2启动/6生成尝试/7控制请求，没有第三次启动。轮询无
竞争观察，不声称排除短于采样间隔的干扰。

5个grammar进程回收，host域无OOM/high/max事件；补验host峰值约
15.714GiB，payload/control域和监听均释放，原锁dev66305/inode3294067
AVAILABLE，GPU计算进程空。两轮均0重启/重放，末尾10秒未复起；SOAR
恢复单元仍masked/inactive、MainPID0，容器空。不是长期无复起证明。

169份封存记录由独立进程重建核对，结果固定为未接受。运行后先核对
1887工具/文档来源、223原生来源及143原轮引用，再修改仅离线report；
运行时来源快照、两次失败和原计划不改。报告修正PEM扫描对源码字面量
的误判并支持实际NO_GO取证；后续加强CPU/生命周期断言只影响离线审核。

- [机器结果](evidence/serve-bounded-deploy-20261105/result.json)、
  [逐项摘要](evidence/serve-bounded-deploy-20261105/manifest.json)、
  [原件归档](evidence/serve-bounded-deploy-20261105/records.tar.gz)、
  [8项离线检查与最终审核器身份](evidence/serve-bounded-deploy-20261105/offline-checks.json)。
- 独立复核：`python3 -B tools/serve_deploy/report.py verify`；检查通过表示
  记录一致，返回的业务结论仍是NO_GO，不能作为部署接受收据。
- 本机原件：`.q4t-work/serve-bounded-deploy-20261105/`；补验位于
  `continuation/`，结论写入`bounded-outcome.json`，不覆盖原FAILED。

这项组合验收保留为一条明确限制，不继续自动补跑。若业务需要重新
开启，先离线评估同时满足MemAvailable触发窗、MemFree安全余量和请求
持续时间的固定夹具，再冻结新预算；不降低安全下限或复用旧run目录。
下一项独立开发为可选单进程RPM/TPM与客户端准入公平；用户负责长稳，
实际LAN/正式通知按启用时验收。[物理RAM结论](SERVE_PHYSICAL_RAM_DECISION_2026-11-05.md)
仍是NO_GO_CURRENT_CONTRACT，不作整机硬上限承诺。
