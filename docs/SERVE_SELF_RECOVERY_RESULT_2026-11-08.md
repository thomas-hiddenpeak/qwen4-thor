# 自动恢复 A/B 有界交付

2026-11-08。结论：`ACCEPT_BOUNDED_SELF_RECOVERY_AB`。
独立 Guardian、控制器和模型恢复、网关代次重绑、持久预算及停止清理
已在本机 Thor 完成 CPU 与真实业务验证。新服务入口已安装并初始化
为 STOPPED，没有常驻服务。原生默认仍为 60b1c4c1/d2ae8db2。
实现与证据在 `codex/serve-self-recovery` 交付；这不表示已经合入 main。

## 实际能力与覆盖

| 情况 | 已实现行为 | 本轮证据 |
|---|---|---|
| NVML 超时、采集异常、普通日志返回错误、metrics 缺失 | 保留健康模型；观测未知，有界退避/半开；指标不是模型健康依据 | CPU 分类及真实模型期间注入 NVML timeout/ENOSPC；同 PID 继续 JSON/SSE/工具 |
| 模型意外退出，内层 NVML 未知 | 持续持有原租约，清空旧子树；每代最多一次独立 CUDA 小探测后按预算恢复 | 实际终止 SSE 中的模型，一次 CUDA 探测及第二代业务通过 |
| 控制器死亡 | Guardian 清理旧模型/辅助后代，核销旧授权后恢复控制器及模型 | 实际 SIGKILL 控制器；固定网关与 Guardian 保持，第二代 JSON/SSE/工具通过 |
| 网关死亡 | 重建网关并核验当前模型代次；保留健康模型及控制器 | 实际 SIGKILL 网关；后续严格工具往返通过 |
| 账本写入/进程失能 | 冻结新启动，健康业务继续；停止不能虚报已经持久化 | CPU 卡住/死亡/迟到事务、停止优先、旧授权对账负控 |
| 真正设备故障、依赖身份损坏、清理不确定 | 关闭准入、保护或锁定，不用无限重启绕过 | CPU 故障分类与生命周期负控；未破坏真实设备/模型文件 |
| 正常停止 | 先冻结恢复，持久化 STOPPED，再清自有树；确认后释放资源 | 三轮停止均持久且清理确认，最后端口与原锁可取 |

模型替换最多 3 次/30 分钟、6 次/24 小时；控制器/网关分别最多
3 次/15 分钟。退避 5/20/60 秒，同故障两个模型代次重复则锁定。
预算绑定持久 service_id，stop/start、更换控制器和墙上时间不能
免费重置。跨 boot 保守保留窗口；首次初始化必须显式执行。

辅助故障注入只发生在验收包装器：不填满磁盘、不改变真实驱动。
外层严格 GPU guard 始终真实采集 NVML；生产包不包含验收包装器或
CPU 模型替身。Guardian 不链接 CUDA/NVML/Python/systemd；控制器与
独立 journal 使用固定 Python，模型和 CUDA 探测在独立进程执行。

## 验收与原失败

177 项不同 CPU 用例：原生进程/配额/journal 25、完整生命周期 35、
政策/账本/Python journal 58、exec 屏障 9、网关纯合同 14、实际
nginx 6、生产接入 22、验收预算/回收等待 8。阶段重复执行不累计为
新覆盖；原始模块遮蔽及其他阶段失败保留。两原生目标零警告构建，
158 来源锚点/3 负控通过；来源检查不等于语义认证。

| 原件 | 模型启动 / Chat | 驱动耗时 | 外层 NVML | 原结果与复用边界 |
|---|---:|---:|---:|---|
| business-attempt-01 | 1 / 5 | 46.991 秒 | 46 次全 OK | FAILED；工具夹具 numeric enum 被正确拒绝；复用普通 JSON/SSE、RPM 拒绝及辅助隔离 |
| business-attempt-02 | 2 / 10 | 118.355 秒 | 112 次全 OK | FAILED；复用模型恢复、工具闭环、配额与不重放；即时 grammar 3/2 样本不追认通过 |
| business-attempt-03 | 2 / 6 | 115.996 秒 | 109 次全 OK | PASS，外层 COMPLETED；补齐控制器/网关恢复、有界回收与最终清理 |

用户明确授权最后补验，累计上限从 4/20 修订为 **5 启动/21 Chat**，
总时间仍限 1800 秒。实际驱动累计 281.342 秒，三次完整租约累计
284.712 秒，单次 CUDA 探测；生成自检、拒绝和中断流均计入预算。
这是三个独立严格租约的针对性续验，不声称整轮一次通过，也没有
覆盖前两轮 FAILED/COMMAND_FAILED。三轮生产包完全相同；改动只在
工具夹具、预算承接及回收等待的验收驱动。

