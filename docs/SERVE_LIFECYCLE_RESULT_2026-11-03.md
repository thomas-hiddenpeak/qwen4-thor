# SRV-13 较长运行、退出重启与发布回退结果

限定接受并交付停机修复。默认程序为c597adc3、sidecar a350c978，
protocol f79cf822，完整head/MTP k3。原小时段在旧3cd3994d上通过，
随后发现排队请求停机分类错误；原整体FAILED不改判，修复后只补验
失败和未执行的流程。新ELF没有重跑一小时，明确复用未变正常路径
的旧证据。工业化持续目标仍active。

- 原冻结：05d899841719acf7138b3f38ebd4ef97d2da1c1f。
- 修复冻结：279c9abb0d0572f1d706ebf6161d0ee08f548d2d。
- [原合同](SERVE_LIFECYCLE_2026-11-03.md)、
  [修复合同](SERVE_LIFECYCLE_REPAIR_2026-11-03.md)、
  [完整摘要](evidence/serve-lifecycle-20261103/correction/acceptance-summary.json)、
  [原记录归档](evidence/serve-lifecycle-20261103/README.md)。

## 发现与修复

原AllocSeqId在seq_stopping时返回-1，handler随即发通用503错误。
已经提交SSE头的排队strict请求因此得到service_unavailable，活动
请求则正确得到server_shutdown。两条流都有错误与DONE，进程退出0，
worker和scope均已清理；这不是尾帧丢失或泄漏。Registration析构日志
显示cancelled，不能代替count_abort的取消统计。

唯一运行时修改是chat_admission.cpp的停止分支：设置kShutdown并
返回已有取消路径-2，复用响应终态、250ms终态写截止和取消计数。
保持首个已经发生的取消原因，不改正常slot选择、模型、分词、模板、
grammar或MTP。原生测试覆盖真实排队停止、首因保持、空槽不被占用、
队列回收；实际日志确认两条修复请求各执行一次count_abort。

## 实际验收

| 阶段 | 实际业务与退出 | 结果 |
|---|---|---|
| 旧A同PID小时段 | 240周期，每周期普通JSON/SSE、strict、真实MODEL读取与引用回填；1200成功生成、240预检 | 3600.000秒；每轮owner归零、worker平衡、slot1/1free；固定包络通过 |
| 旧A首次SIGTERM | 活动普通SSE与已准备strict排队；退出0/0.643秒 | 活动分类正确、排队误报；整体FAILED和guard COMMAND_FAILED原样保留 |
| 修复C定向SIGTERM | 同类两个在途请求；退出0/0.642秒 | 两条完整server_shutdown错误流，各一次取消，worker在scope清理前已退出 |
| C正常重启及SIGKILL | 旧ID取消404、普通与真实工具成功，再同时中断活动与排队请求 | 退出-9/0.402秒；两份journal只读判REQUEST_OUTCOME_UNKNOWN，不接受部分答案、不自动重放；worker在scope清理前已退出 |
| C崩溃后重启 | 旧ID404、普通与真实工具闭环 | 3次成功，worker2/2，正常退出 |
| 默认A→旧B→A | 实际安装84d16983/c5203331，按v3规则/配置诊断，普通及真实工具闭环 | 3次成功，worker2/2，正常退出并恢复A |
| 安装C健康检查 | 实际build/q4t运行，普通及真实资料工具闭环 | 3次成功，worker2/2，正常退出；交付C并保留A回退包 |

累计1218生成尝试=1212成功+原关停2（其中排队分类错误）+修复关停2+
崩溃未知2；另4负响应、240预检、6模型启动、244次真实资料工具执行。
原小时段每15秒开始一轮，非饱和吞吐测试。原与修复两个阶段各自全程
持GPU lease；原阶段失败即停止，补验是明确修复后的独立完整驱动。
补验guard COMPLETED，两个阶段ENV_ABORT/竞争均0，无自动重试/请求重放。

