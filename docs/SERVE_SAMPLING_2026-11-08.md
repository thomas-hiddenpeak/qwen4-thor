# A03：标准生成参数的实际执行

消息子集已交付，总目标仍active。本组解决当前把非中性生成参数
拒绝的问题，不能只扩白名单或仅新增未接入的采样函数。

## 实现合同

- Chat默认/null温度按已抓取官方合同为1，top_p为1，penalty为0，
  n为1；显式temperature=0保留greedy。标准改变及有效执行路径
  必须在能力/文档/发布中说明，不把默认采样宣称为greedy MTP。
- temperature范围0—2，top_p范围0—1，presence/frequency为-2—2；
  token bias为-100—100。token ID对实际词表验证；penalty使用本
  choice已生成token计数，不混入别的请求。top_p=0至少保留最大项。
- seed使用原始JSON词法解析有符号64位，避免binary64舍入损坏。
  每个choice独立RNG/历史，以seed和choice index派生稳定种子；
  可复现以同执行版本、输入与参数为条件，不跨后端承诺相同序列。
- 模型/grammar/thinking边界先约束候选；采样绝不绕过mask，grammar
  接受后才更新选择历史。原MTP和packed argmax只用于其成立的
  greedy合同，其他请求走真实目标logits选择，不能套用错误算法。
- logprobs/top_logprobs实际返回token、原始bytes和概率。以已读
  vLLM默认raw_logprobs为明确约定：原始有限模型分布，独立于
  penalty/mask/nucleus；所选token不在原始top20时遵循官方-9999。
  文本UTF-8切分、推理/工具隐藏、stop截断与SSE重组必须一致。
- n实现独立choice、JSON/SSE index、usage汇总、取消、请求和配额
  结算；不能用循环伪造多请求而重复计数/重放。prediction同属A03，
  须实际比较/消费预测token并报告usage；本组总项不因先实现采样
  核心就关闭。

## 验证与推进

先成组完成采样核心、生成/grammar调用、响应/schema/客户端和资源
计数，再统一做针对性CPU：固定logits数值、概率归一化/边界、seed
精确性及choice隔离、mask与penalty、token字节、协议终态/取消。
必要业务短验执行前冻结实际请求和身份；不跑全模型正确性或完整
性能矩阵，不因一个子组件或阶段完成停止总目标。

采样/choice/logprobs子组已接入生成、grammar、配额、JSON/SSE、
源码schema和JS/Python客户端。CPU及真实HTTP针对性验收已通过，默认更新为7d157168。继续prediction目标
验证/消费及usage，A03整项保持开放，不能因子组交付停止总任务。

logprob只关联至少有一个可见content字节的原始token。若一个token
跨越推理标记或stop边界，保留其实际完整bytes/概率且仅发布一次；
不伪造切片概率。因此这类边界的logprob bytes拼接不保证等于content。
普通UTF-8分片保留原bytes，token显示字符串用U+FFFD修复。原始
分布排除模型padding行；候选中的实际特殊token仍有可解码表示。

多choice只有一个HTTP生命周期，JSON等待全部完成；SSE各choice
终态后才发布一次汇总usage/DONE。中途失败不能变为整体成功。
每choice重建grammar/RNG/history，前一choice回收后才执行后一项。
JSON总响应上限32MiB，logprob待决和非流式收集各16MiB，超限错误
结束当前请求。选择计算属于功能fallback，不声明性能收益。

## 依据

2026-11-08已读取的[官方Chat合同](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
与只读`reference/vllm/vllm/v1/sample/sampler.py`。独立C++实现，未
复制vLLM代码；其raw/processed logprob区分用于明确兼容语义。

## CPU与业务冻结

CPU已通过56项C++、66个输入/schema、53项Python、41项JS、3项
驱动和1项实际tokenizer探针。首次编译类型错误和夹具/导入/路径失败
保留在`.q4t-work/serve-sampling-20261108`，已通过身份证据复用。
业务驱动`tools/serve_sampling/acceptance.py`计划1启动/9Chat，累计
上限2启动/12Chat/900秒、同一严格GPU租约。覆盖默认采样、同seed
n=2的JSON/SSE、bias、strict n=2、真实资料工具与引用、显式取消
和随后greedy MTP请求。无性能接受主张或完整模型质量矩阵。

## 真实验收与可回退交付

实际1启动/9Chat：默认采样、seed固定的n=2 JSON/SSE完整choice与
usage一致、bias改变目标偏好、strict n=2、真实资料工具标题/引用、
显式取消及随后greedy MTP同PID服务通过。请求计数9/成功8/取消1，
slot_wait与target_ready均按9请求统计。驱动55.480秒，完整严格租约
55.777秒、53次NVML全OK。清理模型exit0、grammar回收、端口/锁释放。

576源码逐项核对Git提交94c0fc65；最终零警告构建7d157168、协议
e446567f。原生入口ba6baf0e、Guardian7a57af71及44控制面文件不变；
继续已验CUDA探针ef1af475。首轮发布准备导入路径和CPU记录文件名
错误保留，修复后封包通过，未追加GPU。180份原件见
[证据索引](evidence/serve-sampling-20261108/README.md)。

默认引擎7d157168/元数据b3dde202，服务78b76a7b/发布456fb5bb；
旧47d33690/ef9183ed及4e4e1872/d05b470a、原service.json与symlink
目标均有可核验回退记录。GPU/Guardian/writer三锁下安装，ledger
字节/STOPPED/预算保持；策略、Key、分钟配额不变，help/status检查
0启动/0Chat。此为采样子组交付，prediction和A03整项仍开放。
