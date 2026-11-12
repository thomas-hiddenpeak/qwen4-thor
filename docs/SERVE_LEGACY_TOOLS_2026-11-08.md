# A04 legacy functions/function_call闭环

选择/增量子组已交付945a05ea，继续补标准旧函数接口。统一输入适配
将functions/function_call和历史assistant.function_call/role=function
映射为已有函数合同，保留原始错误字段路径和单调用语义。新/旧工具
定义、选择不能歧义混用；标准JSON/SSE输出仍返回function_call和
对应finish_reason，不能只接收参数而悄悄回成另一种协议。

同一适配用于production、preflight、schema检查和模板。客户端要
支持旧响应的分片累积、完整终态及EOF后执行、角色function结果
回填和最终回答。先成组实现后针对性CPU；真实短验上限2启动/
8Chat/900秒，计划1启动/最多7Chat，具体闭环请求在开发后冻结。
不做全性能/全模型正确性；默认通过业务验收前保持945a05ea。

custom text/regex/lark及其历史/执行在本组后继续；本地已有XGrammar
提供Lark和regex编译，优先复用现有隔离worker，不新增外部服务。
A04/A05与其他总计划持续开放，不能把每一子组当停止点。

开发与针对性CPU：31项C++、53项schema/native、12项Python（含3项
短验驱动）和6项实际SDK JS通过。4个构建目标零警告。第一次JS用例
误取工具计划的id而非call_id，保留失败原件；只修夹具并重验该项。
补查Python/JS桥接的legacy计划一致性并加正反例，已通过。

真实业务冻结为1启动/7Chat：旧named SSE调用→真实docs/MODEL.md
读取→旧function结果回填最终标题/引用；旧auto JSON；同一旧历史
迁移现代tools接口并实际读取；长参数截断；收到参数后显式取消；
同PID正常MTP回答。最多2启动/8Chat/900秒，同一严格租约，不自动
重试，不做性能或整模型质量矩阵。通过业务及资源清理后才替换默认，
未改原生入口/Guardian及控制面复用既有证据。

## 已验收交付

实现30e1b62c。上述1启动/7Chat全部通过，61.614秒驱动、62.658秒
严格租约、59次NVML全OK。旧工具参数流15片段，真实资料标题/
引用正确，旧结果历史迁移到现代tool_calls输出正确，截断/取消后
均不执行工具，同PID继续正常MTP。7请求/5成功/1取消/1预期截断
错误；模型退出0、GPU/端口/锁释放。[124原件](evidence/serve-legacy-tools-20261108/README.md)
与586来源可核验，首次JS失败也保留。

默认33c52e4a/元数据e33fa4f0、协议13f42691；v4服务13f077b6/
发布4996f016。旧945a05ea/6c167898及d2cc6b9b/dda2ef99回退保留，
原生/44控制面字节未变，原ledger/Key/配额保持STOPPED；安装0/0。
A04尚有custom text/grammar，随后A05等所有计划继续，不停止总目标。
