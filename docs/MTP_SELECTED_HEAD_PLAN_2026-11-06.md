# MTP扩展末行head：有界优化判定卡

2026-11-06，用户授权“你可以推进试试”。属于性能优化，执行HTTP首测，
不使用bench，不套用业务推进的小幅波动规则。

## 实现范围

从main def8cc96、默认c1bdb0a7/sidecar0efc2c0d出发，隔离到唯一工作
分支`codex/mtp-selected-head-rows`。不合并任何研究分支。

仅改变serve所用`MtpSpeculativeStepMulti`的验证后extend：完整执行
原forward与状态更新，再只投影每条非terminal序列最后一行hidden。
完整BF16 head与既有65536行BF16 subset都覆盖；一个存活槽直接偏移，
多个槽复用现有offset及sample scratch打包，logits按存活序列顺序排列。
无新增持久GPU分配，不缩小原scratch容量；原下一轮trunk仍取原末行。

只对原始T_ext=2..8且确有未消费行的固定归约BF16 small-M路径启用。
T_ext>8、无多余行、已有GEMV/GEMM ablation保留原路径。prefill、普通
decode、旧单序列MtpSpeculativeStep API、精度、target verify、KV/
SSM/PLE更新、调度和工具协议不改。量化head/QSA融合不在本项范围。

small-M源码声明与GEMV逐行固定归约一致，只作为立项依据；实际选行、
重排、完整vocab logits、argmax/global-ID与输入/状态无副作用仍须独立
检查，不能凭删除计算直接认定等价。

## A：首轮HTTP可行性关卡

固定顺序：full父→full候选→subset候选→subset父。每臂一次启动，
max_seq1/prefill8192/max_len208896、k3、startup-plan off、suffix/cache
关闭；完整采用既有同身份模型、head产物与固定输入，不清页缓存。
每臂1K输入×3、各256输出，加11个固定质量题覆盖五档，各最多32输出。
总预算4次完整模型启动、56HTTP、1800秒；无warmup、连接生成或重试。
每阶段完整驱动持有原仓库共享GPU锁，公共监控收尾必须通过。

事前投入门槛：两种head各自的短档aggregate decode
`sum(output_tokens-1)/sum(latency-ttft)`相对父至少+3%，并且同输出的
aggregate E2E时间下降。原始TTFT、首请求和每请求时间全部保留；
不把首轮单启动配对当统计证明或用该门槛建立通用容差。
全部响应的prompt/文本/计数/finish、11题期望必须符合，普通父候选
比较仅在同head内进行，不强求完整head与subset彼此逐字一致。

环境/监控失败记ENV_ABORT，不作候选NO_GO。质量/输出失败停止定位；
有效观察未达到投入门槛则NO_GO_FEASIBILITY_TARGET，保留默认与全部
记录，不补样本改变结果，也不自动进入B。

## B：仅A通过后执行的完整接受范围

总预算5400秒、11次模型启动、91HTTP、1个独立GPU合同进程（≤900秒）：

- 4臂各补4K/8K/45K/200K×3、每次256输出：48HTTP/4启动；复用A
  每臂1K三请求及11质量，但明确分属两次启动，不混作同一启动样本。
- 当前plain基线五档×3：15HTTP/1启动；ordinary decode源码未变，
  不把MTP与plain跨模式文本差异自动归为本候选错误。
- 两head父候选四槽固定repeat材料各4请求：16HTTP/4启动；明确
  T_ext>8保持原内核，保护吞吐、输出、结束和槽间隔离。
- 两head候选各6个缓存/取消/恢复请求：12HTTP/2启动，保护pending、
  terminal边界和失败清理，使用既有接受的完整状态合同作为未变参考。
- 独立直接合同在HTTP之后执行：真实head全部logits逐位比较、末行/
  多槽行打包、顺序/边界拒绝、global-ID、scratch canary及状态不变；
  明确区分精确重放与局部高精度参考。必要构建不计为测试。

每模式每档TTFT、decode、E2E及首请求、统一RAM/清理分别审查，
不能用别档收益抵消已确认的退化。需要适用五档、数值、业务合同
完整且环境有效后才决定接受。统计不确定性和工程决定分开记录；
不自动扩样、扫描参数或放宽正确性。

若接受，另预留900秒、2启动/6HTTP完成精确回退包、默认程序与元数据
交付健康；失败按原身份回退。若拒绝，研究代码和证据提交推送，默认
保持。本次不开展长稳，也不处理实际内网部署。
