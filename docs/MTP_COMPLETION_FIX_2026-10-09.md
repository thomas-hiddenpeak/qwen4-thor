# MTP 位置与完成边界修复（2026-10-09）

主线 Goal 继续，首组 `90d5f539` 已提交。此组为已确认缺陷修复，
不新增提速候选；在构建、直接测试和模型请求前冻结以下范围。

## 实现范围

- `MtpForward` 已有 host/device 两类位置调用，固定 H2D 与实际
  device 来源不符。使用生产共用的 UVA 方向推断复制，明确源
  在完成前保持存活且不可修改，不改变位置值、形状或模型精度。
- `ModelVerifyMulti` 自有上传数组及 RoPE 标量必须持续到 checked
  completion；成功与失败均在内部完成后返回，checkpoint 有效性
  在成功完成后发布。不能仅在外层等待已被内层析构的临时数组。
- T4 multi step 在所有已提交工作的 Status 出口检查 stream 完成；
  失败清空可消费结果，不允许沿部分推进的状态回退普通 decode。
  scheduler 在 model 锁内标记 sequence 失败，再通知请求线程。
- 审查 draft 循环复用位置上传源的生命周期；如依赖隐含同步，
  改为预构造不可变位置切片，避免扩大为逐轮额外同步。

只承诺上述 Status 出口和当前 HTTP 的 S1/slot0/default stream。
分配异常、进程终止、硬件致命 CUDA 故障的恢复不在本组注入合同内。
EOS/输出尾部消费由并行审查另列后续修复，不因此关闭总 Goal。

## 直接检查（执行前）

新独立 build 目录，服务、显式故障变体与生产位置复制检查目标
零警告。位置检查使用真实 CUDA 和同一生产 helper，不加载模型：
pageable/pinned/device 三来源，default/nonblocking 两 stream，
1/4/8192 三行数，共18例。固定整数预期值、两侧guard、源不变及
同 stream 前驱写入顺序全部逐项检查；环境不可用报失败，不跳过。

新增接口的错误结果合同如能直接执行，限定无 GPU 提交的非法参数
与结果 sentinel；不得用模拟实现替代真正完成边界的 HTTP 注入。
原已通过 host 和 sequential 控制仅按受影响依赖决定是否复用。

## HTTP 修复证据（执行前）

最多10条固定请求，不追加暖场或有利采样：

1. 链接变体9条，显式 T4、S1/k3、208896/8192。固定既有1K输入、
   greedy/seed20260920/max256；fresh control 后依次为四次故障及
   各自同模式恢复。故障点是 verify sequence-ID 上传、完成的
   verify 返回、首次自然 checkpoint restore、首次真实 extend
   attention。每个注入先执行真实操作成功，再返回逻辑失败。
2. 每个故障立即核对真实 step 返回前有注入后的同线程/stream
   checked completion，结果不可消费、caller position/history
   不变、sequence Failed→Idle、无普通 decode fallback。上传故障
   还须在内层 verify 返回前排空且 checkpoint 无效。恢复的全文、
   usage、finish 与本变体 fresh control 一致，检查实际 T4 路径。
3. 自然 restore 未触发即 coverage FAIL，保留结果，不换输入重采样。
4. 新生产服务1条 sequential，以同一1K/max256输入对比首组已有
   结果，覆盖位置复制修复影响的真实初始化、草稿与extend。

使用已有 evalscope HTTP transport/记录工具；测试实现与输入、
候选、链接参数、环境和 checkpoint metadata 在启动前再绑定。
变体无生产注入开关；不依赖同 TU 的 MtpForward 链接拦截。
四次预期错误不允许伪装为正常finish/usage；硬件故障与OOM未模拟。
只有具体失败修复后重验受影响项，原始失败不覆盖。

## 接受边界

新增同步可能有成本，10条补验不宣称五档性能持平或提升。
旧 sequential/T4 质量与五档属于其原受测身份，不改写为新实测。
本组需独立审查、真实补验、状态/日志及证据封存后阶段提交；
完成本组不等于 T4 数值准入或整体主线 Goal 完成。

## 测试前合组扩展：T4终止与最后输出

第二组尚未构建或测试时，终止边界审查已收敛；将以下修复纳入
同一最终候选，避免逐文件构建/推理。本节替代前面的独立后续安排，
原18例及9+1请求清单仍保留，不将新增条目写成已通过。

