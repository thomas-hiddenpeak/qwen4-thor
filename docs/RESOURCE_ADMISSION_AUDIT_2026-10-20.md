# 运行时资源准入与回收合同审计

2026-10-20，非性能可靠性任务。用户授权设目标、发现具体缺口再修复，
有界验收，具备条件后直接收尾。不涉及CUDA Graph或性能优化。

## 冻结范围与预算

- 启动预算、实际max_len/max_seq/max_prefill、文本长度与MTP窗口边界。
- HTTP body/连接/活跃handler上限、槽位排队与正常/取消/错误回收。
- GPU失效后，已排队或已取得槽位但尚未执行的请求不能继续提交模型工作。
- 不扩展为进程OOM恢复、真实硬件故障恢复、全部视觉质量或性能研究。

首轮预算：旧默认1次受控故障复现；候选53项host，普通/MTP各3条
HTTP质量（1024/8192/45056），既有7-handler正常/超长/排队取消/
超时/ID复用合同，以及4种故障路径（槽位1/2×调度/inline）。
部署普通/MTP各1条健康请求；无性能矩阵、无统计追加。每个完整阶段
持有同一NVML GPU lease；只有具体失败才修复并重验受影响项。

## 静态审计

启动ValidateServerOptions先于加载；ComputeMemoryBudget不接受不可行
结果，实际容量来自budget而非原始请求；--no-budget为显式绕过。
预算估计不是系统可用内存保证。预算启用时MTP单流限制与k/window保护已存在。
请求T>=max_len拒绝，输出受长度边界限制；MTP预分配不足回退普通。
HTTP body使用lease自动归还，连接、handler、槽位有独立上限。
正常/排队取消/超时/超长/ID复用沿用最近已通过场景，候选定向再验。
异步提交完成后才通知等待者，清理不会因取消提前逃离scheduler等待。

已复现缺口：入口健康检查在排队前，AllocSeqId未重查GPU健康；
普通批prefill、inline prefill/decode、MTP init/step与普通scheduler
step未统一在model_mu_内阻止已失效后的新模型提交。chunk prefill已有
检查。修复应保留失败通知、drain与资源清理，不能只停scheduler导致
请求永远等待。失效服务须重启，不以槽位回收宣称GPU已恢复。

## 证据

本阶段目录：.q4t-work/resource-admission-20261020。
候选3ae53f26已验收，默认替换见[部署记录](RESOURCE_ADMISSION_DEPLOYMENT_2026-10-20.md)。

## 实测结果与工程决定

旧默认3eb9b2a0：受控首个D2H错误返回500并将GPU标记不健康；
已排队请求仍执行、返回200，产生第二次logits回读。旧行为实际复现，
不是从轮询推断。复现服务exit0、NVML lease COMPLETED（约32.90秒）。

修复在AllocSeqId中拒绝不健康GPU（503）；已准入请求在取得model_mu_
后再次检查，覆盖普通批prefill/decode、inline prefill/decode、MTP
init/step和vision入口；chunk prefill原有检查保持。已有失败drain、
完成通知和清理出口保留，不停止scheduler而留下等待者。

候选零警告，53/53 host、普通/MTP质量各3/3、7-handler回收合同通过。
4种受控故障（slots=1/2 × scheduler/inline）全通过：第一请求500；
单槽位等待者503且未准入，双槽位已准入等待者500；每场景仅首请求
产生一次logits回读。全部槽位回收，health及后续请求503，所有服务
exit0。单一NVML lease约335.22秒；286文件封存复核。首次通过，无重试。
[验收审计](evidence/resource-admission-20261020/acceptance-audit.json)。

有界质量请求相对上一默认的历史同输入单次结果，TTFT变化为：
普通1024/8192/45056：+0.942%/-0.051%/-0.213%；MTP：
-0.166%/+0.089%/+0.060%。E2E变化分别+0.794%/-0.046%/-0.204%
及-0.231%/+0.098%/+0.060%。[原值](evidence/resource-admission-20261020/bounded-timing.json)。
这是未配对历史单次观察，不是因果估计、零损失证明或性能优化验收。
按用户非性能原则作ENGINEERING_ACCEPTED_RELIABILITY决定，不追加采样。

## 边界

故障注入是可恢复的CUDA返回码，用来检查已经标记不健康之后的行为，
不模拟真实GPU挂死，也不证明所有CUDA错误都已被识别。MTP/vision
失效入口本轮为静态审查，MTP正常路径有质量检查；未做视觉故障注入。
资源回收表示请求槽位与退出流程完成，不代表GPU恢复健康或OOM可恢复；
故障服务继续拒绝请求并要求重启。容量估计仍不承诺系统不会OOM。
没有改计算kernel、数值策略或健康状态下的调度算法。

