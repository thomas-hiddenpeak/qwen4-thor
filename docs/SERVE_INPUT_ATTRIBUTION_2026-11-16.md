# 标准输入归属、服务档位与 Responses 默认值

2026-11-16，A01/A07/A12 的下一组实现。rev158 已交付且 PR31 已实际合main
（8015dbc8），本组CPU通过、业务/发行待做；全部33项master仍是完成边界。

## 功能合同

1. Chat 接受 `safety_identifier` 的 string/null，最多128个Unicode字符。
   与 Responses 共用检查，修正后者当前4096字符上限。该标识只作
   请求归属；不作为鉴权、配额或缓存作用域，不进入模型提示。现有
   请求诊断记录按客户端作用域生成的固定SHA256摘要，避免原文进入
   日志。store=true 的Chat私有记录保留相同摘要；普通公开响应不新增
   私有标识，日志写入失败不得改变业务结果。
2. Chat `service_tier` 接受冻结官方enum及null，省略/null请求auto。
   当前本机实际执行池为default，JSON、所有Chat SSE片段（含最终usage）
   和存储对象返回实际default；不能把请求的fast/priority回显成已提供
   相应服务。请求偏好保留在私有存储归属记录，原客户端公平准入保持。
   不承诺云计费、专属容量或未经实现的优先调度。
3. Responses 新请求的省略、reasoning:null、effort:null统一medium，
   显式xhigh等原级别保持。已接受的后台queued任务恢复必须使用原快照
   中的有效effort，不能因升级改变旧任务；原始请求、事件及存储对象
   不改写。缺失、非法或与显式请求冲突的快照不得启动生成。
   旧queued已接受的129—4096字符归属值只在原请求、快照及两条事件
   完全一致时恢复；新请求仍限128字符，不用新限制丢掉已接受工作。
4. 公开schema/生成器、能力声明、输入语义和Python/JS客户端同步；
   input_tokens/preflight、Batch、存储与后台入口共用验证路径。

依据为冻结的[Chat创建参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
及2026-11-16核对的[Responses创建参考](https://developers.openai.com/api/reference/resources/responses/methods/create)。
官方说明返回档位可以不同于请求偏好；本机始终报告实际执行档位。
Responses文档工具仅返回占位页，已用官网正文复核effort默认和128字符
限制；该问题不是跳过协议核对的理由。

## 验证与交付边界

本组非性能改动。先成组实现，再冻结最多20项直接C++用例及少量公开
schema/客户端合同对照；现冻结18项C++及16项schema/native输入对照。
覆盖字符边界/null、归属隔离与日志失败、JSON/SSE
实际档位、存储/重新读取、默认提示、旧queued快照恢复及冲突负控。
必要host/engine/probe使用独立构建目录，零警告；来源与生成物检查。

CPU通过后最多1次启动、6次生成、900秒，同一严格GPU租约：Chat JSON/
SSE、Responses省略/null、一次真实文档工具及SHA续答，验证实际tier、
归属摘要、usage和同PID资源清理。读取/删除本轮自有对象不计生成；
仍记录全部HTTP请求。不跑五档性能或全模型正确性矩阵。后续适用的
主轻/封包/回退短验另行冻结，未变身份的控制证据复用。

这组交付后继续剩余五个Chat字段、历史audio/annotations、媒体缓存、
真实后端、工具与资源/冷启动等全部计划；不把接受字段当整体功能完成。

## 当前针对性证据

[CPU通过记录](evidence/serve-input-attribution-cpu-20261116/README.md)：
18项C++、5项schema/客户端/驱动、16项native/schema对照，715来源/
7负控通过，零警告。新候选7a23cb30未替换rev158，继续冻结的六条
实际生成；不追加无关性能/完整正确性矩阵。