真实质量任务包括普通 JSON/SSE 的 `17+25=42`、强制 strict 工具
读取真实 `docs/MODEL.md` 前 16 行、回填结果及最终来源引用。恢复期
正常凭据 503、坏凭据 401；原 SSE 不补造 DONE，不重放 POST/工具。
新模型最终实际 requests/success=5/5（自检+4 业务），槽位回到 1/1。

最后补验再次观察到响应已结束而 grammar started/reaped=3/2；
51.820 毫秒后实际齐平为 3/3。验收最多等 5 秒，不放宽永久未回收
条件；这没有修复原生 `wait4` 无界阻塞，后者仍属 C。第二轮只保存
了即时快照，不能用第三轮结果改写它的历史状态。

故障注入至业务 ready 的单次观察：模型 60.872 秒、控制器 60.958 秒、
网关 5.566 秒。前两项包含旧模型核销后的完整一分钟 RPM/TPM 保护，
可与加载重叠。该测量不构成吞吐提升、统计非劣或恢复时间 SLA。

[独立离线审计与原件](evidence/serve-self-recovery-ab-20261108/README.md)
重解析实际响应、核对 870 项 Git 来源、38 份封装 Python 来源和
16 项原生构建来源；19 个已记录角色 PID/start 身份全部退场。
267 次外层 NVML 均 OK，每次退出 GPU 计算 PID 为空。合作锁和轮询
仍不能排除短于采样间隔的外部干扰。停止、端口和原锁已独立核对。

## 本机入口与回退

已安装的显式入口为 `build/service/bin/q4t service`，发布元数据在
`build/service.release.json`；持久状态在 `build/service-state`。
入口指向本轮原样封存包，manifest 为
`1d414218fbb108d1c1f1689fd3b42cfdab14f0ffaa12c1a81d86b3f8e760c472`。
原生程序仍为
`60b1c4c121d066789ebc44746316161320448d98095f32a7ec24e3fa00343ff3`。

当前实例为本机回环、process 资源模式、单槽文本/MTP k=3，
max_len=8192、max_prefill=2048、输出上限 256。用户入口
127.0.0.1:18161，后端/运维为 18162/18163。alpha 为 RPM=30、TPM=6000，
beta 为 RPM=1、TPM=6000，fair_queue 开启；独立 Key 只在私有包中。
每代生成自检占用 alpha 的一次普通请求及实际 token 配额。

安装只运行 help/init/status 和来源校验，0 模型/0 Chat。STOPPED
账本没有活 Guardian，`status` 因而返回 UNKNOWN、live_control=false
及退出码 1；这是当前没有运行服务，不用旧账本冒充实时健康。
原始 prepare 回执 `recovery_accepted=false` 保留为当时事实；新的
接受与交付回执单列，未原地修改封装或旧 v9/v10 包。

正式使用时选择前台或后台之一。当前状态已初始化，勿重复 init：

```bash
# 前台，适合终端或外层服务管理器
./build/service/bin/q4t service run --state "$PWD/build/service-state"

# 或显式后台；等待完整业务 ready 后才成功返回
./build/service/bin/q4t service start --daemon --state "$PWD/build/service-state"

./build/service/bin/q4t service status --state "$PWD/build/service-state"
./build/service/bin/q4t service stop --state "$PWD/build/service-state"
```

开发验证仍须把完整驱动放进 `gpu_exclusive.py --inherit-lease`；上述
后台使用示例不代替验证的完整严格租约。改监听、Key、配额或资源
profile 应重新 prepare 独立包，保留旧包与服务身份，按变更范围验收；
不要直接编辑封装文件或删除持久账本来绕过恢复预算。

回退副本与摘要在
`.q4t-work/serve-recovery-b2-20261108/delivery-attempt-01/rollback/`，
包含原 `q4t`、`q4t.release.json` 和入口交接回执。当前原生默认未
替换，原 `build/q4t serve` 仍可直接使用；若停用自动恢复入口，先
对其准确 state 执行 stop 并确认 stop_durable/cleanup_confirmed，
再核对并撤除指向上述 manifest 的新别名/服务元数据。保留 state
账本、封装和证据，不盲目删除或把旧预算变成新身份。

## 保留范围

本轮 A/B 已闭合，以下不随收尾扩展：日志阻塞隔离、原生 grammar
worker 有界回收、通用推理进度 watchdog、诊断目录长期留存、host
exporter 的新所有权接入、自动 profile 降级、委派 cgroup、NVML-free
冷启动。process 模式不提供物理 RAM 硬上限。实际内网 TLS、开机恢复
和长稳需各自验收；长稳由用户执行。

无外层管理器时，可以恢复模型、控制器和网关；Guardian 自身、整棵
进程树或整机退出后没有反向互拉起，也不会安装隐藏恢复脚本。
不自动 reset GPU/驱动或重启主机，不重放未完成请求/外部工具。
新服务恢复组合本轮只验文本；原生视觉既有接受不扩大为本组合视觉
恢复验收。已交付视觉程序继续保留，不追加 GPU 或视觉测试。
