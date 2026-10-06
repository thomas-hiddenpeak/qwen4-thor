# Offload GPU 缓存源码一致回放 — Goal 启动

更新：2026-10-06。承接16be72c，独立分支
`codex/offload-gpu-replay-20261006`。本阶段先做纯host离线回放，
不重复HTTP、启动模型或修改runtime；旧NO_GO、默认关闭和54GB未知保持。

## 冻结范围

[范围计划](evidence/offload-gpu-replay-20261006/scope-plan.json) SHA256
`e294b25ea6f594bffea2de1aef35f17f80cca51f7949d9ef1affcaafcca43fd7`。
只复用上一轮m02-AS、m03-CS、m06-CL、m07-AL四个on服务，各4请求，
共16条完整路由。k0前驱、k1主端点、k2/k3分别保留；不挑样、不拼旧64。
每服务仅从k0入口初始化，随后连续推进4请求，不用观测末态重置回放。

基线必须复现全部80个互不重叠阶段区间的GPU计数，以及每条请求入口和
prefill/decode末态的48层物理slot专家、raw tick、保护位与raw clock。
前1/8/32步没有逐层快照，只能核计数；不能虚构中间状态证据。
基线任何精确核对失败即禁止候选，先保留差异，再定点修正有证明的工具缺陷。

## 源码约束

运行时6b51693/binary8447d898不变。旧离线工具没有快照/rawclock恢复和
当前请求分块策略，不能直接作为精确基线。新增纯GPU核心，省略无法
重建的lower-cache并发；每个chunk提交完成后才规划下一chunk，成功
路径的GPU末态不依赖worker提交次序。

PlanResolve每非空call增加一次clock，先标完整needed，再按原顺序处理；
保护槽、reserved槽及本次仍需要的resident均不可驱逐，重复planned miss
随后计hit。优先最低空槽，否则最低tick、同tick最低物理slot。
真实C++ lex std::sort的同键顺序必须保留，不用Python稳定排序替代。
C的1K请求仍legacy；8193为8192行min_new加1行legacy；decode为singleton。
统计中的single/multi按chunk形状，真实prefill/decode仍按trace阶段。

## 唯一候选与收益边界

基线全过后，只评估occupied最老tick平票改为最低resident expert ID
（末位slot保证唯一）的一个候选；空槽、容量、needed和其他规则不动。
候选从相同首入口开始，之后携带自己的反事实状态。离线工作门槛为：
16条prefill各自loads不增、16条decode各自loads不增，至少一条prefill
严格减少；路由/chunk/resolve次数/容量/保护合同相同。不通过则拒绝，
不搜索第二条平票策略，不把加载降幅当成SSD/PID读或TTFT收益。

同时计算固定实际chunk顺序、实际phase入口下的容量下界：首chunk入口
缺失量，加后续每相邻needed集合的`max(0,|N−P|−(C−|P|))`。各层求和，
与入口distinct下界分列。actual减下界仍只是可避免量上界，不保证可达，
不用于限制改变分区或重排的策略。

只有明确减少工作的机会才考虑runtime。其实现前须另冻HTTP首测、
数值、完整五档加目标容量/history/质量/资源范围；当前未准入runtime。

## 任务与验证

源码、分块映射和证据schema已独立核对；原始trace仅做身份哈希，未回放。
GPU核心、只输出被选分块的C++ helper与严格runner已成组实现；本机
host构建零警告，55项直接合同（核心27/helper11/runner17）首批全部通过。
静审发现的候选前置与来源内部一致性缺口已在首次测试前关闭，记录保留。
16条请求快照/路由身份已提取到6.48MB manifest，没有新trace解析或HTTP。
准备绑定干净工具提交和精确执行命令，再做基线回放。相同身份的旧
HTTP/数值/trace合同只按原范围复用，不为host工具重复完整推理。

产物在`.q4t-work/offload-gpu-replay-20261006/`；原始证据保持只读。
入口核对MAIN七项修改/diff/主binary及228模型元数据相同；metadata
不等于payload字节证明，reference只核tracked范围。最终审查与交付
待本阶段实际结果完成后补充，目前没有新增性能或可避免加载量结论。
