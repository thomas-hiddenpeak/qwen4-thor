# SRV-08 机器输入合同与 JavaScript 客户端交付

2026-11-03 05:51 CST完成有界验收，05:51:56复核GPU释放。冻结源码/
驱动`58c0f2fe`，范围见[阶段合同](SERVE_PROTOCOL_2026-11-03.md)。
持续工业化目标保持active；本批完成输入协议与固定JS客户端范围，
受载资源、严格长payload背压、访问运维和长期稳定仍需后续证明。

## 交付身份和合同

本批没有修改运行时，189个运行时文件与SRV-07已接受身份一致。
真实HTTP直接运行`build/q4t`，完整head、默认MTP k3、单槽8192、
prefill2048；严格工具及none使用原有plain masked decode路径。

- 默认ELF保持`5642eb68d0e2d9296aeb0d5f4197797a18d064b2de749c4368a86fce918167ce`。
- sidecar保持`6aca6a3ebf951ed8e625ff4944b73c34f40f3854b92497be2a8abe485315e382`。
- 协议身份`a6468bf23a7bfb9e870c047cda6d68565e968e13ac4e551fea983f9476f5f747`。
- Node24.18.0、官方openai7.47.0；完整package-lock和4065份SDK文件身份固定。
  独立[客户端发布元数据](evidence/serve-protocol-20261103/client-release.json)
  绑定上述服务身份、源码、客户端/合同文件和验收，不扩大旧sidecar声明。

[请求schema](serve-chat-request-v1.schema.json)覆盖现有38个顶层字段、
嵌套消息/工具/有限schema、null/default/省略和支持/拒绝组合；
[OpenAPI3.1](serve-openapi-v1.json)登记10条路由、请求和既有响应投影。
capabilities保持可扩展发现对象，未宣称其所有响应字段都有静态schema。

[必需原生语义清单](serve-input-semantics-v1.json)把标准JSON Schema无法
单独表达的精确数字、UTF-8字节、工具历史关联、有限schema语义和动态
条件列出，并绑定生产源码。检查器的扩展不是标准schema内建能力。
生产C++探针验证合同、模板和解析初始化，不分词、不编译grammar、不
加载模型；动态预算使用实际preflight，仍不保证未来资源准入和生成。

JS客户端设置maxRetries=0，SDK解析结果与完整原始wire对照，校验稳定
ID/model/created、usage、唯一终态、DONE、完整EOF及整个工具批次。
缺失尾帧、DONE后多余事件、重复ID、不完整参数、未注册工具均停止。
响应8MiB/单SSE事件1MiB/128调用/JS安全整数等范围见
[接入说明](../tools/serve_protocol/README.md)。SSE内部逐块消费，返回完整
事务；目前不提供UI delta回调，也不声称逐token发布严格工具参数。

Node不执行工具；桥接层先完成JS与Python双端业务校验，再交给既有
只读资料执行器。原始字节通过独立继承管道即时回传，Node退出后保留
已收前缀；验收日志逐块fsync。未知结果没有自动重试/自动工具重放。
Python业务read参数仍要求原始JSON解析为int，对`1.0`等数学整数拼写
比JS更严格，桥接层会拒绝，不在验证分歧时执行工具。

## 实际业务与错误验收

| 场景 | 真实观察与结论 |
|---|---|
| 普通JSON/SSE | 两者回答710003，prompt31/completion7，终态/usage一致；普通MTP路径 |
| 真实资料读取 | strict SSE调用read_project_record读取MODEL第1到24行；回填后准确回答Qwen4ExpForConditionalGeneration并附原始引用 |
| 业务错误 | 真实读取指定不存在的docs文件，回填ok:false/record_not_found；模型说明无法核实并报告准确错误码，未把协议完成当查询成功 |
| 同一响应多个工具 | 模型实际发出两个read_project_record调用，分别读取MODEL/ARCHITECTURE；全批验证后逐个执行，结果map反序插入仍按ID正确关联；最终给出qwen4_exp、greedy和两个准确行号引用 |
| 实际传输中断 | 只针对本次loopback连接的代理在真实答案content帧内切断；客户端保留435字节前缀，SDK报SSE JSON截断，无成功receipt、无半段历史提交、无新工具执行 |
| 显式恢复 | 检查原owner/worker/槽已排空，重查能力；从先前已完整工具结果生成最终答案，使用全新request_id/cancel_token，正确引用且0次额外工具执行 |
| 输入拒绝 | 16个真实JS SDK 400错误，code/param与生产CPU探针一致；prompt/completion及grammar worker启动计数不增长 |
| 发现与运行状态 | SDK models/capabilities、config/healthz/livez/readyz通过；返回结构与所发布schema投影一致 |

