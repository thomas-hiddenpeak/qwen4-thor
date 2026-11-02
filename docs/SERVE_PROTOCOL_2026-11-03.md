# SRV-08 完整机器合同与客户端验收

2026-11-03冻结，基线a030143a、默认5642eb68/6aca6a3e。业务可靠性
推进，不作性能优化。本批交付目标是可核对的完整输入合同、官方JS SDK
接入及真实业务/传输失败闭环；长期工业化目标不因此自动完成。

## 工作范围

1. 发布JSON Schema 2020-12请求合同和静态OpenAPI 3.1：覆盖现有38个
   顶层字段、嵌套消息/工具/schema、null/default/拒绝和10条路由。已有
   响应、推理、stop、预检/配置投影复用并绑定身份。不新增管理端点。
2. 区分JSON结构、精确JSON词法/UTF-8字节、跨字段/工具批次语义与
   动态运行时准入。JSON Schema无法单独表达的规则要有机器清单、
   源码锚点和生产C++校验探针，不用宽松schema宣称完整执行保证。
   当前发布限定文本profile，实验媒体显式列出不在已接受范围。
3. 对每个字段覆盖有效值、null、省略、类型/边界和相互作用；差分
   机器合同与生产校验、模板、解析器初始化。旧证据按身份复用，
   结构接受不等于预算/grammar编译或模型质量接受。
4. 新增JS客户端适配，固定Node24.18.0、官方openai7.47.0及完整lock。
   models、JSON/SSE、工具批次、错误封套、usage与稳定响应身份都检查。
   SDK设置maxRetries=0；没有完整终态、完整参数和全批校验不执行工具。
5. 真实只读项目查询包含正常资料、资料不存在、多个工具结果ID回填；
   传输失败保留原始wire与请求，未知结果禁止自动重放，显式恢复使用
   新凭据及最后完整历史。核对现有Python日志恢复工具互通，业务成功
   与协议完成分开判断。受载资源和跨进程任务执行器仍是后续工作。

## 验证与交付边界

先成组实现；CPU结构/语义差分与JS mock传输/故障、记录恢复/篡改测试
通过后才做真实HTTP。生产运行时未变则不重跑原host/数值矩阵、不替换
相同ELF；将新协议/客户端证据绑定现有发布身份。若发现运行时缺陷，
先记录具体缺口和受影响范围，再决定必要的修复与专项验证。

真实HTTP硬上限10次生成、20个专门负例、2次模型启动、首次guard起
30分钟，不自动扩样或抹去失败。计划默认MTP单槽8192/prefill2048：
普通JSON/SSE2次；真实工具成功往返2次；业务失败往返2次；多工具
批次往返2次；真实传输中断1次及显式新请求恢复1次。strict请求按已有
plain约束路径执行。驱动持整阶段GPU锁，仅结束自有进程；环境冲突
记ENV_ABORT，保留现场。完整通过后封存原件、文档、阶段commit/push。

参考官方[JS SDK入口](https://developers.openai.com/api/docs/libraries#install-an-official-sdk)、
[Chat流式字段](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)、
[重试边界](https://developers.openai.com/api/docs/guides/rate-limits#retrying-with-exponential-backoff)。
具体行为同时核对本机固定7.47.0源码；此任务只访问本机Q4T服务，不调用
OpenAI托管模型。SDK默认可重试和忽略DONE后事件，不能替代本项目终态检查。

## 已完成开发与冻结用例

[工具入口和接入方法](../tools/serve_protocol/README.md)。396项CPU原生
语义均符合预期；检查器5项精确整数/字节问题已修复并定向通过，391复用。
JS35项实际SDK内存传输、桥接/日志12项（含31业务参数对照）通过；
驱动3项初次通过，5项同名模块导入错误定向修复通过。全部失败原件保留。
尚未进行真实HTTP，默认保持不变，不能把离线通过写成SDK业务接受。

实际请求固定为：普通JSON/SSE；docs/MODEL.md读取及架构回答；指定不存在
资料的读取及错误回答；同一响应读取MODEL/ARCHITECTURE两文件并双引用
回答；对已完整工具结果的最终答案切断真实SSE，再从相同完整历史用新
凭据显式生成答案。最后两次不重复执行工具，先检查原槽和owner已排空。
有16个专门拒绝，错误code/param与生产CPU探针对照，监控其无token/worker
增量；余下4个负例额度和第2次启动仅为具体失败修复保留，不自动扩样。
