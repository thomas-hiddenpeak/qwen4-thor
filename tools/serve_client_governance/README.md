# 单进程客户端治理阶段工具

本目录是一次有界验收及交付记录的驱动，固定路径禁止覆盖历史尝试。
生产策略用法见[网关文档](../serve_gateway/README.md)。

原启用v2后的[限定结果](../../docs/SERVE_CLIENT_GOVERNANCE_RESULT_2026-11-05.md)
保留失败、复用和资源回收记录。`stage.py`仅收集scoped续验；它的
`finalize`不会安装候选。后续授权的无策略补验及交付使用独立的
`completion.py`，范围见[完成合同](../../docs/SERVE_CLIENT_GOVERNANCE_COMPLETION_2026-11-05.md)，
最新是否交付、是否合入main以[STATUS](../../docs/STATUS.md)为准。

离线复核（不占GPU，不重跑模型）：

```bash
python3 -B tools/serve_client_governance/completion_report.py verify
```

该命令用于完成阶段公开归档生成后，独立重建新验收和旧观察；旧观察的
默认身份只核对封存previous pair，当前安装身份单独核对，避免把历史
“默认未变”误用到新交付。此前独立scoped报告入口保留为原阶段工具。

任何后续GPU驱动仍须使用原共享锁，guard程序和output均使用绝对路径，
以满足既有交付祖先身份校验。不得直接重跑当前已存在的execution目录；
先完成明确的新阶段身份与预算冻结。原package/execution及其密钥留在
`.q4t-work/serve-client-governance-20261105/`，不能用公开证据替代回退原件。
