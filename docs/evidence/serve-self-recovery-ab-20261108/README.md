# 自动恢复 A/B：原始证据与交付

结论 `ACCEPT_BOUNDED_SELF_RECOVERY_AB`，范围与操作说明见
[最终交付](../../SERVE_SELF_RECOVERY_RESULT_2026-11-08.md)。

[离线审计](result.json)绑定相同封装 1d414218、原生 60b1c4c1；累计
5 模型启动/21 Chat/1 CUDA 探测，驱动 281.342 秒、完整租约 284.712 秒。
267 次外层真实 NVML 全 OK，最终 GPU 空、19 个已记录角色身份退出，
18161/18162/18163 和原锁释放。177 项不同 CPU 证据按阶段复用。

原 attempt-01/02 的 FAILED/COMMAND_FAILED、numeric enum 拒绝和
grammar 3/2 即时快照均保留。最后 attempt-03 在用户批准累计 5/21
后完成 2 启动/6 Chat 的控制器/网关补验，外层 COMPLETED。
各轮是独立严格租约；不拼成一次无失败运行。最后真实 grammar 在
51.820 毫秒内从 3/2 到 3/3，没有用时间等待替代实际回收证明。

[282 份原始记录](raw-records.tar.gz)及[SHA256 清单](manifest.json)
包含 CPU 构建/执行/来源、三轮冻结计划与计数、原始 JSON/SSE、实际
工具记录、完整外层监控、停止状态、离线审计脚本及验证驱动历史。
不包含模型、ELF、运行私有配置树、API Key 或内部 gateway token；
归档前核对 19 个实际凭据均未进入记录。其完整私有原件仍保留在
`.q4t-work/serve-recovery-{a1,a2,b1,b2}-*/`。

归档首轮把扫描器源码中的 PEM 标记字符串误判为密钥，未写入任何
归档；失败日志和脚本保留。修正为匹配完整 PEM 行后，282 份记录
全部重读核对摘要，没有重新运行模型。

[交付回执](delivery.json)记录 `build/service` 显式入口和私有持久
state。初始化 STOPPED、未启动服务；原生默认不变，回退二元组已
复制核验。prepare 阶段候选回执保留 false；接受结论由本审计和新
服务元数据给出，不改写旧回执，也不自动迁移旧包。
