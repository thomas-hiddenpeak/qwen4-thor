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
独立前台controller SIGKILL已[实际限定验证](../../docs/SERVE_CONTROLLER_DEATH_RESULT_2026-11-03.md)：
活跃输出与准备完成的grammar排队请求均完整server_shutdown，4个
grammar回收，无辅助清理信号，10秒无重启。验收额外保留同一flock
副本并独立监控到结束；原controller终态缺失保持，不伪造其清理确认。

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
同样，控制器被SIGKILL后锁文件可能保留历史held；真实占用以flock、
进程身份和GPU观测为准，不能为消除旧文本而删除锁文件。
运行目录留下terminal，不提供自动复活或恢复旧请求的入口。

失联后的现场核验使用当前入口（旧包可从工作区入口核验）：

```bash
/usr/bin/python3 -I -B tools/serve_service/service.py inspect \
  --run .q4t-work/managed/run-001
```

它分别输出历史`history`、实际进程/监听/GPU/flock与恢复标记；
`cleanup_observed`只代表本次完整观察下无残留，不改历史
`cleanup_confirmed`。完整观察exit0（包含BUSY），缺证/变化exit2。
当前v3包记录UID和命名空间范围；旧运行缺该信息时保持UNKNOWN，
但仍列出各资源的现场结果。空闲锁会在查询期间临时持有，结束释放；
不写记录、不发送信号、不连HTTP。详见[核验合同](../../docs/SERVE_INSPECTION_2026-11-03.md)。
当前v3已[实际限定接受](../../docs/SERVE_INSPECTION_RESULT_2026-11-03.md)：
51项CPU、1启动/3成功真实业务；live/stopped完整观察和旧记录保留通过。

默认重启次数为0；`prepare --restart-limit 1..3`显式允许恢复已ready、
被信号终止的后端。总次数不随时间重置。普通非零退出、GPU unhealthy、
身份/有效配置变化、启动失败或持续观测失效都会停服；旧进程组回收
完成后才允许替换。恢复服务不会重放HTTP请求或任何工具执行。
内存准入关闭是DEGRADED，正常忙不被当成无进度故障。v2及后续包另有
独立内存监视器：默认1GiB停止门限（prepare可用
`--critical-memory-mib`设置），启动前未知/低内存拒绝加载；运行时
每秒检查，持续低于门限2秒或观测未知5秒即通知owned后端退出并清理，
低/未知交替不能无限延长。保护性停止保留FAILED及具体原因，不自动
重启；cleanup_confirmed与失败状态分开记录。它是轮询保护，不能当作
统一物理RAM硬限额；详见[合同与验收](../../docs/SERVE_MEMORY_STOP_2026-11-03.md)。
旧v1封存包保留旧行为及旧证据，新包的实际接受范围需另查对应结果。
v2目前[已限定接受](../../docs/SERVE_MEMORY_STOP_RESULT_2026-11-03.md)：
51有效CPU合同，实际4成功请求/真实资料工具，显式30GiB门限下
2.25GiB有界压力触发停止、资源回收和禁重启。生产默认1GiB门限
未在真实OOM边缘测试，保持CPU与高门限实测的证据区分。

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


## 显式后端host资源域（v5）

`prepare`同时指定`--host-memory-high-mib`、`--host-memory-max-mib`和
`--host-tasks-max`启用host memcg/pids保护，可与`--client-policy`组合。
三个参数必须完整明确；默认不改变已有v1-v4包。实现及当前验收状态见
[SRV-02k](../../docs/SERVE_HOST_ENVELOPE_2026-11-04.md)。该配置已[通过内核与真实短验](../../docs/SERVE_HOST_ENVELOPE_RESULT_2026-11-04.md)。
16/20GiB与512任务仅是已验配置，不是所有业务的推荐容量。

依赖本机systemd user manager及memory/pids委派。包固定busctl和
systemctl身份；工具升级后需显式重新准备包。每代次创建唯一临时scope，
监督器在control、后端和子进程在payload；GPU锁控制器在该scope外。
payload禁swap、OOM按组终止，pids同时计线程；OOM/任务额度耗尽/
限额漂移/观测失效都会停服且不自动重启，即使设置了restart-limit。

运行目录`resources.json`记录配置和scope身份，`resource-backend-*.json`
记录exec前实际加入，status中`host_resources`为最近观测，
`resources-final.json`记录退出计量和payload移除，terminal另确认scope
退场；异常缺失不补造。全局MemAvailable监视器与CUDA申请额度仍有
独立作用。memcg未完整覆盖Thor CUDA或历史文件缓存，不与CUDA额度
简单相加，也不表示整个统一物理RAM有硬上限；同UID恶意迁移不在沙箱承诺内。

此入口不安装开机服务；旧包/旧day计划不会自动迁移。v5后端搭配新的
托管网关v3快照，旧网关源码不认识v5时会拒绝绑定。实际LAN监听地址、
CA/DNS和网段需另有明确配置；本次只做loopback TLS验收。
