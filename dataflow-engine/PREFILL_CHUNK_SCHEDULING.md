# 计划 P：把文本 prefill chunk 接入服务调度器

2026-09-21，源码基点 ec64259。本文件为下一阶段设计，尚未实现或
验证；不将方案当作性能收益。代码优先于旧注释中的 PD-ready 描述。

## 已核对的调用事实

- HandleChat 的长 prompt 虽然已调用 ModelPrefillTextChunk，但仍在
  整个循环期间持有 model_mu_。44K 和 200K 正式单流 TTFT 约 31/160
  秒；这不是并发阻塞时间测量，但说明锁内工作可能持续很久。
- Model 的 d_ws、d_trunk、d_ids、d_positions、d_ple_emb 等是共享
  scratch，序列状态才按 seq_id 池化。服务的 d_prefill_logits_ 也共享。
- SchedulerLoop 当前只接收短 prefill 和 decode/MTP。两个提前
  continue（pending.empty、plain_reqs.empty）会跳过轮尾新增工作；
  加 chunk 处理时必须显式处理“只有 chunk、没有 decode”的情况。
- 同一轮先处理短 prefill，再处理已收集的 MTP/plain decode。pending
  集合在执行前冻结；执行过程中到达的请求需在下一轮重新收集。
- ModelPrefillTextChunk 的成功仅表示工作已排队，host position/history
  已推进；它不证明 GPU 完成。现有 FinishHostReadback 才检查结果
  复制和同步返回值，失败标记服务不健康。
- PrefillReq/ActiveRequest 都借用请求线程内存，线程等待 done，调度器
  在 sched_mu_ 下写结果并唤醒。新 chunk 请求必须延续这个生命周期。

因此不能把“每块解锁一下”视为调度实现：mutex 不保证交接给 decode，
未检查的设备工作也不能变成可发布的状态边界。

## 下一候选的执行规则

普通文本长 prompt（scheduler 可用、MTP 未加载、无视觉输入）注册
一个持久到整个 prompt 结束的 chunk 请求。单流也经过此路径，使
44K/200K 正式门禁能覆盖它；不靠仅在并发时启用来绕过正常 E2E。
旧短 prefill 的批量化保留，MTP/视觉路径先保留原驱动。

请求携带完整 prompt 指针、长度、ModelSequence 指针及最终 host
logits 地址。请求线程等待最终完成，不在 chunk 中间同时修改历史、
回收 slot 或销毁请求对象。调度器是这个阶段唯一的 host 状态写者。
每轮：处理已选的短 prefill 和 ready decode，然后最多处理一个长
prefill chunk。未结束的长请求排到队尾；下一轮重新收集 ready 集合。

一次 chunk 的边界：

1. 取得 model_mu_。首次 BeginSequence，后续用 seq 的位置继续。
2. 计算 min(max_prefill, 剩余长度)，调用 ModelPrefillTextChunk。
   保持现有首块/末块 logits 策略，不在本阶段改变 head 或 kernel。
3. 非末块检查 stream 完成；末块在锁内读回最后行并检查完成。
   检查失败后标记序列不可继续、服务不健康，禁止重新排队。
4. 释放 model_mu_ 后，在 sched_mu_ 下排回未完成请求，或标 done 并
   唤醒请求线程。不得在持有 sched_mu_ 时等待 GPU/model_mu_。

这里选择现有 stream 同步作为首个完成边界。没有多个 GPU stream
重叠时，不先增加一套 event 对象；以后若引入真正的异步资源回收，再
根据最后消费者增加 event。增加的 chunk 边界同步需经过单流实测，
不能宣称免费，也不把 host 游标等同于设备提交长度。

## 公平性、生命周期与停止

- 保证的是“每轮最多一个长 chunk，然后重新观察 ready 工作”，不是
  固定毫秒延迟。8192-token chunk 本身可耗时数秒，仍可能造成明显
  decode 间隔。chunk 大小优化是后续独立实验。
- 不先实现设备抢占，不在层中间交接，不并发使用共享 scratch。
- 多个长 prefill 使用轮转队列；有 ready decode 时不得连续排完某个
  长 prompt 的所有 chunk。短 prefill 批次预算仍沿用现有容量限制。
- 停机分支必须唤醒所有排队 chunk 请求并返回失败；正在执行的 chunk
  完成或失败后不得重新排入已停止的队列。正常/失败/停止均只能发布
  一次终态。移出队列的指针在完成通知后不再解引用。
- 沿用当前等待期间的客户端断开行为，不承诺立刻取消 GPU 工作。
  更及时的取消需要独立信号和资源回收协议，不能让请求线程直接释放
  仍被调度器持有的 slot/buffer。
- GPU 不健康时未开始的 chunk 直接终止，不继续对错误设备提交新工作。

## 验收计划（未运行）

必要构建后第一项仍为 tools/evalscope HTTP：质量 11 题，再单流
1K/4K/8K/44K/200K 各三次，保持 MTP off、精度、容量与固定输入。
与直接父版及 fcb5925 双参考比较。异常先配对 HTTP，不先 profile。

新调度行为还必须有并发 HTTP 证据，不能用单流门禁替代：

- max_seq=2、max_len=65536，确认服务实际容量未被预算器缩减。
  长请求使用 44K prompt/output=1，期间提交 1K/output=256 的短请求。
  长请求只输出一个 token，避免它加入 decode batch 而把批量数值
  差异混入本次调度对照。父版/候选相同输入和到达顺序各重复三次。
- 记录每条请求的 TTFT、完整输出和完成时间；确认短请求在长 prompt
  尚未全部完成时取得首 token。记录短请求流式停顿，不能只看平均
  decode tps 掩盖每块期间的数秒停顿。
- 并发可改变单条请求的等待时间，不能要求和串行完全相同，也不能
  用短请求改善掩盖整体吞吐下降；分别报告单流门禁、各请求延迟及
  同一工作负载的总完成时间，不编造容忍阈值。
- 两个长请求轮转、停机时排队/执行中请求终止、slot 重用、错误后不
  重新排队需要专门 HTTP 场景。正式性能环境和故障注入环境分开。

只有这些 HTTP 场景通过后，才分析调度顺序/时间线、同步增加与容量。
若正常单流或输出不保持，先修复或回退，不以设计目标覆盖回退。
