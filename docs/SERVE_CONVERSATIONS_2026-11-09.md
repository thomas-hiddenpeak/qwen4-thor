# Conversations原生状态和请求处理

2026-11-09，A07/A08持续实施记录。当前是CPU源码检查点，默认仍为
已接受A06；生产serve入口、Responses共享生成、后台执行和发行继续
实施，A07/A08及总目标均未完成。

## 实现

`ConversationState`在原生隔离对象worker上提供会话创建/读取/元数据
更新/删除，item原子追加、按插入顺序分页、读取及成员删除。创建与
追加的会话记录和独立items共用同一持久事务，配额或revision冲突
不留下半份结果。客户端归属来自认证上下文，不能由body中的user、
metadata或已知ID指定。跨会话游标和item不能串用。

输入先按固定SDK形状全部校验，再解析有归属的item_reference，支持
省略或null的引用type；补齐合法服务端ID/status及必要的返回字段。
存储允许尚未结束的工具历史，不运行工具，也不将参数合法性或记录
存在当成工具已经执行。生成器仍负责完整调用关联、数值与执行合同。
生成输出的非法item不能提交为completed；会话重复ID不能被再次追加。

`include`只控制返回视图，原始完整媒体、logprobs和加密字段保留供
后续历史使用。metadata按Unicode字符计64/512限制及最多16项。
每次最多新增20items、会话最多4096；事务上限8192覆盖输入4096和
输出512的联合提交，原32MiB请求/64MiB日志及client/global配额不变。

`HandleConversationRequest`实现标准路径、方法、JSON和query合同；
使用同一对象worker，不通过其他HTTP应用服务转发。官方SDK在真实
本机HTTP夹具中调用此生产处理器与存储，未使用预制成功响应。

删除会话保留独立items，符合官方合同。成员删除只解除该会话关系，
不修改其他历史快照或独立引用；独立对象保留与显式回收仍须在A08
完整生命周期中核对。压缩控制item已识别，但需要真实A07模型执行
产生compaction结果；当前执行器缺口仍开放，503不算功能完成。

## 针对性证据

27个不同C++检查（13会话、9Responses状态、4事务、1请求合同）及
5个官方SDK HTTP检查通过。SDK使用6次CPU夹具进程启动验证持久重开，
不是GPU模型启动；固定测试bearer到scope映射不冒充生产鉴权。
真实事务一次提交1025对象，重开存储后全量分页读取通过。

首轮构建漏括号、截图结果误补null字段，以及两处SDK夹具误判均
保留原件；最终5次成功构建零警告，只重验修复项与直接影响行为。
[原始证据](evidence/serve-conversations-20261109/README.md)保存失败、
请求/响应、存储文件与来源摘要。无GPU、默认替换、完整性能或模型
质量矩阵。此前未修改的证据直接复用。

## 接续工作

继续实际serve/原生入口路由与鉴权、CLI对象目录和资源配置，随后
Responses生成/事件、input_items、background/cancel和重启终态恢复；
context compaction、媒体/文件/内建工具执行均保留原任务责任。
源代码检查点不是生产交付，不因此结束总计划。

## 固定参考

- 本地官方OpenAI Node SDK7.47.0；类型图和Apache-2.0许可已归档。
- [创建会话](https://developers.openai.com/api/reference/resources/conversations/methods/create)
- [创建items](https://developers.openai.com/api/reference/resources/conversations/subresources/items/methods/create)
- [删除会话](https://developers.openai.com/api/reference/resources/conversations/methods/delete)
- [按会话删除item](https://developers.openai.com/api/reference/resources/conversations/subresources/items/methods/delete)

接口与SDK在2026-11-09核对；不能把SDK类型检查替代执行语义验收。
