# 原生服务入口解耦：有界接受与交付

2026-11-08。结论：`ACCEPT_BOUNDED_NATIVE_INGRESS`。
默认服务封装已移除nginx强制依赖。独立C++入口提供Bearer鉴权、
恢复期准入及JSON/SSE转发，Guardian管理模型、控制器和入口恢复。
本机新入口已安装，保持STOPPED；默认推理程序60b1c4c1/d2ae8db2未改。
实现提交`0fd6deef`，在`codex/serve-self-recovery`与PR #22交付，
不表示已经合入main。范围按[冻结计划](SERVE_NATIVE_INGRESS_PLAN_2026-11-08.md)。

## 默认能力与依赖

- 新v2包只携带自有`q4t-ingress`、`q4t-guardian`、独立CUDA探测及
  已验收引擎；准备、启动和恢复均无需nginx或systemd。原生入口只
  链接系统C/C++运行库。控制器、journal和入口工作进程仍使用固定
  Python；推理所需CUDA、驱动、模型和系统库继续保留。
- 原生入口验证Bearer、Host和实际对端CIDR，拒绝公共内部身份头，
  后端继续执行独立客户端RPM/TPM与公平准入。每次请求读体前及
  转发前检查当前Guardian准入，两次绑定必须相同；每代模型自检和
  完整分钟配额窗口结束后才开放，不重放POST、工具或中断SSE。
