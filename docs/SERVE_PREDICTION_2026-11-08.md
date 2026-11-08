# A03 prediction：目标验证和真实usage

采样子组已交付7d157168。prediction候选已完成开发和针对性CPU验证，
未验收前不替换默认。此项按兼容功能验证，不宣称性能优化。

计划复用现有ModelVerifyMulti和SSM/conv/PLE检查点：一次验证当前
已选token与一个预测token，匹配时提交两个位置并复用第二行logits，
不匹配只提交当前位置、回滚预测状态。目标分布和grammar/seed决定
接受结果，不能把prediction文字强行当成答案；不依赖MTP草稿权重。

预测内容支持字符串与text parts；分词后按实际目标输出token匹配，
accepted只计实际消费的匹配token，未消费的预测token列为rejected。
completion_tokens包含实际生成与rejected，配额和上下文预算同步。
JSON/SSE须一致，取消不发布成功usage，隐藏/停止边界不能伪造正文。

采用[官方预测输出合同](https://developers.openai.com/api/docs/guides/predicted-outputs)：
n>1、logprobs、正presence/frequency penalty、tools、audio和
max_completion_tokens组合不支持；max_tokens仍有实际预算。其余
确定兼容任务继续按总计划，不把这些模型/组合条件推广成全局拒绝。

先统一完成输入/schema、目标消费/回滚、usage/配额及客户端，再做
针对性CPU：词法/组合、预测匹配/插入/错位、部分提交、预算和usage。
真实短验事前冻结：计划1启动/7Chat，上限2启动/8Chat/900秒；匹配
和不匹配、JSON/SSE、采样目标决定、取消后继续服务与清理。
必要目标状态失败只重验受影响项，不跑完整性能或全模型矩阵。
A03和总目标保持开放，全部计划完成前不停在这个阶段。

开发期补充：第7条覆盖prediction与实际grammar组合，仍在2启动/8Chat上限内。

## CPU检查完成，真实业务待验

最终build-02四个目标零警告。27项C++（预测消费、部分状态提交、
失败不提交、预算与usage及直接受影响规划）、22个schema/生产
校验、5项Python（2客户端、3驱动）、2项JS通过；schema派生和
来源guard通过。JS使用实际固定SDK验证JSON/SSE，拒绝非法计数。

保留build-01的GCC数组范围警告及cpu-01的JS夹具失败：序列缩短
改用erase，JSON/SSE比较重组结果而非两种传输的原始事件列表。
只重验受影响JS，其他相同源码证据复用。当前默认仍7d157168，
真实短验按前述1启动/7Chat执行；本条不是业务通过或阶段交付。
