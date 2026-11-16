# Chat原生输入和输出审核

2026-11-17，继续A01/A02/A12/Q01，总计划保持active。rev196支持
Responses和独立Moderations的真实分类，但Chat moderation仍被拒绝。
本组复用已有本机分类器、受限生成与请求所有权，不增加外部守护程序。

## 必须完成的合同

- 标准moderation对象/null、实际模型或已配置别名、input/output独立
  score/block；不将本地模型冒充omni-moderation。统一schema、合同、
  原生预检、Python/JS及Batch均接入，字段合法必须实际运行。
- 分类输入为实际展开的消息、工具参数/结果、推理/拒绝和媒体；输出
  审核覆盖本次全部choice的实际内容、工具参数、推理/拒绝。每个方向
  各一次有界真实分类；多choice整体审核，输出block整体不公开。
  不按关键词猜结果、不把缺失/截断/失败分数填成安全值。
- 公共moderation.input/output使用标准moderation_results模型/结果
  数组或显式error。score可保留分类失败并继续正常业务；block分类
  不可用必须有限错误，命中返回content_filter，不能假装正常stop。
  取消、额度耗尽和真实设备故障始终保留其原有停止/保护语义。
- 输出block在真实批准前不发布内容、工具参数、logprobs或持久化草稿；
  SSE保留生成片段顺序，通过后释放，拒绝或失败清空，限制32MiB。
  score可正常增量输出，末尾给结果；一次终态及DONE，不能重放生成。
- 一份外部准入/取消/RPM所有权覆盖原生成和两个分类步骤，分类各512
  输出预算，累积实际输入和输出计入相同配额；失败仍保留消耗。Batch
  在执行前预收动态多步输入上限和全部输出，不能执行中借线上额度。
- 保持普通Chat、不带审核/为null时的原行为，以及工具/schema、多
  choice、缓存、文件、存储和Responses既有合同。输出审核前释放本次
  device/grammar/slot所有权，避免单流配置内部步骤死锁；外部请求仍
  活动。被挡住的输出不成为成功缓存或持久可见内容。

## 分组实现和针对性验证

先实现共享纯合同/审核状态和真实生成接入，再统一验证。冻结最多
26项直接C++（新旧受影响项）、8个schema/双客户端/CPU预检病例，
针对Batch额度与SSE泄漏/持久边界的正负控。隔离零警告构建，来源
检查；不跑完整性能和全模型质量矩阵。

必要真实业务最多4次模型启动、18个外部生成请求含自检、900秒，
一个阶段驱动一把严格GPU租约。覆盖主轻资格、正常score/block、
JSON/SSE、实际资料工具/结构化/存储、所需保留版本组合和清理；分类
内部实际步骤与token单独记录，不能当作没有额外模型工作。原失败
保留，只补明确受影响项；通过后可回退发行及实际合main，继续全部
master。24小时由用户负责，NVML失能维护仍等待原交接不干预他服务。

依据是2026-11-16已冻结的[官方Chat请求与响应参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
本地分类为当前已配模型的13类评分，阈值0.5；不声称与云端专用
moderation模型同精度/同校准。模型族适配和其他A10任务仍需完成。

## CPU实施出口

[26项C++与8种前端验证已通过](evidence/serve-chat-moderation-cpu-20261117/README.md)，
原失败及定点修复保留。引擎候选`23f93435`，默认rev196未变。主/轻
资格仅给既有schema请求加真实input score/output block与存储回读，
保留资料工具、媒体及embedding原业务，2启动/10请求；两种保留控制
包各1启动/4请求（含自检），覆盖SSE、输入block及score JSON。
内部分类工作独立记录；通过后发行/合main，再继续其余总任务。
