# SRV-10b网关托管与生命周期短验结果

固定源码入口、依赖身份核对、停止排空、异常回收和有界日志已完成
开发，并在Thor本机TLS与真实模型上限定通过。冻结实现`f187ef8d`，
[原合同](SERVE_GATEWAY_MANAGED_2026-11-04.md)与失败原件保持。
[运行说明](../tools/serve_gateway/MANAGED.md)提供显式启动/停止入口。
实际LAN监听、证书信任、来源限制和常驻部署尚未验收；工业化目标
继续active，小时/天级验证由用户专门开展。

## 实现与证据范围

| 能力 | 实现与本次证据 | 仍未证明的范围 |
|---|---|---|
| 包与身份 | 包内固定nginx/Python身份/网关依赖源码；同包flock实际继承到nginx；运行目录独立 | manifest是可信本机控制面的身份锚点，不是签名认证；系统动态库仍依赖本机 |
| 持续监督 | 每秒核对后端controller/worker/backend、监听inode/ELF以及前端/配置身份；依赖停止即退场 | 轮询不能排除采样间隔内的替换，不隔离不可信同机用户；证书有效期仅准备/启动核验 |
| 正常停止 | 正式stop持久化意图、pidfd通知manager，SIGQUIT排空；实际活跃SSE完整成功 | 实际只覆盖2.715秒流；35秒上限及超时升级由CPU验证，超过上限的长请求可能中断 |
| 异常停止 | manager死亡触发nginx退场；master崩溃后回收孤儿worker；失联与FAILED/cleanup分别记录 | 失联不会伪造历史终态；未知自有身份需要只读调查，不能从遗留PID盲目清理 |
| 日志与unit | 每代次日志2×4MiB、事件2×256KiB；写失败退出；真实临时systemd单元/no restart | 轮转/日志故障为CPU证据，短验没有填满日志；跨代次磁盘总量、外送和告警接收未上线 |

原10a网关、默认runner及模型算法不变。本轮没有编译/安装新默认pair，
仍为`d66f2bb9`/`42d7672a`、protocol`55a3e67b`及211运行时来源。
nginx仍为官方签名包提取的1.30.6，未系统安装。模型后端复用已接受
v3包`160796c3`，同一PID 2488968/start191496165贯穿四个网关代次；
完整boot身份与四包SHA见[计划](evidence/serve-gateway-managed-20261104/plan.json)。
TLS仅`127.0.0.1:18444`，后端18123、seq1/8192/full-head MTP k3；
临时自签证书及两个随机Key仅用于本机，data每client2/全局4。

## CPU失败保留与修正

首轮18项中12通过/6失败。夹具错误传入`claim_fd=-1`，导致6项
未进入子进程路径；另1个startup负例仅检查失败，因同一准备错误
误通过。只修测试夹具为真实flock/FD，并要求正确的监听失败原因。
定向7项全通过、复用11项，最终18有效，实际累计25次执行。
生产实现未因该夹具问题修改，全部原输出、源码和私有目录保留。

覆盖真实子进程/孤儿回收、正常排空/截止升级、日志写失败/轮转、
锁竞争/symlink拒绝、幂等停止/PID身份/失联、后端身份、真实nginx
配置与隔离快照、systemd语法。另4项驱动协议及3个`-I`隔离入口
通过；裁切旧explicit_cancel wire只作CPU夹具，不冒充新断流实测。

## 真实模型与生命周期

全部一次完成，无自动重试：**1模型/4网关启动、8生成，6成功与2次
预期断流**，1次真实资料工具执行，3个grammar worker全部回收。

