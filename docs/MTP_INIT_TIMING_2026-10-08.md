# MTP 初始化耗时诊断（2026-10-08）

用户授权继续 Goal。沿用 codex/mtp-admission-20261007，从 6d2c2b3
开始；旧受测服务 73c39bab、build/mtp-init-last-row-20261008 与全部
旧证据保持只读。本阶段只量化剩余成本，不追加优化候选，不把上轮
NO_GO 改成接受。默认 MTP 关闭，不合并、部署或改变默认入口。

## 冻结范围

新增默认关闭的 Q4T_MTP_INIT_TIMING=1 诊断，仅用于 MTP、单序列
文本 HTTP 初始化；请求拥有诊断对象，显式指针传入初始 DraftExtend
及其 Forward。旧调用默认空指针，实际 k3 decode 不增加 GPU 计时。
保留所有数学、行数、内存预算、同步和调度合同，不测 kernel bench。
构建独立放 build/mtp-init-timing-20261008，证据放同名 .q4t-work。

计时按两个时钟分开：steady_clock 记录服务请求的主模型 prefill、
trunk 分配/释放、MTP reset、shift 构造、DraftExtend、checkpoints/
scratch reserve、第一步等待和首次非空内容发送；CUDA events 记录
初始 draft 分块的输入/前投影、attention、HC、MoE、mixer 与末行
head 区间。事件只 record/query/elapsed，依赖既有 readback/drain，
不增加分段 synchronize。事件资源准备与日志/收集成本单独声明。
记录 request_id、base、rows 和完整性，不记录 prompt 或模型权重。

已有 server ttft_seconds 在主模型 prefill 结束后、MTP 初始化前记录，
不能当客户端 TTFT。HTTP first_chunk_latency 保持客户端口径；首个
内容还等待初始化和第一次投机步骤。host 调用可能含既有 CUDA 等待，
CUDA event 区间也可能含提交空闲，不称 kernel active time；两者不相加。
每段覆盖/嵌套/未覆盖残差显式校验，失效事件不替换为零或接受数据。

## 冻结验证与样本

本轮第一项测试仍是 tools/evalscope HTTP，之前只读设计与必要构建。
单 Thor 串行、正式容量 208896/8192/S1、k=3、固定精度，清理 Q4T_*
及 LD_PRELOAD，仅诊断组显式设置新环境变量。不额外连接探测生成、
预热、换输入、追加有利采样；首次失败保留，具体缺陷才定向修复。

1. 新二进制关闭诊断，固定质量 11 条，按旧 reference 验收。
2. 质量通过后完成计时工具的有限 host 合同和运行时/设备代码身份
   核对，明确旧数值证据可复用范围，不称新整体数值证明。
3. 新二进制关闭诊断的同期五档控制组，各 3 次、输出 256。
4. 同二进制开启诊断的五档组，各 3 次、输出 256；完整记录客户端
   计时、内部区间、实际 MTP 路径、输出和退出状态。

总计 41 条新 HTTP 请求，固定 1024/4096/8192/45056/204800 输入，
旧 73c39bab 五档仅作历史辅助。控制组固定先于诊断组，不声称随机
交错实验或由三次测量证明零开销。采样与原始 SQLite/响应留存；
所有组必须维持实际 MTP、容量与长度，否则保存失败、不混入统计。

## 分析与出口

按输入长度列主 prefill、MTP 初始化、首 step、首次内容等待的 wall
分解；初始 draft 的同流事件区间按模块和块位置排序，保留全部三次、
服务首请求与后续请求。检查 sums/residual、块覆盖与所有输出身份。
控制与诊断的 TTFT/decode/整请求差异只衡量观测条件差异，不宣布
算法加速或用阶段时间代替五档性能接受。旧/新/普通模式分开。

报告观测到的主要成本与理想删除该项的收益上限，并标明依赖、
重叠和不可直接删除的计算。先证明边界再提出至多一个后续方向，
不在本轮实施它；数据不足则明确 NO_GO_FOR_ATTRIBUTION。
整模型跨模式数值合同仍未闭合，本轮不把输出相同当正确性 oracle。

按实现/验证/结果提交并推送同一工作分支。保护原目录七项、wt-c1
两项未提交修改、main 与默认/旧二进制；模型/reference 只读。
成功出口是可审阅的有界诊断报告和下一步取舍，不要求制造性能收益。

## 实现与必要构建快照

默认关闭的计时接线已实现，独立 Release 零警告构建。服务 SHA
`0d3495c864879a22da02b28284f43fb33eff5baa211294539ec2d8218220ef36`，
模型库 `e2415430`。事件池最多 64 个初始化分块，host 标记最多 256，
正式最大输入只需 26 块；每块 10 个共享事件边界、9 个叶区间及
独立整块时长。中间块未执行 head 的标记间隙单列，不能计作 head。
默认空指针不调用事件 API，但 getenv/空指针判断有主机指令开销。

收集位于 request_end 之后；report_host_ms 包含查询、销毁和主体
序列化，未包含最终字段序列化、最后字符串复制及 fprintf。setup、
record、report 都不是全部插桩成本，控制组与诊断组差异另列。
CUDA 13.3 文档说明事件 elapsed 的分辨率约半微秒，事件销毁无需
等待完成；本工具不从该分辨率推导绝对误差保证。见
[NVIDIA Event Management](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/group__CUDART__EVENT.html)。

本提交仅记录实现和必要构建，尚未执行任何测试；随后第一测为
quality-off-01。新增离线检查器与合同将另行形成验证边界。

## 第一测与工具合同

quality-off-01 首次 HTTP 11/11 通过，实际 MTP、正式容量、固定
质量与零 trace 检查通过，runner/server/sampler 正常退出。旧/新
二进制提取的全部 21 份 CUDA ELF 按字节摘要完全一致；120 个既有
运行文件未变，另外三个旧运行文件的主机改动保持计算、形状、
状态与既有同步顺序。结合来源审查，复用上轮固定输入的有限数值
合同，不外推整模型等价，也不为插桩重复运行这些模型测试。

新增 tools/evalscope/analyze_mtp_init_timing.py 只读原始 HTTP SQLite、
响应与 server trace，禁止丢失/重复/非有限/未完成记录被当作有效。
20 项有限 schema 合同首次通过。随后静态发现 quality 完成数门禁
误写为 1，按实际 runner 合同改为 11；针对 load_run 以真实 11 条
质量原记录做只读集成复核通过。原件、修正 patch 与旧合同结果
保留，20 项测试未调用该计数门禁，未重跑未受影响合同或 HTTP。

整块/叶事件求和采用预先固定的每个 binary32 结果一 ULP 预算，
仅检查共享边界的一致性，不是 GPU 计时准确性保证。host 残差是
时间线上未覆盖的空档，不是 wall 减 GPU；客户端 TTFT 不与另一
时钟的 host 偏移相减。报告开销与默认关闭的少量主机指令限制保留。

instrumentation-review.json 已绑定质量、源码、设备代码及工具合同，
开始冻结的 control-off-01；随后唯一 diagnostic-on-01。运行时与
二进制保持不变，性能/归因结果尚待完成，不提前给出接受结论。
