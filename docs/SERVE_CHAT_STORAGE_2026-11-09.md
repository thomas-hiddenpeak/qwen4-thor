# A08 原生 Chat 持久对象

2026-11-09开始实施。按[列表](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/list)、
[更新](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/update)和
[消息](https://developers.openai.com/api/reference/resources/chat/subresources/completions/subresources/messages/methods/list)官方合同及固定SDK7.47.0核对。

完整交付包括store/metadata真实执行、成功JSON/SSE与n choices聚合落盘，
GET列表/单项、POST metadata更新、DELETE及messages分页。默认store=false，
metadata允许null或16对64/512字符值，不作为授权身份。列表支持model与
metadata筛选、asc默认/desc、after游标；输入消息保留role/name、内容parts
和工具关联，不能把生成后的助手消息冒充原输入。

先实现独立核心：同一隔离对象worker，只有已完成的实际响应才写一个
原子对象，失败或取消不发布成功；回执未知按原ID查询，无自动重放。
列表使用真实生成时间，metadata更新保持绝对到期时间；所有存储均计
客户端/全局配额，分页内存与工作受限。无store的请求不触发对象I/O。

本组冻结11新增CPU和4直接存储回归，必要实际引擎/host构建不运行GPU。
测试覆盖Unicode边界、无存储、CRUD重启/期限、归属/重复、跨过滤页、
消息/工具/媒体保真、配额/不完整拒绝、真实丢回执、时间顺序和HTTP形状。
之后继续实际共享生成、路由/SDK、协议/发行声明及对应真实业务短验。
默认已验c704eebf/d0e5f43f保留，本核心子组不计A08或总目标完成。

核心15/15 CPU已通过，必要引擎/host构建47.298秒零警告。继续共享生成：
store=true流式实际收集choices/概率，写入确认后才终结成功；metadata
控制查询不占生成配额。接入子组冻结9 CPU（5新增、2改动、2直接）、
4官方SDK场景/5 CPU进程、2实际原生入口/TLS及1可选nginx路由验证。
同时补齐可选nginx遗漏的Responses/Conversations，原生默认保持。
声明/来源随实现更新；真实GPU短验单独冻结，不重复已通过核心组。

接入与补验已通过：[CPU原件](evidence/serve-chat-storage-20261109/README.md)。
SDK发现metadata=null序列化为metadata=，现按省略处理且拒绝混合对象
编码；损坏UTF-8在查询解析前拒绝。只重验受影响4 CPU与SDK分页，
其余三项SDK与3入口直接复用。4声明检查含25真实CPU线对象通过，
保存生成内容仍须下一组真实业务；本组未替换默认。

真实业务驱动冻结1次启动/7条Chat（两个n=2共至多9choices）/600秒，
同一严格租约；JSON/SSE存储、概率原bytes与usage、CRUD/metadata、
实际资料工具回填、原始消息查询。仅暂停自己模型的准确object worker，
验证最终写入失败不发成功终态、原ID查询及同PID核心Chat继续。
先做2项Python负控与2项官方SDK桥接CPU，保存失败及所有拥有进程
原件；默认c704eebf不变。通过后可回退发行并继续所有剩余主计划。

首轮在已完成3条生成后，驱动误读SDK分页属性导致FAILED；原始
服务列表字段完整。见[原件与续验](evidence/serve-chat-storage-prefix-20261109/README.md)。
桥接CPU补验通过，引擎未改。只续1启动/4Chat/300秒，累计2启动/
7Chat/600秒；复用前三条，私有复制原状态完成剩余工具及故障场景。
