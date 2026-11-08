# A03 prediction：目标验证和真实usage

采样子组已交付7d157168。prediction已完成针对性验收与可回退交付，
当前默认d61c98b7。此项按兼容功能验证，不宣称性能优化。

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

## 真实业务与可回退交付

源码73dbe96e，1启动/7Chat一次通过；驱动42.512秒，严格租约
43.832秒，42次NVML全OK。实际两行验证日志证明：完全匹配5/5
消费且JSON/SSE相同；错误预测0/3被消费，答案仍42；strict组合
5接受/1拒绝并产出合法对象；bias目标采样输出7个7加1拒绝token，
恰好占8个completion预算。显式取消返回错误/DONE，无成功usage，
同PID随后普通greedy MTP完成。进程退出0，GPU/端口/锁释放。

[128份原件](evidence/serve-prediction-20261108/index.json)与581源码
Git/副本核对；保留首次构建警告和JS夹具失败。原生入口/Guardian
构建零警告且与已验字节一致，44控制面运行文件不变，CUDA探针
沿用已验ef1a，复用TLS/恢复及采样证据。

默认引擎d61c98b7/元数据b96c0f35，协议b800ac70，v4服务ee95045b/
发布9e17f6c0。安装持GPU/Guardian/journal三锁，原ledger、Key、
策略与配额保持STOPPED，仅help/status，0模型/0Chat。旧7d157168/
b3dde202及78b76a7b/456fb5bb保留为[可核验回退](evidence/serve-prediction-20261108/delivery-01/rollback/receipt.json)。

A03定义的生成参数/choice/logprobs/prediction已完成针对性交付；
A01缓存提示等剩余字段、A04工具、A05跨能力组合和其余全部总计划
继续实施。此结果不等于完整OpenAI兼容、性能改善或长稳通过。
