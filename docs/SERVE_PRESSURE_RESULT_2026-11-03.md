# Serve 同进程压力恢复与64轮混合业务结果

**限定接受，长期工业化目标保持active。** 冻结4fd5ec7c，复用实际安装
`build/q4t` 84d16983 / sidecar c5203331、protocol aeb66adc。198份运行时
源码与模型/模板/grammar未变，原零警告构建、CPU/数值证据复用；本轮
没有替换默认pair。合同见[验收范围](SERVE_PRESSURE_2026-11-03.md)，
原始记录见[证据目录](evidence/serve-pressure-20261103/README.md)及
[机器摘要](evidence/serve-pressure-20261103/acceptance-summary.json)。

## 执行与结果

实际326生成尝试、4负例、1模型启动、65预检，全部首轮符合预期：
324成功、1主动取消、1排队内存拒绝。真实只读工具执行65次（恢复组
1次、混合组64次），参数/调用ID、真实文件结果及引用回填逐项通过。
无自动重试；330/6/2/1800秒硬限未扩大。业务645.972秒、首次lease到
finalize656.418秒；guard COMPLETED、child0、ENV_ABORT0。属于业务
可靠性验证，不作吞吐优化或性能非劣接受声明。

同一PID1994177/start ticks186011818、独立scope、完整head、普通MTP k3、
严格plain；单槽8192/prefill2048、inflight8/queued2、loopback18106。
测试显式floor30GiB、恢复30.5GiB；默认2048/512MiB配置未改，不能将
这一测试等同于在默认门限附近耗尽整机内存。

| 场景 | 实际证据 |
|---|---|
| 真实压力关闭 | 匿名CPU触页最高4992MiB（4.875GiB），真实读数降至floor以下；3个只送头的chat/chat/preflight均503 memory_pressure |
| 排队回收 | 实际strict queued=1/worker=1后受压，原SSE一个错误和一个DONE；queue/worker/严格租约全部回收 |
| 活跃请求与控制 | 关闭后观察到普通SSE content由28增至32字节；ready503、live/health200，未知取消404，正确凭据取消202后slot归还 |
| 滞回与同进程恢复 | 部分释放后实际指标32389369856字节（约30.164GiB），gate仍关闭且ready503；全部释放后同一PID ready200，普通/strict/真实工具成功 |
| 持续混合业务 | 64轮，每轮并发普通JSON/SSE+一个strict，随后真实资料工具读取/答案回填；strict JSON/SSE交替，320成功生成、64预检/工具执行 |
| 资源回收 | 最终worker196/196 reap，owner全部归零、队列0、slot1/1free；4条内存拒绝日志与固定指标一致（headers3、execute1） |
| 退出 | 排空正常、returncode0、独立scope删除、所有客户端结束；未干预其他服务、未drop_caches |

329份生成/拒绝wire、1361封套通过schema；3个body前拒绝无法区分请求
体的JSON/SSE模式，不据其命名增加覆盖。控制和65份预检另行校验。
错误流完整保留DONE后再交严格客户端判错，没有把部分答案记为成功。
混合组第1到第64轮静止端点跨度588.547秒，单请求实际0.346–4.179秒
（含排队/传输/客户端处理，不是纯decode性能）。没有小时/天级长稳结论。

## 内存与核算边界

每秒连续观测638次，跨度643.999秒，最大采样间隔1.034秒；另有压力
分步和每轮静止端点。全程PID/start ticks及scope身份匹配。压力助手
实际读数最低MemAvailable29.385GiB、MemFree18.302GiB，高于它的20/
12GiB安全余量；所有临时匿名内存已释放，助手returncode0。

前8轮为冻结的预热基准，后56轮资源包络通过：parent RSS最高增加
4.5MiB、scope anon增加4.5MiB（上限分别64/128MiB）；FD一直49、
threads一直5（允许各+2）。这一有限运行不证明长期绝无泄漏。

| 独立观测视图 | 本轮值 | 限制 |
|---|---:|---|
| parent RSS采样最大值 | 3.813GiB | 包含启动阶段，非静止服务RSS，也不包含全部CUDA物理内存 |
| direct child RSS采样最大值 | 224.059MiB | 558个有值样本；2个退出中的子进程缺RSS，保留未知而非0；采样不保证已exec |
| worker日志post-exec最大RSS | 224.059MiB | 196条退出记录；wait4 lifetime最大3903.535MiB另列，包含继承/exec前历史 |
| scope memory.current采样最大 / kernel peak | 15.367 / 15.938GiB | 包含scope helper及归属该组的缓存，不能代表全部物理RAM |
| NVML模型进程内存采样最大 | 80.610GiB | 与RSS/memcg存在重叠/遗漏，不能相加或用独立峰值相减估总量 |
| 连续采样MemAvailable最低 / MemFree最低 | 31.649 / 8.165GiB | 含模型启动，非压力助手全程安全余量；短压力低点由分步记录捕获，1Hz未捕获该最低点 |

scope memory.events的oom/oom_kill/oom_group_kill均0；MemoryMax=infinity，
这里只证明归属与计量。此前[CUDA探针](OFFLOAD_RAM_PROTOCOL_2026-10-02.md)
发现device漏计，本轮也不把cgroup作为整机硬限额。没有触发实际OOM，
没有给启动加载提供内存准入，采样不能排除间隔内更高峰值或短暂干扰。

## 交付与保留

CPU19项及OOM计数未知拒绝1项通过，含真实128MiB触页/EOF释放、父退出
释放、scope祖先关系/所有权/退出计数。原始日志已完整落盘，未重复
运行身份不变的旧运行时矩阵。2698份原记录逐字节封存为2702文件、
19419823字节；1个本机ELF登记身份。package/execution/root seal、源
文件表和工具/SDK身份可核验，不靠提交本身证明验收。

[入库核对](evidence/serve-pressure-custody-20261103.json)确认2702份Git blob
与磁盘逐字节一致、2698原记录和1本机ELF身份一致，回退pair只读核验。
全库1755个本地文档链接扫描发现14个已有失效链接（历史入口快照与
3份2026-10-08 MTP报告），相关文档与冻结前字节一致，本阶段未修改。
原全库检查失败保留，本阶段证据链接有效；不声称全库文档已完整修复。

09:52:06 CST复核GPU计算PID为空、共享锁可用、owned模型/scope不存在、
无恢复标记，MemAvailable116.994GiB。当前默认84d16983/c5203331和旧
ed672a65/67897c81及完整回退链保留；见[原发布与回退入口](SERVE_MEMORY_ADMISSION_RESULT_2026-11-03.md)。
本轮直接使用安装路径完成全部真实HTTP，无额外安装/重复模型健康测试。

后续继续运营告警/排障运行手册、较长持续运行与重启/发布回退合同。
整机物理RAM硬保护、实际网关/鉴权和多人资源策略、跨进程业务恢复
仍未完成；局部通过不能关闭工业化持续目标。
