# Chat 标准缓存合同与实际状态复用

2026-11-16，继续 A01/A07/A12/Q01/Q02；前一组 rev166 已发行，PR32
最终head检查中。全部33项master保持required；本组为兼容性业务实现，
不声明性能收益，不运行全模型质量或五档性能矩阵。

## 已核对的问题与实现

- Chat顶层仍拒绝prompt_cache_options，文本块拒绝显式断点；即使文件
  解析保留断点，生成时也只采用Responses的坐标。这些均须接入执行。
- Chat采用独立API合同：options只含mode和ttl，mode为implicit/explicit，
  顶层ttl为30m；内容块的显式断点可继承ttl或指定30m/12h。显式null、
  错误类型、未知字段和错误角色/内容类型须在准备文件或占用槽之前拒绝。
  预热和comparison_response_id属于Responses，不能当作Chat控制混用。
- 文本、instruction、user、assistant和tool内容的原坐标经实际模板与
  tokenizer字节偏移映射到完整token边界；不猜字符数、不跨NFC簇，
  不删除原请求/历史中的标记。文件展开后的坐标由最终Chat规划重新收集。
- Chat默认隐式缓存；原生prefix_cache_scope显式入口继续原合同，不能
  与标准缓存控制/内容断点混用。显式模式无断点必须既不读也不写。
  复用已有客户端隔离、key摘要、有界SSD状态、TTL和可选缓存故障隔离。
- Chat创建参考规定latest80可匹配断点，本组按该接口定义在实际唯一
  token边界排序后保留最近80个；写入仍为隐式1+显式3或显式4。官方
  指南仍描述first2+last50/20 earlier，二者存在差异；不把这一差异隐藏。
  既有Responses已验合同保留，后续独立核对其版本迁移，不靠本组宣称完整。
- 实际视觉状态指纹/媒体缓存仍是独立必做项。携带图像时本组保持真实
  cache miss和普通执行，不允许text-only KV复用造成错误结果。接受
  媒体断点本身不等于已经实现媒体状态恢复。

依据：[Chat创建参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、
[缓存指南](https://developers.openai.com/api/docs/guides/prompt-caching)，均于
2026-11-16通过OpenAI Docs读取正文。最低1024 tokens、TTL及写槽上限
与本机执行/存储约束分别记录，不冒充云计费或驻留保证。

## 冻结验证

先成组实现和必要构建：最多20项直接C++用例，少量schema/客户端/驱动
CPU用例及最多24项native/schema合同对照；覆盖嵌套参数、默认/显式、
精确坐标、写槽/80边界、TTL、存储保持、客户隔离与失效安全。
已通过且身份不变的控制/资源证据复用，具体失败只重验受影响部分。

CPU通过后冻结最多1模型启动/8次Chat生成/900秒的严格GPU租约：
实际SSD写入与重复JSON/SSE命中、显式无断点、key隔离、不同suffix
结果、默认隐式和目标解码路径。每个hit必须有实际状态恢复及usage
对应证据；配置关闭/失能不冒报hit。必要主轻/封包/回退边界另行冻结，
仅验证新身份和直接受影响合同；不覆盖原失败、不反复采样追性能。

本组交付后继续其余Chat字段、Responses差异、媒体缓存/后端、客户端、
人工回退outer、冷启动/资源和其他master任务，不在阶段完成时停止。

## 针对性 CPU 结果

[20项C++/4项schema/3项判定器/20项原生对照](evidence/serve-chat-cache-cpu-20261116/README.md)
通过；零警告，原导入失败保留，0GPU。实际短验冻结为8条：显式双断点
写入、改变后缀的SSE命中、另一客户端同key不命中、同客户端另一key
不命中、显式无断点禁读写、无任何标准控制的隐式写入、target严格JSON
写入及其SSE命中。总请求8，两个客户端实际TPM结算各自核对，不增加
模型调用或性能采样；主轻/版本恢复等后续按身份确定必要边界。

[首次实际驱动失败](evidence/serve-chat-cache-business-failure-20261116/README.md)
保留：5项已执行且原捕获复核通过；可选零缓存details假设修正，两个
CPU正负控通过，引擎不改。只补原八项中未执行的后三项，额外最多
1启动/3生成/600秒；累计2启动/8项，不重复前五，也不重跑完整质量。
