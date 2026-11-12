# A07.8 Responses 字段与剩余执行链核对

固定 SDK 7.47.0、已生成 application type graph 与本机实现为依据。
这组解决输入字段不一致；不能替代 A07 全部 item、include 和 SSE 的
实现，也不减少 A08—A12、R05—R10 等总任务。

## 已确认缺陷和修复

- create 的 `input`、`user`、`prompt_cache_options` 不接受显式 null，
  原实现会将其视为省略。八个非 nullable 顶层字段统一提前检查；
  `input` 类型在状态读取之前验证。
- `reasoning.mode:null` 原先被吞掉，改为带准确 param 的 400；其他
  四个 reasoning 字段的允许值以固定 SDK 的合同核对。
- SDK 已定义 fast/ultrafast 服务档位，原入口误拒绝。接受全部七种
  标准值，执行结果返回实际采用的 default；不虚称本机有云端计费、
  专属算力或延迟保证。
- 提示模板展开原先先访问状态再校验原请求。改为先校验，再读取与
  展开，之后复查有效请求及客户端限制。
- count 和 compact 与 create 的 null 合同不同，分别归一化自身的
  合法缺省值，不能因为复用 create 而误拒绝。

44 个冻结向量来自 SDK 类型图：32 个顶层字段的 null、5 个 reasoning
嵌套字段的 null、7 个服务档位。CPU 同时比对 C++ 和公开 schema；
不是只检查字段在白名单里。

## 验证范围

先完成成组修改，再运行 13 项 C++ 与 2 项 schema、2 项业务驱动
CPU 检查，包含缺陷复现、模板读取顺序、count/compact 默认和有效
模板／客户端限制直接回归。必要 host/引擎构建零警告。

实际业务上限：1 次模型启动、2 次生成、2 次计数、10 次无效请求、
600 秒，同一严格 GPU 租约；JSON/SSE、合法缺省值与实际服务档位、
错误 param 和无效请求不消耗生成账本。复用未改的真实工具、压缩、
模板、后台恢复证据，不重跑完整性能或整模型质量矩阵。

[CPU验收](evidence/serve-request-fields-cpu-20261110/README.md)通过，首驱动夹具错误和定向补验保留。当前默认70504790与完整回退保持，下一步实际短验，通过后交付并继续全部计划任务。

## 本组交付

13 C++/2schema/2驱动及实际1启动/2生成/2计数/10拒绝通过，
40.241秒、40 NVML全OK。默认5935be0a/b39ad17e，revision30、
STOPPED，上一代完整保留。[交付证据](evidence/serve-request-fields-delivery-20261110/README.md)。

## 未完成执行链继续入总目标

[逐tag审计](serve-responses-execution-audit-v1.json)覆盖31种input item、28种output item、59种SSE事件；不按数量宣称兼容率。文本/function/custom/reasoning/compaction核心已交付，文件/媒体、内建工具/MCP/程序执行、真实检索annotation和原生refusal信号仍须实现。max_tool_calls目前只有解析/回显，必须在A11实际内建执行器中执行总调用上限；不能按字段已接收算完成。

八种include已有存储投影，但没有producer的结果不能生成；A09/A10/A11继续实现producer及事件。先做其依赖A08留存治理，再按计划接Files/Uploads/Batch及真实工具/媒体。所有缺口仍在A07完成判定中。
