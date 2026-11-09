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
