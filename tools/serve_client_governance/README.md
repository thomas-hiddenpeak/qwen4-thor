# 单进程客户端治理阶段工具

本目录是一次有界验收及交付记录的驱动，固定路径禁止覆盖历史尝试。
生产策略用法见[网关文档](../serve_gateway/README.md)。

当前开发和启用v2后的短验完成，候选未安装；默认关闭策略的实机验证
待额外1启动预算选择。[限定结果](../../docs/SERVE_CLIENT_GOVERNANCE_RESULT_2026-11-05.md)
列明失败、复用、资源回收和剩余项。`stage.py`的当前执行入口仅收集
scoped续验；`finalize`不会安装候选。

离线复核（不占GPU，不重跑模型）：

```bash
python3 -B tools/serve_client_governance/scoped_report.py verify
```

任何后续GPU驱动仍须使用原共享锁，guard程序和output均使用绝对路径，
以满足既有交付祖先身份校验。不得直接重跑当前已存在的execution目录；
先完成明确的新阶段身份与预算冻结。原package/execution及其密钥留在
`.q4t-work/serve-client-governance-20261105/`，不能用公开证据替代回退原件。
