# 完整会话缓存候选：验证进展

当前候选09fe25ba，默认f2a6保持。范围和上限见[冻结合同](SESSION_CACHE_PLAN_2026-10-27.md)。
本页记录中间证据，不是发布决定。正式性能清单位于
`.q4t-work/session-cache-20261027/phase-c/plan.json`，SHA
`3dd516410b297bfa2cedc2a3e30c64d0849f12c4fefcfe8ff688fe24499706a6`。

## 实现与使用合同

缓存默认关闭，启动显式指定私有SSD目录`--session-cache-dir DIR`，请求
带随机64位小写hex `session_id`。同会话顺序复用ID，不同agent用不同ID，
仍发送完整prompt/messages。恢复不依赖原槽位；不同历史安全miss并完整
prefill。同ID并发409；进程结束/重启无缓存；超过TTL或淘汰会miss。
支持完成请求后等待工具，不支持取消中的半个请求快照或跨进程持久会话。
`usage.prompt_tokens_details.cached_tokens`报告实际复用的目标前缀长度。

保存最后完整MTP边界，包含完整目标有效状态、draft有效状态、g/d0/pending
和最后目标trunk；最后最多4个输出token（对应最多3次普通decode forward）
与新工具内容一起重新计算。新token取代预测pending时只重建draft末行。
这不是把递归快照改名成完整缓存，也不要求新token恰好等于旧预测。

8项/24GiB SSD上限包含正在写入项，O_DIRECT匿名文件，不静默回退页缓存；
单个8MiB对齐staging，新增启动RAM预算32MiB，全部CPU/GPU复制、hash、I/O、
等待model_mu及suffix索引重建计入请求。过期在访问/淘汰时回收；退出关闭fd。
不支持媒体、非k3、无显式槽位布局、trace或SSD专家卸载组合。

## 已完成

- 两head各11真实evalscope质量通过，包含200K；首HTTP后49 CPU存储
  检查通过，覆盖逐块读取、尾块padding、校验错误/截断、失败取消不发布、
  lease互斥、TTL和8项LRU。见[CPU结果](evidence/session-cache-20261027/cpu-storage.log)。
- full实际17请求（16成功1取消），subset18（17成功1取消），共35，全部
  slot release。真实保存9227token→其它请求覆盖slot→恢复与fresh均答43，
  修改早期history安全miss；subset取消后同ID新请求通过。full的取消后
  请求尚未执行，按原服务预算并入C full ON末尾，不补开服务。
- full首取消夹具误读日志标记、subset收尾误用eager缓存审计，各在完成的
  原日志上CPU修复（4/6检查）。原STOPPED状态、退出与监控记录保留；修复
  次数2已用尽，不额外重放有效请求。旧stage总计840.219秒、2服务，无
  ENV_ABORT；这两次工具失败不标成推理失败或性能失败。
- 两head各一条GPU fixture：真实4层目标（含PLE/GDN/full attention）和
  真实draft。独立枚举状态与生产序列化分开，验证原slot0被其它请求覆盖
  后恢复slot2；有效字段逐字节一致；改pending末行修复与独立draft重建
  一致；新增17token logits与状态一致；终止draft补齐对独立extend一致。
  全阶段29.032秒、2fixture、无跳过，监控完整结束后flush。
  [full](evidence/session-cache-20261027/full-state.log)、
  [subset](evidence/session-cache-20261027/subset-state.log)、
  [监控](evidence/session-cache-20261027/gpu-state-postflight.json)。

4层合同不能代替整模型质量；完整HTTP在实际48层服务运行，仍需最终长
工作负载质量/性能。接受0/1/2/3及mixed suffix/neural的独立前缀参考只在
未改动数值分支上复用；新增terminal flag和完整状态搬移已单独验证。
续跑保持已执行状态，不证明与不同计算shape的fresh prefill任意输出一致。

## 尚未决定

C正式对照：full OFF/ON、subset ON/OFF，各重复代码、文本的
first→四槽干扰→second，第一轮约131K/32K/48K/60K，第二轮约134K/35K/
52K/63K，每路2048输出，容量含输出预算均合法。一个服务配对/每head，
只作描述统计，目标每场景总事务耗时减少≥10%、恢复TTFT减少≥50%。
之后完成五档父候选保护、跨轮记忆质量及最终资源核算；未满足不替换
默认程序，不把中间局部成功当成完整交付。