- HTTP两类verifier共用完整步准入：k3要求output/context各至少
  剩5；剩1..4走已有普通尾部，保留最后输出未消费。
- T4接受循环在每个可达target预测先检查stop，再比较draft。
  stop作为未消费next_b返回；提交非stop前缀，按consumed-1恢复
  checkpoint（四行全接受不恢复），terminal不做extend，next_d0=-1。
  物理T4仍可能计算后来被丢弃的投机行；不称stop从未进入forward。
- Multi末参增加stop span；HTTP传实际集合，默认空仅保持历史
  raw研究调用语义。混B打包跳过terminal；正式HTTP仍S1。
- generation在emit前统一验证count、非stop前缀、correction及seed；
  日志区分合法普通尾部与错误fallback。工具只对显式新T4日志承认
  pure-tail，不改变历史缺verifier原件解释。terminal分段trace
  标记不具备旧schema完整资格，保留真实零extend，不伪造计数。

直接验证包括实际生产准入谓词output/context剩1..5的25格、
非法/溢出边界；保留旧strict名兼容。模式解析新增两组正反例，
运行受影响acceptance模块。完成/终止变体的纯解析工具也需反例。

另增独立terminal链接变体9个固定1K HTTP：前4在真正T4 verify
argmax读回后，将前序预测设为实际草稿，首stop固定在row0/1/2/3；
后5为max_tokens1/2/3/4/5，其中5通过受控选择全接受与非stop
correction，确保一个真T4步加最后pending输出。先执行真实模型，
仅变体替换选择结果，保留真实原值，明确不属于自然质量/数值准入。
前4核对恢复索引、terminal跳extend、无效seed、提交前缀不含stop，
输出计数分别2/3/4/5；后5核对提交位置分别prompt+0/1/2/3/4。
自然早EOS或提前stop使预设形状未覆盖时保留coverage FAIL，不换题。
stream/nonstream交错，顺序与变体身份在执行前绑定。现阶段共19条
修复HTTP（9故障、1生产strict、9终止控制），不另加生成探针。

## 最终生产候选五档成本

上述实现与直接/HTTP修复合同完成后，对同一最终生产binary统一
运行plain和sequential各五档×3，共30请求；context固定1024、
4096、8192、45056、204800，max256、greedy/seed20260920、正式
S1/208896/8192。输入复用原冻结performance-off-04字节，模式
顺序plain后sequential、每模式上下文递增；三次全保留，无暖场、
重试或有利采样。已有run_acceptance/evalscope工具执行，严格
比较输入、全文、计数/finish与实际路径，保留原DB与全部成本。

本次提供当前版本普通/strict的实际代价，不新增T4优化或宣称
相对旧main因果无回退。必要修复不因旧错误路径更快被否决；发现
实际路径、输出、状态异常时保留失败并只修复/重验受影响项。
旧五档成本仍保留旧identity，不替代这次最终候选记录。

## 第二组实际结果与阶段边界

生产候选SHA256为`eae6949bb275e9c97c3e37c505eddc0988f585daaab01ec3b587c02b80cf5872`。首次构建测试wrapper链接失败：
span类型可读签名相同，但手写替换编码不是实际导出名；保留原失败，
仅修正两变体及CMake链接名。增量补构建3.819秒、零警告，已构建
生产与常规测试二进制SHA保持；未为该工具错误重跑生产构建。

5项尾部policy、3项sequential生产控制、18例真实CUDA复制、28项
模式解析、15项完成合同与11项terminal解析首次通过，共54项Python。
19条冻结HTTP首次全部通过：9条T4逻辑故障/恢复、1条生产sequential、
9条受控终止/短输出。四故障均先成功执行真实操作，再注入逻辑失败；
内外排空、无效结果、Failed→Idle与四次同模式恢复符合合同。自然
restore在第一次step实际触发，没有换输入。三个服务均正常退出0，
无强制清理或遗留进程组。终止控制39.370秒，仍不属于自然质量或
T4全链路数值准入。生产sequential全文/计数/finish与M1对照一致。

原始协议、请求/响应、完整SSE/usage、观察日志、来源身份和首次
失败保存在`.q4t-work/mtp-mainline-20261009/`。测试变体无生产注入
开关；direct位置非默认stream通过不扩大为完整T4非默认stream支持。
最终30条五档正在按原协议执行，成本尚未结论；总Goal继续。