- 入口与模型分进程：模型/控制器恢复期间入口继续鉴权并返回
  503/Retry-After；入口恢复保留健康模型。认证后的`/livez`表示
  入口存活，`/readyz`受真实业务准入约束。容量、请求体、超时和
  控制通道边界见[README](../README.md#原生服务入口与可选前置)。
- 可选nginx前置只使用普通HTTP和Bearer，无私有准入子请求；TLS
  由部署方按需前置。CPU互操作覆盖JSON/SSE及前置退出后直接入口
  仍可用。原生入口本轮只监听loopback，未接受实际TLS/LAN部署。
- 未移植nginx源码，复用q4t自有HTTP/JSON/进程代码。README已明确
  来源；未来实际借鉴第三方代码时遵守固定版本许可并保留必要声明。

## 验证结果

本阶段151项不同CPU检查通过：HTTP读取10、原生入口17、准入竞态4、
封装7、网关政策14、生产依赖22、显式旧nginx6、可选普通前置3、
验收驱动8、Guardian进程18/journal7/生命周期35。原生目标零警告；
来源审计163锚点、38个Chat字段、10个后端/8个入口路由及4个负控通过。

封包复核发现CIDR检查错误地把监听地址当作客户端来源。旧1b67761f
的负控确实将127.0.0.2放行；新4a499f1a改为核对实际accept对端，
17项入口回归通过。负控失败原件保留，16个重复用例不算新增覆盖。
新Guardian/探测ELF与旧包不同，已针对实际构建补CPU及真实探测；
其他未改行为按来源一致复用，未重复整套历史推理矩阵。

真实业务在**同一严格GPU租约**内一次通过：3次模型启动、18条Chat、
1次独立CUDA探测、655次控制操作，驱动191.673秒、完整租约192.691秒。
全部生成自检、拒绝与中断Chat计入，未超过4/20/1800秒上限。
计划的19条为保守预估，实际逐请求账本为18，不补发无意义请求。
外层179次NVML采集全部OK，退出计算PID为空。

| 场景 | 实际结果 |
|---|---|
| 三个模型代次的JSON/SSE | `17+25=42`，终态/usage/请求身份重新解析通过 |
| 三次严格工具闭环 | 实际读取`docs/MODEL.md`前16行，每次执行一次，回填后返回精确引用 |
| 辅助依赖失能 | 内层注入NVML timeout及日志ENOSPC、无metrics轮询；健康模型同PID继续业务 |
| 模型在SSE中退出 | 旧流不补造DONE；入口503/坏Key401；一次CUDA探测后按预算恢复 |
| 控制器退出 | 原有模型及后代清空，恢复控制器和模型；入口与Guardian保持 |
| 入口工作进程退出 | 新原生入口重绑，健康模型和控制器PID不变，后续工具闭环通过 |
| 配额及资源回收 | beta RPM拒绝保持429；新代次最终requests/success=5/5、槽位1/1、grammar3/3 |
| 正常停止 | 持久STOPPED、无自动复活；31个已记录进程身份退出，端口及原锁可取 |

单次故障到业务ready观察：模型61.085秒、控制器61.027秒、入口5.538秒。
前两项包含完整60秒配额隔离，可与模型加载重叠。JSON为0.304～0.330秒，
SSE为0.256～0.308秒，工具调用3.609～3.672秒、工具结果回答1.472～1.517秒。
这是功能/可靠性短验耗时，不构成性能优化、统计非劣或恢复SLA。
第二/三代grammar在约51.857/52.128毫秒内实际回收齐平；验收有界等待
不等于修复原生无界wait4，后者仍属后续C。

[203份原始证据](evidence/serve-native-ingress-20261108/README.md)包含
CPU、CIDR负控、编译、逐请求wire、故障、严格租约及交付记录。
独立离线审计核对311项Git来源、39份封装Python及27项原生来源。
进程观察记录两个原生入口代次，未观察到nginx；包清单确认不携带
nginx。轮询不证明采样间隔内不存在干扰。14项已知凭据扫描排除，
不归档API Key、私有配置、完整账本树或模型资产。

## 本机使用与回退

入口为`build/service/bin/q4t service`，元数据`build/service.release.json`，
**新状态目录为`build/service-native-state`**，已初始化，勿重复init。
封装manifest：`41cb22eaf6166ed316b4999c13e4f355a3c357d19debccc54073a660dd030909`。
原生入口：`4a499f1a62e2f165697582b8570c85dd9890c135548f966d0e800d744faed559`。
Guardian a23f31fa、CUDA探测ef1af475；原始prepare回执仍保持当时
`recovery_accepted=false`，实际接受与发布元数据单列。

当前为本机回环、process资源模式、单槽文本/MTP k=3，max_len=8192、
max_prefill=2048、输出256。公共端口18161、后端18162、运维18163。
alpha RPM30/TPM6000，beta RPM1/TPM6000，fair_queue开启；Key仅存私有包。
每代生成自检占用alpha普通请求和实际token配额。

```bash
# 前台运行，或改用 start --daemon 显式后台运行
./build/service/bin/q4t service run --state "$PWD/build/service-native-state"
./build/service/bin/q4t service status --state "$PWD/build/service-native-state"
./build/service/bin/q4t service stop --state "$PWD/build/service-native-state"
```

安装仅help/init/status与来源检查，0模型/0Chat；当前无活Guardian，
status返回UNKNOWN、live_control=false及退出码1，如实表示尚未运行。
开发验证仍必须完整经过`gpu_exclusive.py --inherit-lease`，上面是
正式服务入口示例。更改Key、端口、配额或TLS应另行prepare并按范围验收。

旧v1封装1d414218、`build/service-state`及旧证据全部保留。安装前确认
旧状态revision=0、无attempt/active/run、从未启动；因此初始化新包
状态没有清空使用过的恢复预算。交接时持共享租约和旧账本写锁，
分别原子替换入口别名及元数据，失败恢复旧二元组。
回退回执在`.q4t-work/serve-native-ingress-20261108/delivery-attempt-01/rollback/`。
回退前先停止新状态并确认持久停止/清理，再核对旧manifest、恢复旧
别名和元数据并使用旧state；不要把新别名与旧state混用或删除账本。
原`build/q4t`及发布元数据保持不变，旧包还保留同摘要引擎副本。

## 保留边界

无外层管理器时，Guardian自身、整树或整机死亡后不会反向拉起。
日志阻塞隔离、原生grammar有界回收、通用进度watchdog、留存治理、
exporter新接入、自动profile降级、委派cgroup、NVML-free冷启动仍后置。
未做长稳、视觉恢复、实际LAN/TLS、HTTP/2或通用Web服务器扩展。
不自动重置驱动/GPU、不重启主机、不重放未完成请求或外部工具。
