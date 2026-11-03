# 当前文本发布的受控托管

入口：`service.py`。本机CPU合同及真实托管/MTP工具、systemd恢复与停止
已[限定通过](../../docs/SERVE_MANAGED_SERVICE_RESULT_2026-11-03.md)。
当前默认二进制和已接受业务范围见[STATUS](../../docs/STATUS.md)。

从项目根目录准备独立包，不加载模型：

```bash
/usr/bin/python3 -I -B tools/serve_service/service.py prepare \
  --model-dir /home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --output .q4t-work/managed/text-package --port 8080
```

输出包含`package`和`manifest_sha256`，后续命令必须使用该摘要。
包包括ELF、匹配sidecar/cache、原接受记录和实际运行源码副本；模型
目录只读，只重新核对JSON元数据，没有重算全部权重shard摘要。
准备包不会把托管入口自动标为业务验收通过，摘要也不是签名认证。

实际运行应使用身份与配置匹配的托管证据；本次已验包和绑定manifest
SHA的伴随接受记录见上述结果页。prepare本身的未验声明不被改写，
不能把新准备的其他配置自动视为本次接受。启动新代次示例：

```bash
/usr/bin/python3 -I -B tools/serve_service/service.py run \
  --package .q4t-work/managed/text-package \
  --manifest-sha256 PREPARE返回的摘要 \
  --output .q4t-work/managed/run-001
```

它转入包内代码，以前台方式持有仓库共享GPU锁和监控。每个运行目录
只能使用一次；失败、停止、残留记录都不能通过重跑旧命令覆盖。
新代次要用新目录。锁竞争、未清恢复标记、监控异常拒绝执行，不修改
外部服务；锁文件永不删除。锁描述符贯穿控制器、worker及后端，两个
父子边界设置跨exec的parent-death信号。控制器死亡时后端退出前锁
仍保持；这不保证内核不可中断任务能够退出，也不证明物理RAM硬上限。

```bash
/usr/bin/python3 -I -B tools/serve_service/service.py status \
  --run .q4t-work/managed/run-001
/usr/bin/python3 -I -B tools/serve_service/service.py stop \
  --run .q4t-work/managed/run-001
```

`stop`先持久记录意图，按boot ID/start ticks/pidfd通知当前自有worker。
返回`STOP_REQUESTED`仅代表请求停止；之后`status`的终态与
`cleanup_confirmed`才说明控制器观察到清理完成。`OWNER_ABSENT`不等于
GPU已清理，应查单元/cgroup与NVML，不按旧PID或进程名kill。
运行目录留下terminal，不提供自动复活或恢复旧请求的入口。

默认重启次数为0；`prepare --restart-limit 1..3`显式允许恢复已ready、
被信号终止的后端。总次数不随时间重置。普通非零退出、GPU unhealthy、
身份/有效配置变化、启动失败或持续观测失效都会停服；旧进程组回收
完成后才允许替换。恢复服务不会重放HTTP请求或任何工具执行。
内存准入关闭是DEGRADED，正常忙不被当成无进度故障。新v2包另有
独立内存监视器：默认1GiB停止门限（prepare可用
`--critical-memory-mib`设置），启动前未知/低内存拒绝加载；运行时
每秒检查，持续低于门限2秒或观测未知5秒即通知owned后端退出并清理，
低/未知交替不能无限延长。保护性停止保留FAILED及具体原因，不自动
重启；cleanup_confirmed与失败状态分开记录。它是轮询保护，不能当作
统一物理RAM硬限额；详见[合同与验收](../../docs/SERVE_MEMORY_STOP_2026-11-03.md)。
旧v1封存包保留旧行为及旧证据，新包的实际接受范围需另查对应结果。

`unit`接收与`run`相同参数，只把unit文本写到stdout；它不安装服务。
模板使用包内固定入口、`Restart=no`和`KillMode=control-group`，没有
Install段。运行目录同样只能使用一次。systemd直接发送SIGTERM时，
原guard会保留ENV_ABORT中断记录，终态另列操作者停止意图与清理结果；
竞争/监控错误不能因此被归为正常停止。未配置开机启动、TLS、鉴权、
网关、通知或多人配额。

每代次原始backend日志2×4MiB、事件2×256KiB、guard事件与NVML各
2×4MiB；轮转总上限24.5MiB（元数据另计）。轮转会丢弃旧日志，字节
截断可切开一条记录，不能当作完整历史证据；私有状态快照另存为完整
JSON。写日志失败会停服。不同运行目录/发布包不会自动清除，因此
该上限不等于磁盘整体上限。验收证据另行封存，不放进在线轮转日志。

开发检查（无模型、无HTTP）：

```bash
/usr/bin/python3 -B -m unittest discover -s tools/serve_service -p test_service.py -v
/usr/bin/python3 -B -m unittest discover -s tools/evalscope -p test_gpu_exclusive.py -v
```

首个配置固定为full-head MTP k3、seq1、8192上下文、2048 prefill、
inflight8/queued2、2GiB内存准入/启动余量与512MiB恢复滞回。配置与
当前已验HTTP与实际托管结果保持这一范围。
其他上下文、媒体或代理入口应另有明确配置和适用验收。
