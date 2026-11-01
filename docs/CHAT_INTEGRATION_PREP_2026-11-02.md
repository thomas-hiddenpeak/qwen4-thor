# 聊天接入开发准备：能力、验收入口与失败恢复

## 冻结范围

2026-11-02用户批准继续提前开发，继承不测试要求。基线3756269，
默认程序0c5ab1de/sidecar e6fdd13b保持；不操作vLLM。任务属于接口
与工具业务推进，不作性能优化或性能接受声明。

本轮只开发、静态审查和必要编译，不执行测试、模型请求、示例、
能力探测或部署；提交使用`[skip ci]`。候选不替换默认程序。

1. 增加只读能力查询及模板/协议源码身份，客户端能明确拒绝不支持
   的服务；声明描述实现，不代表质量或发布验收通过。
2. 准备统一有界验收入口，固定源码/产物身份，串联host、客户端、
   模板及既有HTTP驱动；整个未来执行阶段必须持有同一GPU独占锁。
3. 完善工具示例的持久记录与只读恢复判定，区分请求结果未知、工具
   结果未知、可从已保存结果恢复和已完成；不自动重发或执行工具。

已有HTTP预算保持23生成/10负例/3启动，上限24生成/30分钟；能力
查询复用同一服务启动，不额外生成。统一入口增加host准备的有界
时间，不把旧证据或编译成功当成本候选已验收。

## 状态

三项开发完成，**全部运行验证未执行**。候选830694ce、Thor host
程序9e66abd4编译/链接零警告；8个Python文件只作语法编译，没有
导入或执行。模板核对工具复用前阶段同身份f1aff912编译产物。
未重新配置公共host工程，因为其配置阶段会运行pidfd探测。

新增4项C++合同进入发布清单（共97项）；客户端由19扩到22项，
新增11项恢复、3项编排合同。上述用例、原21组模板、真实HTTP、
客户端示例和恢复检查器均未运行。默认程序与前阶段候选身份保持，
当前仅为可回退的开发快照，构建证据见
[开发证据](evidence/chat-integration-prep-20261102/README.md)。

## 能力与身份

新增`GET /v1/capabilities`，不触发生成、不查询模型文件。回复含：

| 字段 | 含义 |
|---|---|
| `schema_version=1`、`model` | 本接口格式与服务模型名 |
| `implementation_only=true`、`release_attestation=false` | 仅声明实现，不证明健康、质量或发布验收 |
| `chat` | messages默认解析、raw兼容、推理字段、完整工具参数流式输出、结束顺序和输出解析错误合同 |
| `tools` | function/auto/parallel=true，工具在客户端执行；strict、约束解码和完整schema校验不支持 |
| `tools.history` | 完整带ID批次校验、文本结果乱序归一、媒体结果不得乱序、无ID旧路径 |
| `template` | 原生参考模板、renderer/history源码、冻结夹具摘要及模板选项 |
| `runtime` | 本服务实际media/MTP开关；不是健康检查 |
| `cancellation` | 202只接受取消，不能证明清理结束；请求ID不提供重放幂等性 |

原生参考SHA仍为c3cf9e34；它来自冻结夹具元数据，不声称运行时加载
并使用了模型目录的Jinja文件。实际使用编译进程序的C++ renderer。
`protocol_source_sha256`为CMake选定15个协议相关源码的有序
`路径:SHA256\n`摘要；不是完整binary身份或完整依赖证明。构建生成
源码清单及header，源文件变化触发重新配置；本候选协议摘要a765c021。
完整binary与运行时源码身份另记在阶段证据。

客户端新增`capabilities()`及`validate_capabilities()`；支持显式固定
协议和参考模板摘要。404、字段不兼容或摘要不符会失败，不自动退回
raw接口。demo在第一条生成前查询并保存能力，因此当前demo需要本轮
候选，不能直接用于没有该接口的旧候选或现有vLLM端点。

## 统一有界验收入口（当前不要执行）

`tools/response_protocol/acceptance.py`区分三个入口：

- `freeze`只读源码/产物/model配置及tokenizer身份，写新计划，不执行
  测试或进程能力探测。记录全部src/include/cmake/tests/tools清单；
  同时检查生成header、协议源码清单与当前源码是否一致。
- `execute`核对计划后，启动现有gpu_exclusive并在整个阶段持锁。
  `worker`仅供guard启动，直接调用会检查真实锁与祖先进程。
