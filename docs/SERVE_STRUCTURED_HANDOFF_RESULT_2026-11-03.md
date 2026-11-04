# 结构化终态交接修复限定交付

默认已更新为`d66f2bb9`，sidecar`42d7672a`，protocol`55a3e67b`；
运行时冻结`fded390d`、211来源中仅6个serve文件改变。模型计算、
tokenizer、模板、媒体实现不改，不作性能保持或优化结论。

问题来自实际托管验收：严格JSON已经完整发出，旧grammar worker与
外层独占lease约6ms后才释放，立即发起的工具请求误报503。现于JSON/
SSE终态发布前标记收尾，后继等待资源析构；生成中的竞争仍503。
等待受既有inflight容量和请求取消/超时约束，没有提前释放资源。
详细合同见[修复范围](SERVE_STRUCTURED_HANDOFF_2026-11-03.md)。

- CPU：最终候选零警告，公共261/必需227/结构化后端9通过；确定性
  socket夹具覆盖已读终态但owner未销毁的窗口，以及四类取消/超时。
  驱动4项有效通过。初始重复变量编译失败、计数receipt覆盖失败保留；
  后者重验3、复用1。80列整理前的通过产物与记录另存。
- 实际HTTP：原生25通过；1启动、9生成尝试，8成功/1预期截断错误。
  严格JSON→真实资料工具/答案、错误→JSON及SSE→JSON通过，无客户端
  sleep或自动重试。9份完整wire保留，grammar started=reaped=7、
  owner归零、slot1/1free、structured拒绝0。
- 原首轮整体仍FAILED/COMMAND_FAILED：末尾把`/healthz`交给只允许
  诊断路由的客户端，被本地白名单拒绝，**该HTTP没有发出**。已取得
  `/livez`、`/readyz`及GPU健康/slot/owner指标；没有最后独立资源样本。
  正常独立stop步骤未走到，fallback scope停止的实际exit0、排空日志
  和scope删除记录通过。原失败不改写，也不虚构一次healthz成功。
- 纠正阶段重新审核全部wire/身份/计数/健康和回收证据；不新增模型、
  生成或HTTP，只在新lease内完成安装CLI与同ELF HTTP/worker证据复用。
  纠正guard COMPLETED，至交付约250.3秒，保留原600秒期限。两次lease
  分开记录：首轮9次模型请求始终在同一lease内，安装阶段没有推理。

此前托管失败的4次生成与本修复9次分别计数，不宣称仍在托管原8次
预算内。托管29/guard30 CPU结论保持，实际恢复与退避停止尚待补验。

当前/上一个完整pair+缓存+规则保留于
`.q4t-work/serve-structured-handoff-20261103/rollback-bundle`，已只读核验，
没有实际回退。专用入口`tools/serve_handoff/rollback.py check`只读；
执行回退需要新`gpu_exclusive.py`阶段锁与显式`execute --output`。
默认安装无pending recovery；原SOAR恢复unit仍masked/inactive，GPU空、
原锁可取得。未安装常驻q4t服务，不证明新ELF长稳或物理RAM硬边界。

[原始记录及逐文件身份](evidence/serve-structured-handoff-20261103/manifest.json)
保留原托管失败、CPU失败、实际wire、资源样本和纠正交付。