仍计入第一次lease起算的硬预算：1224生成/8负例/7启动/240预检/
5000控制观察/6000秒；累计控制观察1839，finalize时4610.080秒，未扩预算。
补验候选业务164.904秒、安装健康42.920秒。无性能非劣或五档优化接受声明。

成组CPU：零警告生产构建、host242、必需192、驱动24；锁内原生24通过。
防漂移清单与生成合同先拒绝新源码，复核停止准入语义后只重验两门通过，
原失败保持。准备时曾引用不存在的SHA256SUMS，改为真实sealed-files.json；
当时尚未构建/启动模型，无伪造receipt或修改旧记录。

## 资源、诊断与证据边界

小时段前16轮为warmup，后224轮静止样本相对基线最高RSS增长9.160MiB、
memcg anon增长9.180MiB，FD49/threads5不变；低于预先冻结的64/128MiB、
FD+2/threads+2包络。小时段3600份连续样本，加补验201份共3801份。
进程RSS、cgroup和全局MemAvailable分别记录，不相加或用cgroup小读数
声称限制了全部CUDA统一内存。有限观察不证明无限期无泄漏或物理峰值。

14份实际诊断均健康且身份匹配；同实例计数增量有效，重启/版本变化
明确拒绝跨实例比较。v3/v4规则只选取核验身份，未部署常驻监控。
崩溃请求没有parent完成日志；其worker退出由PID/start-time独立观察，
不把scope最终清理冒充parent reap或请求成功。

1218条真实wire、5022个完整封套和4拒绝响应离线复核通过；原错误
分类仍标原失败，两个崩溃前缀明确不完整。10321份原记录无损封存为
100个Git文件约13.56MB，12个本机ELF/bundle等身份另留清单。完整日志、
wire、连续采样和原失败都在归档中；整个归档不称已脱敏。
[Git逐字节/原件与解压核对](evidence/serve-lifecycle-custody-20261103.json)
单独留存，不把压缩文件存在当作原件一致证明。

## 发布与回退

- 当前binary：`c597adc38e202bb810a2b7ace4314618bd504e76eff968bf9ea0103f4078a344`。
- 当前sidecar：`a350c9782c42bd2008026b35e61ed3d0acadc5e254a746caa6ee53743322fca9`。
- protocol：`f79cf82274e4e3df0732f09e8362b1145c207ced1bd53a783a711b05aca3b589`。
- 直接上一版A：3cd3994d/bb620240；其上一版B：84d16983/c5203331。
- 包：`.q4t-work/serve-lifecycle-repair-20261103/package/`；
  原失败：`.q4t-work/serve-lifecycle-20261103/execution/`；
  补验：`.q4t-work/serve-lifecycle-repair-20261103/execution/`。
- 已核验独立回退bundle：
  `.q4t-work/serve-lifecycle-repair-20261103/rollback-bundle/`，current=C、
  previous=A，各自ELF/sidecar/cache/v4规则完整。C→A实机往返未另测；
  本次实际操作是A→B→A、随后安装C并真实验证。

回退使用[发布工具](../tools/serve_lifecycle/README.md)的switch，target
previous为A，current为C；必须完整GPU lease且服务已停止。中断文件
替换留下匹配marker时用recover回到current；未知身份拒绝覆盖，工具
不自动启动服务、不重放业务。仅逐文件原子替换，不宣称双文件跨掉电事务。
只读发布清单现为38个匹配pair，原10月18日stale历史边界仍是不可安装
归档端点，不被当作第39个可安装pair或此次新增损坏。

13:50:33 CST收尾：全部owned模型/worker/scope退出，GPU PID为空，
共享锁可用、无恢复标记，MemAvailable116.527GiB。默认文件已更新，
没有留下服务占用GPU。下一优先项是启动可用内存检查和Thor统一内存
计量边界；实际访问/通知/多人策略、天级稳定与通用任务恢复继续待办。
