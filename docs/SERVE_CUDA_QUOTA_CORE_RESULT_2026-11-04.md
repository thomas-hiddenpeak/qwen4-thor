# SRV-02j1 CUDA申请额度核心限定结果

**额度核心限定通过，尚未接入默认serve。** 固定表/原子预占、混合
CUDA申请额度、异步完成边界与未知所有权锁存已实现，并通过CPU与
真实Thor短探针。它限制申请字节，不是整机物理RAM硬限额。

实现冻结`b224c567`，保管修正冻结`910f3ed1`；native探针SHA256
`710f061ed7f131146cbec10d0dce631ebf88dfd69009873b11b2df71c55774a3`
保持。见[原合同](SERVE_CUDA_QUOTA_CORE_2026-11-04.md)、
[保管修正合同](SERVE_CUDA_QUOTA_CORRECTION_2026-11-04.md)及
[证据清单](evidence/serve-cuda-quota-core-20261104/manifest.json)。

## 已实现的合同

`allocation_quota.h`提供独立ledger和backend适配器。申请前同时
预占字节与表项，拒绝不进入后端；device、cudaHostAlloc、
cudaMallocHost、managed与async device合计。16384项表在本机
固定917600字节，活动路径未触发C++ new。CPU不链接CUDA；这不
宣称拦截了后端/标准库的一切malloc或驱动内部资源。

后端失败且未返回指针才退还预占；未知指针/释放失败保持额度并
拒绝后续申请。票据绑定账本及generation；跨流与旧票据不能错退。
async free进入pending，同步前捕获的发布边界和成功同步共同决定
可退额项，后发提交不提前退额；stream代次须由后续接线层管理。

已修正CUDA释放先完成、host尚未发布结果时的合法地址复用误判。
releasing/pending旧代次与新live分别计额；旧释放结果未知时，同
地址全部代次锁存unknown，不能重复释放。启动预留物理申请计入
额度，交付指针不重复记账，超额时只回收已成功申请的块。

源代码词法清单找到92处cudaMalloc、11处cudaMallocAsync、4处
cudaHostAlloc、2处cudaMallocHost匹配；它包括源码文本而不是完整
AST/运行覆盖证明。现有link wrapper仍不覆盖外部DSO内部申请；
这份清单为02j2接线提供检查入口，不能直接认定全部内存已受约束。

## 实际验证与保留失败

| 项目 | 实际执行 | 接受范围 |
|---|---|---|
| 初轮CPU | 25通过、4驱动通过，零警告 | 初版合同；尚缺释放发布并发时序 |
| 发布时序定向复现 | 1失败 | 原误判真实复现，原件保留 |
| 修正CPU | 26通过、4驱动通过，独立零警告构建 | 含成功/失败发布、并发预占、跨流、票据、溢出和startup owner |
| 保管修正CPU | 6通过 | 实际WNOWAIT/回收、只终止自有超时child、新旧观察与身份拒绝 |
| 原GPU轮 | 4本地exit0，15完整buffer/112MiB/14检查点；guard ENV_ABORT | 不记环境有效的GPU通过 |
| 定向GPU复验 | 相同4探针exit0，15完整buffer/112MiB/14检查点；guard COMPLETED | 额度核心及短进程保管限定通过 |

原生CPU实际52次：25通过＋1失败＋26通过；驱动4＋4与保管6另列，
不把重复运行算成新增覆盖。GPU累计8次探针，只有后4次记接受。
全程零模型启动、零HTTP、零小时/天级验证，无性能接受结论。

接受的四个探针使用CUDA runtime13030/driver13020、integrated
SM11.0；实际行为如下：

- 混合申请32MiB满额，额外1MiB在CUDA调用前拒绝；全部释放后可
  再申请32MiB，完整写读正确，最后零额度/零表项。
- 两个流各8MiB；async free后仍计额，错误流同步不退额，正确流
  完成才退还。实测发生地址复用，设备同步最终归零。
- 20MiB启动计划在16MiB额度下第三笔拒绝，已申请两块全部回滚；
  16MiB计划完整交付/读写/释放，交付未二次申请或计额。
- 8线程各申请2MiB，8MiB额度下恰好4成功/4拒绝，只有4次后端
  申请；完整buffer检查与释放后归零。未制造物理OOM。

## GPU守护器与短进程保管

原轮GPU租约1.314618秒、3观察，driver本地1.143321秒。guard
看到PID2503164的CUDA上下文时该短进程已被wait回收且从未进入
guard history；其PID/父/start_ticks与本轮concurrent原始启动
记录匹配。原ENV_ABORT及driver -2终态保持，不事后改为通过。

修正保留所有子进程的zombie身份，等一个查询开始晚于最后子进程
注册、并随后完成的无冲突guard扫描，再逐个回收。既不降低未知
PID判定，也不把固定睡眠视为已观察；原生产guard没有修改。
复验driver1.139997秒、GPU租约2.379515秒/4观察，4个身份的
保留、观察时序和回收记录完整，COMPLETED/exit0。轮询仍不能
排除短于采样间隔或不可见的干扰。

## 收尾与下一阶段

02:13:00 CST后检GPU计算上下文空、8个native子进程均退出、容器
为空；GPU锁device66305/inode3294067保持且可取得，SOAR持久
mask仍指向/dev/null，inactive/dead。MemAvailable116.896GiB
只是末次全局快照，不是物理释放逐字节证明。

默认ELF`d66f2bb9`/sidecar`42d7672a`、211运行时来源和用户day
来源保持；day未执行。固定基线pair/day-plan副本另存本阶段baseline/
以便以后默认更新，不修改原交接计划。169份原件无损封存为3份
Git文件165420字节、17份本机二进制/依赖身份保留；两种失败均纳入。
只读离线核验已通过，不重启GPU或重复推理。

下一阶段02j2接入生产wrapper和startup owner，明确所有分配API/
flags、零大小语义、stream代次、async/sync边界、CLI/config/metrics
与错误传播；冻结真实serve/工具闭环后再决定发布和长稳交接。
普通host堆、文件缓存、驱动/库内部、CUDA池保留与外部进程仍不在
本额度账本内，物理RAM完整保护及工业化持续目标保持未完成。