| 代次 | 触发和业务 | 终态及清理 |
|---|---|---|
| front-1 | 实际临时systemd单元；普通/strict/真实工具与引用4成功，第5个活跃SSE中调用正式stop | 流完整输出1..40、finish=stop和111 completion tokens；STOPPED/cleanup=true/nginx rc0；后端仍READY，重复stop幂等 |
| front-2 | 活跃SSE时只SIGKILL manager | nginx依靠parent-death自然退出，未发辅助清理信号；客户端拒绝缺失终态的流；OWNER_ABSENT/cleanup=false、无terminal原样保持，独立观察确认进程/监听/claim已释放 |
| front-3 | 活跃SSE时只SIGKILL nginx master | manager回收剩余worker，FAILED/cleanup=true/rc-9，restarts=0；客户端拒绝不完整流；同后端可继续工作 |
| front-4 | 普通请求成功恢复，再由驱动正式停止后端 | 依赖消失触发网关FAILED/cleanup=true/rc0；网关未对后端发信号，未重启或重放请求 |

front-1实际在systemd单元内通过生产`managed.py stop`排空，核对了
KillMode=mixed等属性；本批没有额外执行活跃请求中的`systemctl stop`
场景。后者信号路径依据已配置unit和CPU边界，不能混称同一实测。

两次断流HTTP均为200且客户端收到了EOF，但缺少成功终态/[DONE]，
客户端正确报ProtocolError，不能仅以200或EOF判成功。后端8条end
trace各出现一次且released=1：6 completed、2 client_disconnected。
最终metrics为8总/6成功/2aborted/0error，owner归零、slot=1，
grammar started=reaped=3；没有请求重放或额外模型启动。

普通及恢复响应均为710003，strict为`{"ok":true}`；真实
`read_project_record`读取docs/MODEL.md第1..24行，工具SHA/逐行文本
与源文件一致。模型回答Qwen4ExpForConditionalGeneration，并引用
`[docs/MODEL.md:L1-L24]`，不是模拟资料或跳过工具执行。

HTTP实际耗时：普通0.427秒、strict0.601秒、工具3.743秒、回填4.036秒、
排空流2.715秒、故障后恢复0.330秒。这里只记录有界业务观测，未形成
性能优化、五档E2E或吞吐非劣结论。

## 租约、封存与交接

GPU窗口72.009秒、总72.079秒，19显式控制；均低于360/420秒和128
控制冻结限额。监督GET保守上界300另列，不当作实际请求计数。
后端生产guard为COMPLETED、62记录/58观察、61.172秒，正常停止后
元数据写released；独立custody此前复制的是**同一个flock打开描述符**，
实际锁继续持有到最后网关退场、临时unit删除、10秒无复起与GPU空。
custody 30样本、COMPLETED/cleanup=true，原inode3294067未替换。
观察无竞争，不排除短于采样间隔的干扰；不是另取第二把锁或中间释放。

[408份原件目录](evidence/serve-gateway-managed-20261104/manifest.json)
封存7份Git文件795968字节；另47份私有材料/二进制仅记本机身份。
包括失败CPU、快照源码、完整wire、工具回填、进程与锁/监督记录。
已知API Key逐项扫描；私钥、私有policy/nginx.conf与syntax诊断不
入Git。故意构造的symlink拒绝夹具不跟随或复制，其目标内容保留；
故意篡改的CPU快照作为测试原件保留，不能当成可部署包。

```bash
python3 -B tools/serve_gateway/managed_report.py verify-archive
```

该入口只核对既有原件/身份，不启动模型或HTTP。后续证书自然过期
不否认历史TLS证据；再次运行必须经生产入口核验当时有效期。
终检本轮前后端进程全部退出、GPU空、原锁可用，容器空、SOAR恢复
服务仍masked/inactive，临时unit已删除；不承诺其他会话之后不使用GPU。
默认pair及10a来源不改；用户day计划只读检查通过，execution不存在。

下一步实际LAN地址/域名、证书路径与来源限制，以及通知接收、日志
跨代次留存、物理RAM硬保护仍在工业化账中待办。新默认小时/天级
证明必须等待用户长验；本轮不自动开启常驻服务或长测。
