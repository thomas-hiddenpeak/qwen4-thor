# SRV-15 托管服务低内存退场

## 缺口与范围

SRV-14的DEGRADED只表示准入关闭，模型仍驻留；持续低内存时，原监督器
不会主动回收它。新增独立于HTTP探测的全局MemAvailable监视器，只通知
当前owned后端，按既有进程组合同清理，保留失败原因且不使用重启预算。
本批是可靠性业务改进，不改变C++推理ELF、模型、模板或GPU数值路径，
不宣称性能提升。原始接受/失败记录不修改。

## 生产合同

- 新包v2默认critical_memory_mib=1024，可在prepare通过
  `--critical-memory-mib`显式设置1..1048576MiB；写入不可变manifest。
  不允许通过环境或CLI替换观测来源，只读固定/proc/meminfo。
- 启动前新读数缺失/非法或低于门限直接拒绝加载；后台每1秒读取，
  连续低于门限2秒触发critical_memory_pressure，连续未知5秒触发
  memory_observation_unavailable。低/未知交替5秒而未恢复正常读数，
  触发memory_safety_unconfirmed。只有有效且不低于门限才清除不安全期。
- 独立线程先锁存原因，使用boot/start/pidfd向所属后端发SIGTERM。
  监督器回收同组及孤儿worker；原因不被正常停止或信号恢复覆盖。
  active请求可能被取消，业务必须按已有结果不确定/幂等合同处理。
- 2GiB请求准入/512MiB恢复滞回保持原行为；暂时DEGRADED不自动杀服务。
  监视器低门限与请求准入相互独立，自定义高门限可能在准入仍打开时停止。
- terminal保持FAILED、guard保留COMMAND_FAILED；cleanup_confirmed单独
  表示实际资源清理结果。不得把保护性失败改写为操作者正常stop。
- v1旧包继续通过自身封存入口运行；新验证器保留其原库存与配置，
  不把旧包追认为已有内存退场能力。没有安装常驻或开机unit。

这是轮询/协作清理，不预留物理字节，不保证内核不可中断任务退出，
也不提供硬实时停止界限。其他进程或单次快速分配仍可能先耗尽RAM；
memcg对CUDA覆盖不足的实测结论保持，整机物理RAM硬保护仍待解决。

## 冻结验收

先成组运行原托管CPU合同、原8驱动及新增解析/计时/真实CPU子进程
保护停止、观测阻塞独立信号、退场验收反例。复用未变C++211来源、
GPU guard30及d66f2bb9既有协议/数值证据，不重建或重复完整模型矩阵。
核验实际旧v1封存包仍可通过新验证器，只读核验不执行旧服务。

实际GPU使用一个临时systemd unit、真实包内控制器/worker与同一共享
lease贯穿完整模型阶段；一个启动、四次生成（普通JSON、严格JSON、
实际MODEL资料读取工具与回填答案），一个真实低内存退场场景。
先检查空闲owner/slot/grammar，再用既有有界host压力助手降低真实
MemAvailable；不向生产入口注入假读数。验收包门限固定30GiB，压力
最多8GiB/128MiB一块，同时保持MemAvailable至少20GiB、MemFree至少
12GiB。任何安全下限、启动/业务/退场失败均保留现场并终止本轮。

GPU窗口220秒，总600秒；生成上限4、启动1、负例1，显式控制48含
清理8，监督GET保守上限188。锁忙/外部GPU竞争/监控失败ENV_ABORT；
只清理本轮owned进程。停服后10秒（两倍重启退避）仍应仅一次启动、
NRestarts=0、监听消失、GPU空、原锁可用；删除临时unit，不恢复vLLM。
原guard预计COMMAND_FAILED/child1，场景接受记录独立核对保护原因、
清理及外部资源，不将其改写COMPLETED。默认1GiB策略CPU证明、30GiB
实际策略证明分开报告；不能声称在真实1GiB余量/OOM边缘实测过。

产物：`.q4t-work/serve-memory-stop-20261103/`，源码提交后封存计划与包，
验证后独立接受记录绑定manifest SHA。默认runner/sidecar不变，旧已验
托管包保留为回退入口；需要回退时显式启动旧包新代次，不复活旧目录。