恢复用例证明的是“已完整工具结果后的最终答案重建”。未声称能自动
恢复未知工具执行结果或任意跨进程任务；404/202也不作为未执行证明。
节点退出、工具批次截断、未知函数和记录篡改另外有离线真实SDK检查，
不把内存Fetch夹具称为真实HTTP。没有访问OpenAI托管模型。

完整响应9份＋拒绝响应16份，共25份wire/56个封套通过已发布传输schema。
预期截断单独记录为失败状态，不能计作完整响应。全部生成后观察slot、
请求owner、strict lease、worker和词表storage归零，关闭正常。

普通JSON/SSE分别约0.580/0.481秒；单资料读取与回答约3.914/4.216秒；
双调用与回答约6.947/8.570秒；显式恢复约4.165秒。只是本机有界业务
观察；没有五档性能对照，不作性能提升或非劣接受。

## CPU、保留失败与预算

396项CPU生产语义均符合预期（250接受/146拒绝，38字段各有有效/null/
错误形状/省略）；含既有模板输入形状复用，不冒充新增Tokenizer证明。
新增schema检查器初次391通过、5失败：引用资源切换回基础validator，
使精确整数与UTF-8扩展失效。修正注册分派后仅重验5项通过，其余391复用。
动态cache/后端配置是探针声明输入，不能当作真实cache/grammar验收。

实际固定SDK内存Fetch35项、跨Node/Python桥接与日志12项（含31业务
参数对照）、驱动9项通过；16个HTTP拒绝夹具和2个strict请求探针检查
另行核对。C++探针`-Wall -Wextra -Werror`编译零警告；生产二进制不重建。
已接受的运行时host、Tokenizer、数值证明按189文件身份复用。

原件保留：schema资源缺$id加载错误、初次JS括号语法错误、SDK未导出
package.json的加载错误；驱动8项中3通过、5因旧同名模块导入失败，改
完整包导入后只重验5项；汇总门的Node报告符号/Python单条test拼写错误
分别修正，再增1项报告门检查通过。这些失败均发生在GPU阶段前。

真实阶段首轮完成，累计**10生成尝试/16专门拒绝/1模型启动**，包含
预期传输中断；没有隐式重试或扩样。业务驱动96.27秒，首次guard到
finalize105.73秒，未扩10/20/2及30分钟原预算；guard COMPLETED、
ENV_ABORT0。默认pair没有替换，没有额外安装模型启动。

## 原件、回退与后续

[证据入口](evidence/serve-protocol-20261103/README.md)直接复制283份记录，
归档共287文件/2.70MB，3个探针ELF/source.bundle登记本机身份。包、执行
及归档根seal均核对；包含所有wire、工具结果、观测、guard和上述失败。
早期未作为验收依据的初版探针与npm安装日志仍留本机工作目录。
源码bundle锚定58c0f2fe，SDK完整文件树身份在package/sdk.json。

本批无运行时回退操作；停用新增客户端/合同文件即可回到原接入方式。
原SRV-07默认和更早二进制/sidecar回退链完整保留，服务回退仍按
[SRV-07原冻结入口](SERVE_PREFLIGHT_RESULT_2026-11-03.md#证据与回退)。
05:51:56 CST GPU计算PID空、共享锁可用、无恢复标记或常驻模型，
MemAvailable116.98GiB；没有主动丢弃文件页缓存。

下一批先冻结受载资源与严格长payload实际背压的边界和验收，再补
访问运维及长时间运行证明。通用任务恢复按明确业务需求单独推进，
不从本次最终答案恢复推导幂等执行或工业化已经完成。
