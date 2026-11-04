# SRV-14 托管机制CPU结果

2026-11-03：**开发和CPU合同通过，真实托管GPU/HTTP验收待执行**。
当前默认仍为280b95dc/sidecar18c9d2c5、protocol29810d03；211运行时
来源逐项未变。不把既有模型接受记录当成新监督器的实际生命周期证明。

入口为[serve_service](../tools/serve_service/README.md)。它准备独立的
已交付ELF/sidecar/cache/原接受记录和源码包，工作树后续变化不会悄悄
改变包内逻辑。运行目录只允许创建一次；显式停止记录不可复活。
默认不重启；明确配置后最多3次，且仅限ready后被信号终止的后端。
非零正常退出、GPU故障、身份或有效配置变化、启动/观测截止均停服。
低内存准入关闭保持DEGRADED及同PID，不复用旧healthz503重启判断。

共享guard新增两个**可选**能力：长期服务的两代有界监控日志，和
继承同一flock描述符。普通验收入口默认仍保存完整日志且不继承fd。
托管worker与backend均设置parent-death信号；CPU用真实SIGKILL、
exec和孤儿回收验证，控制器退出后子进程仍持锁，子进程退出才释放。
这不是物理RAM保护，也不保证内核不可中断任务能被杀掉。

## 本机检查

| 项目 | 结果与范围 |
|---|---|
| 托管合同 | 最终29项通过；首次24，随后补锁继承/退出/配置等5项。实际子进程/孤儿、停机优先、重启预算、观测截止、GPU/配置故障、日志写失败、包篡改和systemd语法 |
| 共享GPU guard回归 | 30项通过；继承/轮转分支另在上方真实CPU进程测试覆盖，无NVML/GPU测试载荷 |
| 运行包隔离入口 | 从包内用Python isolated模式生成unit并通过本机systemd255语法检查；没有安装或启动unit |
| 配置对照 | 首个profile与02h+02i的实际HTTP配置原件逐项一致：full-head MTP、seq1、8192/2048、8/2、2048MiB floor及512MiB恢复滞回 |
| 源码/默认 | 211运行时源与已交付ELF/sidecar保持；没有重建或替换默认程序 |
| 新模型/HTTP | 0次；真实托管验收仍待办 |

测试期间的中间通过日志全部保留，不把重复运行次数相加成独立覆盖。
最终29/30两组分别约2.21/1.12秒。固定日志写入100字节到31字节额度
实际得到当前7字节/上一代31字节；完整日志默认仍保留100字节。
缺磁盘写入故障可见，子进程组被回收；状态失败不伪装为正常停止。
同时验证操作者停止不能掩盖竞争或监控异常的ENV_ABORT。

## 身份与证据

- CPU包manifest：`e33a0ac2771d450a380c2b49eed8d6a5221b5e67c6b10ea6c70eca5199de5787`。
- 本机包：`.q4t-work/serve-managed-service-20261103/package-cpu`。
- [最终CPU记录](evidence/serve-managed-service-cpu-20261103/result.json)
  保存测试代码、运行源码、默认pair、配置原件和unit摘要。
- [证据目录清单](evidence/serve-managed-service-cpu-20261103/archive.json)
  封存10份原件30,670字节，另有清单本身；包含全部中间日志、包manifest
  和未执行unit。包内ELF/cache副本保留本机，按manifest核对，不以摘要
  代替本机真实文件的存在。
- [收尾环境快照](evidence/serve-managed-service-environment-20261103.json)：
  NVML正常且GPU进程为空，共享原锁可取得，SOAR仍masked/inactive。

下一步冻结具体实际驱动，在3启动/8生成/8负场景/240控制/1200秒上限
内验证当前MTP、真实资料工具、同一lease内的受控重启和显式停止。
实际systemd单元与前台运行分别列证；当前没有常驻、自动启动或通知。
工业化总账中的物理RAM硬保护、实际网络/多人、长稳和通用任务恢复
继续保留，不以本次CPU通过关闭。
