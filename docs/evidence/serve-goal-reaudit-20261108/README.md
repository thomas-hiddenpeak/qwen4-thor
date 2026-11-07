# Serve总目标重审证据

2026-11-08。此次是源码、目标和官方协议的审计，不是业务验收。
基线`cc6e9d5b6acb98440861152642a2ac48224b29a5`；R1工作树未验证，
不改变默认引擎/服务包，也不宣称新功能已经实现。

## 可核对原件

- [本地字段、路由与16文件摘要](local-surface.json)：包含当时R1工作树，
  不是假称纯commit快照；WIP补丁另保留在本机同名`.q4t-work`目录。
- [官方Chat顶层字段](official-chat-fields.json)与
  [逐字段任务映射](chat-field-mapping.json)：37字段；本地38字段含扩展。
  允许字段名不证明参数执行、缺省值、nested类型或响应兼容。
- [官方端点目录及任务分配](openai-endpoints.json)：245路径，无HTTP
  方法/完整语义；不能用路径清单冒充已取得完整OpenAPI规范。
  具体操作语义在A01细化。G00代表范围待澄清，不能当作已排除。
- [旧SRV任务映射](legacy-task-mapping.json)：保留PHASES各次接受记录，
  映射剩余兼容/组合/发布问题；不把已接受工作全部重测，也不把旧版
  “已交付”外推到新增能力。原R1—R10完整映射到总计划R01—R10。

## 关键可复查位置

| 现象 | 源码锚点 | 总计划 |
|---|---|---|
| 只接收中性采样参数 | `src/server/chat_contract.cpp`的temperature/top_p/n/penalty/logprobs校验 | A01/A03 |
| system/developer位置及name限制 | `src/server/chat_template.cpp`的历史与角色校验 | A02 |
| schema和工具输出组合限制 | `src/server/output_constraints.cpp`、`chat_response_parser.cpp` | A04/A05 |
| 仅Chat生成路由 | `src/server/chat_http.cpp`、`src/service/ingress_main.cpp` | A06—A11/N05 |
| 原生仅127.0.0.1、拒绝证书 | `ingress_main.cpp`参数/监听；`tools/serve_recovery/bundle.py`原生校验 | N01/N02 |
| 无浏览器预检、拒绝分块传输 | `src/server/http_request.cpp`及入口路由 | N03/N05 |
| grammar阻塞回收 | `src/server/grammar_process.cpp`的Stop/wait4 | R02 |
| 普通写入阻塞 | 既有`tools/serve_service/supervisor.py`及恢复控制器；R1未验WIP隔离中 | R01 |

## 官方资料及获取局限

通过OpenAI官方文档工具读取了[Chat请求/响应参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、
[Responses迁移](https://developers.openai.com/api/docs/guides/migrate-to-responses)、
[工具调用](https://developers.openai.com/api/docs/guides/function-calling)、
[结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs)
和[Batch](https://developers.openai.com/api/docs/guides/batch)。
Chat的OpenAPI工具结果只返回未解析的`$ref`且没有components；
Responses-create和Models-detail抓取只返回摘要占位，未用它们宣称
完整schema已审完。后续逐接口实施仍要读取完整操作合同。

vLLM当前[原生服务参数](https://docs.vllm.ai/en/stable/cli/serve/)
列出TLS、证书刷新、CORS与Key；
[服务接口](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/)
用于对照能力层次。本地只读源码固定
`67111973eeb104e83bc6707a2d0f139737b711a0`，不冒充最新上游。
未复制参考项目源码，也未改动reference/。

## 目标工具迁移

已实际尝试创建覆盖A/N/R/Q的总目标，工具拒绝：
`cannot create a new goal because this thread has an unfinished goal; complete the existing goal first`。
当前接口没有改写活动目标功能。旧R1未完成，不能虚报完成来释放槽位。
因此应用仍显示旧R1，实际执行遵循新总计划；R1真实交付后立即迁移
为总目标并继续，不能在该检查点停止。迁移成功时追加回执记录。
