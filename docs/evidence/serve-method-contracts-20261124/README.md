# 冻结范围的方法级审计与成功响应

2026-11-08 范围目录的 245 条路径和查询选择器与上游提交 926a7b813 的机器规范逐项相同。用户决定将其中 157 条推理与应用路径纳入项目。它们包含 232 个 HTTP 方法。方法数量包括同一路径不同 beta 选择器。复核时按方法、路径模板、查询选择器和成功状态码比对。[官方快照](https://github.com/openai/openai-openapi/blob/926a7b8133226630bac025350e84b0ca5c31d45a/openapi.json) · [G00 决定](../serve-scope-decision-20261120/README.md)

rev450 声明其中 118 项，114 项缺少声明。在已声明方法中，112 项的成功状态与官方相符。5 个创建操作在已发布的清单中返回 200，上游要求 201。另一个差异是 Models 删除接口。上游成功分支用于组织所有者删除微调模型，本机只读基座返回 403，未知模型返回 404。模型读取和推理继续在范围内。[逐方法清单](rev450-methods.json)

候选已修复 Agent、Session、环境模板、Vault 和 Vault 凭据创建的状态。实际 SDK 和原生 TLS 短验中，五类创建均返回 201，会话流式创建也返回 201，生成了 13 个事件并以 idle 结束。模板更新返回 200。模板输入长度、数量和映射字段限制取自同一官方快照，创建和更新共做了 32 个本地与上游 schema 的边界对照。SDK 还验证了跨客户拒绝及更新失败后原资源保持一致。单次模型启动用时 38.547 秒，随后 Chat 回答 42。首次短验因夹具误读事件中的会话 ID 而失败，两次原始结果均保留。[候选证据](candidate.json)

在仓库根目录运行以下命令可以使用同一份上游文件复算已发布或候选的清单。部署状态、请求与响应字段、流式终态、权限与恢复仍各自需要运行证据。

```sh
python -m tools.serve_audit.scope_methods "$OFFICIAL_OPENAPI_JSON" --issued "$ISSUED_OPENAPI_JSON"
python tools/serve_responses/environment_bounds.py "$OFFICIAL_OPENAPI_JSON" --check
```
