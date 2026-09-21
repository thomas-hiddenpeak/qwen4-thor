# 文本 prefill 的逐块序列接口

2026-09-21，父版 18f1abe，固定参考 fcb5925。状态：v2 已接受，
零警告构建后首项质量 HTTP 11/11 通过，完整性能双参考门禁通过；状态专项与时间线通过，按性能持平接受。

## 问题与实际改变

原 serve 长 prompt 路径首块调用 ModelPrefill，立即将阶段标记为
kDecode；后续块调用裸 ModelDecodeBatch，position/history 仍停留
首块，全部结束后再由 serve 修正到完整 prompt。这不是逐块一致的
host 序列状态，无法直接成为后续 chunk 边界调度的入口。

新增 ModelPrefillTextChunk：以 seq->seq_id 为唯一状态池索引，
以 seq->position 为下一块起点，显式接收完整 prompt 与本块长度。
首块使用原 RunPrefill；后续块使用原 ModelDecodeBatch，以序列拥有
的历史供 PLE 查询。每块成功排队后追加历史并推进位置，最终块才
将 kPrefill 转为 kDecode。预先 reserve 完整历史容量，避免 GPU
开始推进之后追加历史再触发分配。

入参检查拒绝空指针、错误阶段、非法 slot、历史长度不一致、越界
prompt/chunk。forward 返回失败后标记 kFailed，必须重新 Begin
才能执行，不能仅退回位置再消费可能已经部分更新的 recurrent state。
旧 ModelPrefill 增加 position/history 必须为空的检查，避免在分块
中间误用整段入口从位置零重跑。

serve 长 prompt 循环使用该 API，移除最后的手工 history 修正。
保持 max_prefill=8192、首块和最后块输出 logits、中间块跳过输出头、
trunk 全 prompt 缓冲及偏移。未修改数学 kernel、QSA 选择预算、精度，
没有新增 chunk 间同步或释放全局模型锁。

## 状态含义与边界

成功返回表示在同一 stream 上成功排队，position/history 是有序
host 游标，不是设备已完成的提交证据。serve 仍需经过已修复的结果
读回检查才能发布结果，异步错误不能靠 kFailed 的同步返回检查覆盖。
函数不提供设备失败回滚；调用方不得并发推进同一序列。

仅为文本分块路径，完整 prompt 在各次调用之间必须保持不变；不在
每块计算完整 prompt 哈希。未实现多模态 chunk、MTP epoch 提交、
跨请求 chunk 公平调度或整个数据流引擎。旧单块/批量 API 保留。

## 验收进程

首版在质量请求尚未完成时，源码复核发现旧 ModelPrefill 可在部分
prefill 期间混用的问题。主动中止评估，evalscope 143、服务退出 0，
原目录 prefill-chunk-sequence-20260921 及 interrupted.json 保留。
该轮没有通过记录，不作为性能失败或超时，也不与第二版拼接。

补上整段入口拒绝检查后重新构建，当前完整独立证据目录为
`.q4t-work/e2e/prefill-chunk-sequence-v2-20260921/`。
先质量 11 题、五档各三次，对父版与固定参考按重复范围比较。
通过后才核对跨块状态、旧调用链输出、边界拒绝和失败状态，以及
长 prompt 时间线调用次数。当前没有运行前置数值/单元/故障测试，
没有调用旧 bench。尚不能宣称 API 行为、性能或资源改善已验证。

## v2 首项质量结果

质量 11/11、输出与父版一致，服务退出 0；二进制 SHA-256：
`b65bc985108fa96f16bcaeee26aede2b1018b27424b47ad671b9ef6d643935ce`。源码冻结副本一致。完整五档性能已接续；
尚未做状态专项或 profile，不以质量通过替代性能与接口边界验证。

## 完整 HTTP 门禁与门禁后核对

质量 11/11、五档 HTTP/输出 15/15，服务退出 0。对直接父版 18f1abe
和固定参考 fcb5925 均没有 TTFT/decode 不利范围分离。按持平理解，
不把微小均值变化算作提速。全部数据来自修正后的独立完整矩阵。

| 输入 token | TTFT 秒 | decode tok/s |
|---:|---:|---:|
| 1024 | 0.805338 | 18.571120 |
| 4096 | 2.683342 | 17.857736 |
| 8192 | 5.269449 | 18.130858 |
| 45056 | 30.626500 | 17.890933 |
| 204800 | 159.637531 | 17.106177 |

门禁后源码核对确认：剔除新增分块函数与旧 ModelPrefill 入参检查，
整个 model.cu 与父版逐字符一致。该证据不替代新增 host 状态行为
验证。父版/候选的 44K HTTP 时间线串行采集中；选择实际经过分块
接口的长度，不用 4K 路径代表长 prompt。仍需链接模型库，对原有
分块调用链核对逐块输出、历史、阶段和失败后拒绝继续的行为。

## 最终接受与验证边界

门禁后 verify_prefill_chunks.py 链接同一组已验收库，加载完整 48 层
模型（专项 max_len=64、max_prefill=32、max_seq=2）。旧链为首块
ModelPrefill＋后续 ModelDecodeBatch，新链为 ModelPrefillTextChunk。
三组块长 [7,5,1]、[4,4,4]、[1,3,5]，包含非四倍数块和非零 slot 1，
各块 logits 与 trunk 共 8,791,040 个 BF16 值逐位一致。每块核对
position、完整前缀 history、slot 及最终块才进入 decode。

部分 prefill 期间旧整段 API 和 decode API 被拒绝；首块/续块各
注入一次 H2D 返回错误，进入 kFailed、游标与历史不前进；再次调用
被拒绝，Begin 重置后能完成 prompt。共 14 项拒绝检查、2 次注入，
退出 0、构建零警告。返回错误注入发生在该块首个 H2D，不模拟已经
执行部分层后的硬件故障或异步设备损坏；不宣称全部入参组合已覆盖。

父版/候选 44K profiled HTTP 均输出匹配、服务/采集器退出 0。
两版块序列都是 [8192,8192,8192,8192,8192,4096]，prefill/decode
分别 537278/436815 次 kernel，全部 kernel 与 CUDA API 次数一致。
窗口耗时 prefill 31229.667→31223.792 ms，decode
14533.825→14541.384 ms；保留局部略慢结果，不把单次 profile 当作
正式性能判据或速度收益。实际 HTTP 对照维持 MTP off、单流基线。

按状态归属集中、性能持平接受，固定参考 fcb5925 不变。不同 slot
的专项不是并发隔离验证；没有 MTP/视觉/多请求公平调度验证，host
游标不等于 GPU 完成事件。这一接口为后续 chunk 边界调度提供基础，
当前 serve 仍在整个 prompt 期间持有模型锁，尚不是完整执行计划。
