# 客户端隔离与托管 TLS 入口

按“受控内网、多客户端、TLS 网关、独立 API Key”推进。当前已交付
客户端状态/配额隔离，并完成生产托管后端 v4 与托管网关 v2 的本机
组合短验。实际内网地址、允许网段和证书来源尚待提供，当前只监听
loopback。测试 Key 和自签证书不用于正式接入。

- [客户端隔离与预算结果](../../docs/SERVE_CLIENT_RESULT_2026-11-04.md)
- [默认交付与回退](../../docs/SERVE_CLIENT_DELIVERED_2026-11-04.md)
- [托管组合短验结果](../../docs/SERVE_CLIENT_MANAGED_RESULT_2026-11-04.md)
- [冻结运行合同](../../docs/SERVE_CLIENT_MANAGED_2026-11-04.md)

## 用户长时间验证入口

2026-11-04 已为当前默认 `7619966e` / sidecar `a2f9cb8f` 准备新 day
计划，复用短验通过的同一后端包和网关包。没有启动 day，也没有
timer、恢复脚本、常驻安装或开机自动启动项。长时间验证由用户开展。
旧两份 day 计划仍绑定其历史版本，未修改、未执行，不能代替本入口。

在项目根目录检查并显式启动：

```bash
python3 -B tools/serve_clients/managed_stage.py check --profile day
python3 -B tools/serve_clients/managed_stage.py run --profile day --operator-run-day
```

`check` 不加载模型或发送 HTTP。来源、默认 pair、包、策略或证书
不符合冻结身份时拒绝；不要改写 plan 或删除已有 execution 来重跑。
后端生产 controller 自带 `gpu_exclusive`，覆盖全部模型/HTTP 和网关
退出；不要再套外层 guard。检查到其他业务不会停止它或清除其锁。

该命令前台运行，使用临时 systemd 后端 unit，restart=0。共 5760 轮，
每轮 alpha 普通 JSON、beta SSE、严格 JSON、真实项目资料工具及回填，
最后增加排空/重连两次，合计 28802 成功请求、5760 次只读工具执行。
轮次间隔至少 15 秒、业务窗口至少 86400 秒；GPU预算 87720 秒，
全程 87900 秒，显式控制上限 60000。超额、缺失证据或业务失败立即
保留失败并清理本轮，不自动重放请求或重新启动模型。

运行记录位于
`.q4t-work/serve-clients-managed-20261104/day/execution/`：

- `progress.json`：当前轮次和成功数；不是进程存活证明。
- `first-hour.json`：满 3600 秒且完成 240 轮/1200 请求后，记录首小时检查。
- `capture/`：READY 至停止前的连续资源样本、完整 guard 日志副本，缺观察/轮转缺页即失败。
- `result.json`、`soak.json`、`guard-archive.json`：最终业务、资源和租约记录。

Ctrl-C 或另一个终端执行以下命令，只停止此计划记录的驱动身份。
驱动会先停止网关，再停止后端并移除自己的临时 unit；提前结束不计通过。

```bash
python3 -B tools/serve_clients/managed_stage.py stop --profile day
```

如果驱动已被强杀，不能只凭 `signal_sent=false` 宣称清理完成。
用 plan 中的 unit 名及 run 中的 PID/start_ticks 核对实际状态；后端 unit
有 RuntimeMaxSec，但这不是已经退出的证据。不要按历史 PID 批量 kill，
不要删除原 GPU 锁文件；保留原件再处理残留。

完成后离线重新解析全部响应、工具参数/引用、grammar 回收、首小时及
day 资源包络、完整租约和进程退出结果，不再发送 HTTP：

```bash
python3 -B tools/serve_clients/managed_report.py check --profile day
```

本机测试证书仅有效 30 天，启动前还需覆盖全部运行预算加 1 小时；
过期后需要显式准备新证书/包并重新确定验收身份。短验没有证明小时/
天级稳定、长期公平或物理 RAM 硬保护；RSS、cgroup anon、fd、线程的
16 轮预热后包络也不代表所有 CUDA/统一内存的峰值硬限额。

## 已完成短验证据

CPU 38 项有效通过，首次 37 项中的两项初始化错误、修正轮新增夹具
的一项错误均保留，只补验受影响项。真实短验一次模型启动、12 成功、
两次工具/引用回填和两代网关通过；GPU 锁实际持有 78.713 秒，10 秒
无复起观察后清理复核通过。默认 pair 保持不变。

```bash
python3 -B tools/serve_clients/managed_report.py verify
```

上述命令核对 Git 中 328 份公开原件归档。私钥、API Key、gateway token
和带这些值的配置留在权限受限的本机目录，不进入公开归档。
