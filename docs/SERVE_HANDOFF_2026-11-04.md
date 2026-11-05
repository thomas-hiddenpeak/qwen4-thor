# Serve 主线交接清单

2026-11-05后续：三项有限收尾已完成，最新身份、旧包限制解除范围及
剩余四组事项见[最新交接](SERVE_CLOSEOUT_HANDOFF_2026-11-05.md)。下文为
原交接快照，其中“未验WIP/必须固定旧包/暂停”不再表示当前进展。

2026-11-05补充：[PR #9评分提取已合入main](MTP_PREFILL_MAINLINE_2026-11-05.md)，
合并提交cdea60e0；当前原生默认为a56bd448/sidecar914e067f，协议cc321239。
下文保留2026-11-04交接身份；最新默认及回退见上述合入记录。旧托管包、
持久任务迁移及SDK仍按原已验身份使用，10客户端未提交文件保持未验状态。

交接日期：2026-11-04（Asia/Shanghai）。用户本轮只要求整理交接，
**工业化目标保持暂停，不继续开发、测试或部署**。下面的待办供后续
明确恢复工作时使用，不是启动授权。本次只读源码/已有记录并更新文档。

## 1. 接手基线与工作区

| 项目 | 应依赖的身份 / 状态 |
|---|---|
| 已合入主线 | [PR #8](https://github.com/thomas-hiddenpeak/qwen4-thor/pull/8)，合并提交`668c1921`；本次整理起点为`11ddaa9e27a45a73f483526d70eaf9ebc85882aa`，含合入收尾文档 |
| 验收实现锚点 | `92129c71`；其后至交接起点没有生产代码变化 |
| 当前原生默认 | `build/q4t`：`8e9b4ce7…`；`build/q4t.release.json`：`e61bed7c…`；协议`a56aed21…` |
| 工作分支 / 目录 | `codex/main-reaudit-fixes`；`/home/rm01/models/dev/qwen4-thor`；包含下文10个未验客户端文件，不是干净main工作区 |
| 交接范围 | 单台Thor、单模型文本Chat及只读项目资料/运行记录查询；按已公布的接口子集和配置使用 |

PR检查与合入后主线检查已通过，详情见[合入记录](SERVE_MAINLINE_READINESS_2026-11-04.md)
及[11ddaa9e主线检查](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/41835721166)。
这些是已有检查，本轮不重跑。当前默认的完整摘要、实机证据和回退身份见
[03c机器结果](evidence/serve-prepare-lock-20261104/result.json)。

接手使用已验客户端时，应读取干净main或指定封存版本；当前工作区的
Python共享模块和JS适配器已有未提交改动。不得直接把此目录中的客户端
执行结果归到主线验收，也不要为取得干净目录而覆盖、stash或删除这些改动。
仓库内隔离检出如有必要，仍使用`.q4t-work/`，本轮未创建新检出。

## 2. 当前主线可依赖的能力

以下为已有分阶段接受范围，完整字段和组合以[能力矩阵](APPLICATION_SERVICE.md)、
[OpenAPI](serve-openapi-v1.json)和[输入语义](serve-input-semantics-v1.json)为准。

| 能力 | 已有实现与证据 | 接入限制 |
|---|---|---|
| Chat与流式协议 | JSON/SSE、usage、统一错误、请求追踪、心跳及终态校验；38个输入字段、10条路由 | 单模型、单choice、greedy；中断不能当成功完整响应，未知字段明确拒绝 |
| 工具调用 | 响应解析、完整调用批次、tool_call_id关联、工具历史回填；auto/none/required/named及并行数量控制；真实资料查询闭环 | 工具由应用执行，应用校验参数；不承诺逐token工具参数发布，允许多调用不等于应用并发执行 |
| 消息与聊天模板 | system/developer/user/assistant/tool；模板预检与真实请求共用准备逻辑 | system或developer只允许第0条且不能混用；非null的message.name拒绝；不是任意模型/模板兼容 |
| 推理与停止 | reasoning_content/reasoning字段选择、推理隐藏、thinking_token_budget、文本stop与输出预算 | 隐藏不减少计算；stop的受限profile与严格输出/cache等组合有明确拒绝规则 |
| 结构化输出 | `q4t-json-schema-v1`有限profile、逐token约束、最终验证、隔离grammar worker | 构建显式启用`Q4T_ENABLE_XGRAMMAR=ON`并使用固定依赖；任意schema不支持，业务语义仍由应用校验 |
| 请求与资源保障 | 显式取消、协作截止、准备锁等待取消、准入/容量队列、内存准入、CUDA申请额度、错误与资源回收 | 无GPU硬抢占；没有租户长期公平/RPM/TPM合同；额度与可选host记账域不等于Thor整机物理RAM硬上限 |
| 发现与运维 | models/capabilities/config/preflight、health/live/ready/metrics、加载前保留监听；可选独立运维监听 | preflight不预留资源；TCP连接成功不等于ready；运维端口需显式启用且仅loopback |
| 客户端互通 | OpenAI Python SDK 3.42.1；Node 24.18.0 / openai 7.47.0限定业务互通，关闭自动重试 | 不是全部SDK/框架；主线标准库示例仍为单IO超时，项目JS辅助类仍固定占位Key，没有显式CA配置 |
| 可选应用与部署能力 | TLS/独立Key/CIDR/client预算及状态域、托管/观测、持久任务/显式恢复等已有指定组合短验 | 托管和持久迁移按第3节固定旧组合；实际LAN、正式证书/通知及长期生产接受另行判断 |

最新原生短验为[SRV-03c](SERVE_PREPARE_LOCK_RESULT_2026-11-04.md)：
1模型、6尝试（5成功/1预期取消）、2真实工具闭环；原生16项完成观察
复用加7项定向补验，保留首轮夹具失败。配置为单槽、8192上下文、2048
预填充、MTP k3完整head、本机临时TLS双Key及独立运维端口。
不扩大为208896上下文、多槽、所有可选配置组合或当前ELF长稳已验。

Responses/Assistants/Batch、任意schema、更多模板/SDK、采样和多choice
不在此基线承诺内。媒体另有实验限定证据，不扩展为本次文本交接范围。

## 3. 仍需固定旧包的限制

**原生Chat使用最新已验默认；已有持久任务升级与已验托管组合继续固定
旧身份。** 这是不同使用路径的接受边界，不要求把普通Chat整体回退。

| 使用路径 | 固定内容 | 原因与升级条件 |
|---|---|---|
| 依赖已验托管生命周期 | 下述v6包`fcff7218…`，包含ELF `61abe09f…`、sidecar `ce925d51…`及协议`15d7ccb1…` | [SRV-12l](SERVE_SUPERVISOR_DEADLINES_RESULT_2026-11-04.md)接受此包；最新`8e9b4ce7/a56aed21`尚未新增托管生命周期接受 |
| 已有持久任务跨协议恢复 | [SRV-11k](SERVE_TASK_STARTUP_PROTOCOL_RESULT_2026-11-04.md)的应用来源/模板/模型/连接绑定，以及登记的有向迁移链 | 已验链为`63b29f13 → fc63db28 → ddfba8c0 → 15d7ccb1`；没有到`a56aed21`的登记，不能跳步、反向或静默升级 |
| 核对历史原件 | 原归档中的`current-source/`、`historical-source/`与对应二进制摘要 | 原`build/q4t`路径记录的是当时61abe09f，不是跟随新默认的指针；当前源码/ELF变化导致旧核对器拒绝，不等于旧证据失效 |

已验托管包位于本机：

```text
/home/rm01/models/dev/qwen4-thor/.q4t-work/serve-supervisor-deadlines-20261104/backend-package
manifest SHA256: fcff7218de3b8616d641d3e8780077ca577d3ba9e012350a7723390031428240
protocol SHA256: 15d7ccb103258ea5ec8ca3cfa66c9bfe1245eadb7d38ad6ee149ad97075ec30e
```

同阶段`accepted-package.json`是接受入口。保留包内q4t、sidecar、source、
declarations和策略身份，不把`build/q4t`单独拷入旧包，不就地更新封存包。
旧托管回退包是`.q4t-work/serve-startup-listeners-20261104/accepted-backend`；
原生默认回退pair及依赖在
`.q4t-work/serve-prepare-lock-20261104/package-native-fix/previous/`。
原生pair不是完整托管包的替代品。上述本机目录已确认仍存在，不代表
已在运行；本轮未查询或变更服务状态。

持久任务仍遵守：显式plan/apply、共享8次迁移上限、恢复前重新预检，
已保存工具结果复用，UNKNOWN不得自动重放；幂等键属于应用，request_id/
session_id不能替代任务库。归档迁移接受路径和连续三跳只有CPU证据，
实机11k证明的是活跃任务单步迁移及随后去重/拒绝。

[11k归档清单](evidence/serve-task-startup-protocol-20261104/manifest.json)
保存33项读者来源（含17项应用运行来源）及二进制身份；干净main的共享
parser仍与该记录相同。已有任务应继续绑定原client、模型、模板、地址、
Key/CA和预算；更改时按对应审核路径处理，不能直接修改历史hash放行。
凭据/私钥仅留本机，不写入交接文档或Git。

## 4. 未验收客户端改动：SRV-08b

位置为上述工作区，**6个已跟踪文件被修改，4个新文件未跟踪**，均未
进入PR #8或main。只读hash核对显示与主线合入前保存的10文件快照一致。
表中描述是草稿内容，不是功能已通过的声明。

| 状态 | 文件（相对仓库根） | 草稿内容 |
|---|---|---|
| M | `tools/examples/chat_client.py` | 接入单调总网络预算；新增timeout/ca_file/connect_host与参数校验；保留api_key入口 |
| 新 | `tools/examples/deadline_http.py` | DNS子进程、TCP/TLS、分段写、HTTP底层读及结果返回的共同截止 |
| M | `tools/examples/js/chat_client.mjs` | 显式apiKey/caFile/connectHost、默认Node传输接线，保留总截止和零重试 |
| 新 | `tools/examples/js/node_transport.mjs` | Node HTTP(S) Fetch适配、CA快照、同origin及连接IP控制 |
| M | `tools/serve_protocol/js_client.py` | Python桥将连接参数加入stdin消息 |
| M | `tools/serve_protocol/run_sdk.mjs` | Node入口转交连接参数 |
| M | `tools/examples/test_chat_client.py` | 导入新的截止测试类 |
| 新 | `tools/examples/test_deadline_http.py` | 14个Python测试方法草稿，覆盖慢头/体、chunked、DNS/清理及共同预算等 |
| M | `tools/examples/js/test_chat_client.mjs` | 新增Key传递和连接配置拒绝场景 |
| 新 | `tools/serve_client_transport/baseline.py` | 原客户端确定性慢IO缺陷观察驱动 |

已有的唯一SRV-08b执行记录是**旧实现的缺陷观察**：0.2秒预算下，虚拟
时钟到0.65秒仍返回成功；无真实HTTP/模型/GPU。原件在
`.q4t-work/serve-client-transport-20261104/baseline/`，含旧四份源、
identity.json及result.json。它不能证明新实现正确。
新实现、Python/JS测试及真实闭环均未验收；阶段CPU/HTTP驱动和报告
尚未补齐。冻结范围见[SRV-08b计划](SERVE_CLIENT_TRANSPORT_2026-11-04.md)。

恢复该项时仍需处理：

- [ ] 先审核现有草稿与失败/清理路径，确认DNS子进程、TLS、响应体、
  EOF、超时/abort及SDK Fetch语义；不把用户sink/CPU工作称为可抢占。
- [ ] 补齐应用来源闭包：[engine.RUNTIME_FILES](../tools/project_tasks/engine.py)
  当前列17项，含chat_client.py，尚未包含其新增依赖deadline_http.py。
  持久任务虽然有独立传输层，仍导入共享parser；来源身份不能忽略。
- [ ] 对新应用来源做显式兼容审核；保留已有compatible_runtimes和
  compatible_protocols历史项，不自动登记新协议或放行任意来源。
- [ ] 审核JS Key/CA/IP配置、原始wire与SDK解析一致性、完整工具批次、
  零重试/无重定向及凭据处理，补齐现有测试和缺失的阶段驱动。
- [ ] 用户恢复验证后再执行针对性测试和Python/JS真实资料工具闭环；
  既定上限为1模型、6生成、80无模型HTTP/TLS场景、120控制、360秒
  （含45秒清理），同一GPU原锁覆盖完整阶段；有失败保留现场，不扩样。

这些项本轮均未执行。未来提交必须单独形成客户端验收边界，不能因
原生服务已合入main就把共享Python/JS草稿视为已接受。

## 5. 后续接手顺序与保留项

- [ ] 先确定下游走普通Chat、托管还是持久任务；普通Chat可依赖当前
  原生基线，后两者使用上述已验身份，避免把包升级变成隐式前置。
- [ ] 若需要最新包的托管/持久迁移，独立完成a56aed21协议审核和新ELF
  托管组合接受；与SRV-08b分开记录范围，明确共享parser变化的依赖。
- [ ] SRV-08b只在恢复授权后接着完善；本机源码保留，不清理当前脏分支。
- [ ] 实际LAN/正式证书/通知、长稳、公平调度、整机物理RAM保护及扩展
  接口仍为后续工作，不作为已完成能力；长时间运行验证由用户执行。

模型目录和reference保持只读。原包、Key、任务记录、三份day计划和
回退证据保持；day仍未执行。GPU验证仍须遵循[评估规范](EVALUATION.md)，
原锁不得删除/替换。SOAR恢复单元此前已masked，不能为接手恢复其自启；
本次不把历史资源清空记录当成当前GPU空闲证明。

本轮仅形成交接文档与索引，不执行应用代码、测试、归档重放、服务查询/
启动或部署。文档提交使用`[skip ci]`避免触发自动测试；既有接受结论
及暂停状态不变。
