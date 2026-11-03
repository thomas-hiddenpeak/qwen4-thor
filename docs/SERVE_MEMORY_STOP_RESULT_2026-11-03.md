# SRV-15 托管低内存退场限定接受

2026-11-03：源码96e0d76a实际通过。原SOAR恢复服务仍持久masked/
inactive，临时验收unit已删除，GPU空、原锁inode3294067可用。
本批补齐持续临界内存的主动退场，未安装常驻服务。

## 改动与身份

原监督器在内存准入DEGRADED时保持模型驻留。新v2包增加独立线程读取
/proc/meminfo、启动前拒绝、持续低/未知锁存原因、owned pidfd通知及
进程组清理。保护性失败不允许自动重启，正常stop不能掩盖清理期间的
内存故障；FAILED与cleanup_confirmed分开表达故障和回收结果。

- 当前接受的托管包manifest：
  `dd94fb050aa23adad672cbf15935cb2c6d35bedf58468b086f39c42160dc030a`。
- 默认ELF仍为d66f2bb9，sidecar42d7672a，protocol55a3e67b；211来源
  未改，无需重建或重复完整数值/协议验收。实际MTP/full-head、seq1、
  8192上下文、2048 prefill、inflight8/queued2范围保持。
- 已接受旧v1包manifest71da1a1d仍能由新验证器核验；保留包内固定
  旧入口和既有证据。回退应显式使用旧包和新运行目录，不复活旧代次。
- 包manifest仍保留prepare时managed_service_accepted=false；独立
  [接受记录](evidence/serve-memory-stop-20261103/accepted_package.json)
  绑定准确manifest与本轮结果，不重写准备声明。

## 验证结果

CPU首轮50通过；审查后补上清理末尾保护原因检查及一个竞态测试，
仅重跑受影响34项，复用未变17项，最终51有效通过。包括实际CPU
子进程退出、HTTP等价探测阻塞时独立通知、临界/未知/交替读数、
暂时恢复、启动拒绝、幂等信号及禁止错误接受ENV_ABORT/操作者stop。
原guard30的实现依赖未变，直接复用；实际旧v1包兼容核验通过。

实际GPU在包内生产控制器持有的同一共享lease中完成：

| 项目 | 实际结果 |
|---|---|
| 模型启动/生成 | 1次启动，4次请求全部成功 |
| 业务质量 | 普通JSON、严格JSON、真实MODEL资料工具读取及回填答案通过 |
| 空闲资源 | 4 success/0 error，grammar3/3回收，owner归零、slot1/1空闲 |
| 真实压力 | 2.25GiB匿名host驻留，52条压力记录；固定30GiB停止门限 |
| 安全余量 | 记录中最小MemAvailable 31,964,168,192字节、MemFree 13,345,083,392字节，均高于20/12GiB安全下限 |
| 保护停止 | critical_memory_pressure，54次watchdog采样、明确发SIGTERM；完整回收，无模型替换 |
| 禁止复起 | 即使restart_limit=3，restarts=0；10秒观察后attempts1/NRestarts0、无监听/GPU进程 |
| 窗口与观测 | GPU56.118秒、总66.813秒；15显式控制、53次guard观测；监督GET上限188，合计保守203 |
| 收尾 | 压力助手allocated0/error:null，临时unit删除，锁inode保持可取得，SOAR仍masked/inactive |

原始worker/terminal保持FAILED、guard保持COMMAND_FAILED/child1，
这是冻结矩阵要求的保护性停止；两个cleanup_confirmed均为true。
没有把此失败终态改写为COMPLETED。独立场景结果为
[PASS_MANAGED_MEMORY_STOP](evidence/serve-memory-stop-20261103/result.json)，
完整原始终态见[terminal](evidence/serve-memory-stop-20261103/terminal.json)。
本轮没有ENV_ABORT、额外模型重试或扩大原次数/时间预算。

## 证据、复用与边界

80份原件无损封存、1份本机ELF单列身份，共9份Git证据文件约140KB。
[manifest](evidence/serve-memory-stop-20261103/manifest.json)记录原件、
摘要及本机产物；可运行`python3 -B tools/serve_service/memory_closeout.py verify`
再次只读核验，不启动模型/HTTP。最终内存观察MemAvailable
125,079,724,032字节（约116.49GiB）；
[postflight](evidence/serve-memory-stop-20261103/postflight.json)保存原锁、
恢复服务状态与GPU证据身份。默认runner/sidecar没有切换。

生产默认1GiB停止门限由CPU时序与配置合同验证，实际GPU采用显式
30GiB安全门限；不宣称真实1GiB/OOM边缘验证过。启动/未知读数的
保护拒绝和正在阻塞探测的独立通知属于CPU证据，本轮实际GPU在
业务完成后空闲退场，不扩大为GPU活跃输出压力取消证明。

监视器是轮询和协作清理，不预留字节，不保证硬实时或内核不可中断
任务退出。整机物理RAM硬保护、新ELF小时/天级长稳、实际访问/通知/
多人策略、独立控制器死亡的实际GPU边界仍需继续推进；当前工业化
持续目标未完成。