- worker依次运行97项发布host门、22客户端/11恢复/3编排合同、
  21组模板字节核对、既有HTTP驱动。每步有截止，失败即停且保存现场；
  源码/产物执行前后变化均拒绝。HTTP30分钟，总阶段40分钟，另保留
  guard启动/清理的有界时间。

HTTP仍为23生成/10负例/3启动；每个启动增加一次能力GET，生成预算
不扩大。候选声明和生成header对应，baseline应404；实际MTP/media
开关逐模式核对。解析正例的同一份原始响应交给正式客户端再次读取，
检查message/finish/usage和request_id一致，不新增生成或执行工具。
完整demo的真实网络执行、恢复检查器的真实崩溃注入仍未纳入本HTTP
计划；离线合同及同报文客户端解码不能被称为这些业务流程已实测。

以下命令留待设备业务空闲、允许验收后执行；本轮没有运行：

```bash
/usr/bin/python3 -B tools/response_protocol/acceptance.py freeze \
  --candidate .q4t-work/chat-integration-prep-20261102/q4t-candidate \
  --baseline .q4t-work/response-parsing-20261102/previous/q4t \
  --host-tests .q4t-work/chat-integration-prep-20261102/q4t_host_tests \
  --template-checker .q4t-work/chat-integration-prep-20261102/verify_chat_template \
  --identity-header .q4t-work/chat-integration-prep-20261102/chat_protocol_identity.h \
  --model-dir /home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream \
  --output .q4t-work/chat-integration-prep-20261102/acceptance-plan.json

/usr/bin/python3 -B tools/response_protocol/acceptance.py execute \
  --plan .q4t-work/chat-integration-prep-20261102/acceptance-plan.json \
  --output .q4t-work/chat-integration-acceptance-01
```

只在worker、guard及自有进程清理全部成功时记录PASS；业务竞争、
监控或清理失败记ENV_ABORT。该结论仅为有界聊天接入验收，不部署、
不覆盖默认文件、不声称性能或完整模型质量通过。计划记录源码和
产物的关联，不代替可复现构建证明或所有权重内容校验。

## 失败恢复规则

demo增加version=1持久日志和整轮文件锁。JSON记录写临时文件并fsync，
用不可覆盖的原子发布并fsync目录；提交记录先于网络，工具started
先于工具执行，result先于后续工具和历史提交。完整wire持久化后才
写response。示例仍只有显式的本地只读lookup，不新增副作用工具。

`tools/examples/tool_recovery.py`只读取已停止run的记录，不发请求、
不取消、不执行工具、不修改原日志。完整响应需通过原始wire重读和
身份核对；工具计划、开始、结果和历史必须一致。最多256文件、单
文件32MiB、整目录128MiB；旧的无版本日志、损坏/矛盾/越界记录或
符号链接拒绝自动推断。活动writer返回ACTIVE_RUN。

| 状态 | 后续处理 |
|---|---|
| READY_TO_REQUEST / REQUEST_NOT_SUBMITTED | 核对能力与剩余预算，用原配置/完整历史创建新凭据；不能原样重放旧请求 |
| REQUEST_OUTCOME_UNKNOWN | 保留响应片段与原请求；核对服务状态或显式取消，不提交半段内容或工具 |
| TOOLS_PENDING | 重新按当前白名单校验整批，只能显式执行从未started的调用 |
| TOOL_OUTCOME_UNKNOWN | started无result可能已经有副作用，必须对账，不能自动重放 |
| LENGTH_LIMIT / REQUEST_BUDGET_EXHAUSTED | 保留最后完整历史和已知结果，不自动执行工具或扩大预算 |
| FINAL_RESPONSE_SAVED / COMPLETED | 不重放；协议完成与业务答案成功分开判断 |
| RECORDS_INVALID / ACTIVE_RUN | 保留现场，人工核对或等待writer结束 |

全部工具result已保存而history尚未落盘时，可从记录重建下一轮完整
history；仅生成恢复计划，不自动续跑。取消202不等于GPU已释放，
404也不能证明请求从未执行。外部工具要自行提供幂等键/对账能力，
本日志不宣称事务性或exactly-once执行。

后续检查已停止的示例记录可用以下命令，当前同样未运行：

```bash
/usr/bin/python3 -B tools/examples/tool_recovery.py \
  --directory .q4t-work/agent-tool-demo-01 \
  --output .q4t-work/agent-tool-recovery-plan.json
```

输出不可覆盖且在原run目录外；文件含完整历史/工具结果，应按私有
会话材料保存，终端只输出判定摘要。没有后台等待、自动补测或自动
业务接管；后续先在合适窗口统一验收，再按实际结果决定交付。
