# SSD offload 基础可靠性阶段结果

状态：候选19ea4e57已按可靠性任务接受，正在部署收尾。
目标整体仍active，资源基线和策略筛选尚未执行。

## 修复范围

- 专家payload H2D/mirror D2H及event错误统一经失败事务收束：等待
  已入队工作结束后才允许释放source claim，撤销可能已改写的slot身份。
- mirror在预留时先撤销旧身份；成功发布后不再由原事务回收该ring
  entry，避免失败清理误伤另一worker的后续预留。
- 完成未知时poison该residency，拒绝后续plan/loading并保留在途
  payload存储；Free先join workers，再drain，持续失败时不释放payload。
  这是保守保留直到模型销毁/进程退出的机制，不是硬件故障恢复保证。
- 成功路径仍按原event异步完成；host四标量在source claim释放之前
  读取，避免另一个worker复用源。H2D源仍是pinned，不借用局部栈数组。
- H2D错误消息保留实际返回码，不调用cudaGetLastError清除错误来拼消息。
- 两个offload runner预检改用监督超时的NVML查询；memory monitor使用
  同一查询的进程显存记录。缺失/不支持/sentinel/超时保留unknown，
  不用空列表或0替代失败；新依赖加入来源快照及审计清单。

## 验证及复用

payload-v1：53host、6类部分失败、5项真实驻留检查；payload-v2补
mirror来源与双worker失败，8类单worker+1组并行检查通过。后续发现
host标量读取顺序缺口并修复，payload-v3重新验证9类及5项驻留通过，
53host不涉及改动，身份不变复用。所有GPU阶段独占NVML，封存后复核。
故障shim进行真实CUDA复制后注入返回码；持续同步失败是先实际同步
再返回失败，用于验证保留/拒绝复用，不声称模拟真实未完成DMA或挂死。

quality-v1全驻留普通/MTP及C256 offload各3条通过；仅移动上述四次
host读取后，quality-v2重验offload三条全部通过。普通/MTP全驻留绕过此类，
复用未受影响的6条，不把旧binary写成新binary；部署当前binary仍做
两模式健康检查。offload L2=16、mirror=8、workers=16，初始空GPU槽，
未清cache、不限host/cache；只作运行时质量检查，不是冷SSD性能验收。

工具首轮141项中10项失败：新增NVML依赖未同步到矩阵审计工具清单。
修复后62项受影响检查全部通过，余下原通过复用；27项GPU guard host
检查通过。原始失败记录保留。实际quality-v2 lease查询已观察到目标
进程显存54624477184字节，该驱动读数不等于完整去重物理RAM。

## 已知边界

完成未知的payload保留可能增加错误进程的内存占用，不能继续复用；
应重载/重启服务。未实现可取消SSD I/O、跨流预取、MTP draft卸载。
旧offload runner仍须由外层完整阶段gpu_exclusive持有lease；内部空闲
预检不替代互斥。显存观测可用不闭合物理RAM/cgroup/cache归属未知项。
默认全驻留路径和模型计算kernel未改，未提出性能优化结论。

本机证据：.q4t-work/offload-foundation-20261020。

最终五阶段549文件封存复核通过，无ENV_ABORT。普通/MTP各3条加两版
各3条offload，共实际12条HTTP全部通过；最终范围复用6条全驻留，
采用当前候选3条offload。零编译警告，无性能优化主张。见
[审计](evidence/offload-foundation-20261020/acceptance-audit.json)、
[有界HTTP原值](evidence/offload-foundation-20261020/bounded-http.json)。
