# SRV-02g：内部清理与MTP启动限定交付

2026-11-03，已接受并交付本批可靠性修复。长期目标
“持续推进完善serve到工业水准”保持active。合同见
[原冻结](SERVE_BACKEND_CLEANUP_2026-11-03.md)及
[唯一探针纠正](SERVE_BACKEND_CLEANUP_CORRECTION_2026-11-03.md)。

## 交付身份与行为

| 项 | 当前证据 |
|---|---|
| 默认ELF | `3fd31a217ee145c9a56659bacd2ca015041d02bd1bc0603cf6f5845cb362d1c3` |
| release sidecar | `3ba0b8d3ba421851bd423150928e299720bb8ba749c03885378858473670e39f` |
| protocol | `044ef2f3758a7ceac74870ac9669b4e3d1708de0f12d5b5dedafad55dd9ef815` |
| 运行时冻结 | `601fa1b3`，206个运行时/构建来源；纠正工具提交`048b9f06` |
| 验证服务ELF | `3d6cdf755324fe5a9511f96a24bd81da8a57668bb7d62faabc2e84f35ee8d20c` |
| 修正probe ELF | `e2858c05190e3af8434825189f1e6d001c75ed07f15e1bb6f3c671fa1d227b9f` |
| 直接回退pair | `40cf6376… / 79d4a657…`，完整匹配包保留 |

- 请求MTP时，legacy与planned加载失败都拒绝启动，不再默默退回plain。
  显式`--no-mtp`仍支持；没有把未分类加载错误当成安全降级理由。
- 默认q4t直接调用的八类CUDA释放/完成API检查返回值；无人安全处理的
  错误记录`[q4t][cleanup-fatal]`、API/phase/error并`_Exit(1)`，不重试
  未知释放、不继续析构。Model最终加载排空先于源释放；MTP失败守卫
  先排空再释放loader。没有新增GPU同步或改模型算术/精度/分派。
- serve已有明确责任方的cudaFree错误仍返回原始状态，关闭健康、排空
  停服并走原错误协议；这个例外不能吞掉同步失败。
- capabilities新增作用域与边界，inventory仍38输入字段/10路由，新增
  runtime/model/build审查锚点。成功async free/destroy不当作排空证明。

## 验收与原失败

生产/验证构建零警告。CPU host OFF245/ON254、发布必需211、后端9、
驱动6、配置15+10通过；这些集合重叠，不合计成独立测试总数。
原生HTTP25通过。真实MTP生命周期验证缺tensor/分配失败、scratch23个
分配边界、checkpoint3边界及真实权重加载成功/第8与第39分配失败。
该进程实际MTP权重加载3次，另有微小夹具；不是零模型工作。

原17个CUDA探针中16个通过，最后一项错误要求未完成计划的Finalize
整体成功。实际4096字节已释放，allocated1/freed1/live0/unknown0，
接口正确报告`plan_incomplete`。原FAILED/COMMAND_FAILED完整保留。
仅纠正该断言，独立零警告构建/4项CPU检查后重验；原生HTTP、MTP及
其余16项证据复用，没有改生产/验证服务ELF。

因此累计18探针尝试，原17次数计划没有追认为通过。17个逻辑场景的
最终证据齐备：八API错误、正常传递、生产忽略注入环境、handled free
返回、MTP scratch/失败drain、未移交预留释放及三个正常组件路径。
注入是验证ELF返回模拟CUDA状态，不是物理OOM、硬件损坏或驱动恢复证明。

全量server实际7启动、10生成尝试、6负场景，均在补验中首次执行：

| 场景 | 结果 |
|---|---|
| 生产普通JSON/SSE、strict JSON、JS真实资料工具与回填 | 5成功；读取真实`docs/MODEL.md`并引用；3个grammar worker全部回收 |
| `--startup-plan off`请求MTP加载失败 | 无ready，退出1，未退回plain |
| MTP device free、model device/freeHost/eventDestroy | 各1正常回答后触发清理错误，各退出1 |
| 服务缓冲底层cudaFree返回错误 | 完整SSE `gpu_unhealthy`与唯一DONE，随后退出1，未被直接退出边界抢走响应 |

共9个成功回答与1个错误响应，10份完整wire/25封套通过；另有启动拒绝
和四个shutdown故障，因此6负场景不等于6个HTTP错误。52次业务控制
观察、213条连续样本保留；正常静止点owner归零、slot1/1 free。启动
拒绝不跑持续采样，故意退出前先停止活PID采样；最终pidfd/cgroup记录
另列，不声称采样覆盖退出瞬间、租约间隔或全部资源峰值。

本批普通JSON约0.448秒、SSE0.346秒、strict JSON0.623秒；真实工具与
回答约3.876/4.170秒。业务250.507秒，原阶段开始至finalize641.336秒，
保留原1800秒墙钟及1500秒安装边界。只是有界业务观察，不是性能非劣
或全模型数值等价证明；未变计算证据按来源复用。

## 安装、回退与证据

补验guard COMPLETED、ENV_ABORT0。安装CLI与独立grammar worker握手通过，
HTTP复用相同SHA候选，无额外安装模型启动。原阶段COMMAND_FAILED保持。
只读核验新/旧回退bundle及41个完整pair的历史链，未实际切换回退；
最早明确stale sidecar边界仍不可安装，不用旧sidecar伪造匹配关系。

16:04:35 CST，GPU计算PID为空、共享锁可取得、7个owned进程均退出，
无恢复标记，MemAvailable115.787GiB。这不是物理RAM硬限额或回收时延证明。

[证据目录](evidence/serve-backend-cleanup-20261103/README.md)无损封存583份
原记录为20份Git文件，合计1,009,212字节；16个大产物保留本机身份。
原失败、所有wire、CPU/native日志与采样均保留，不宣称原日志全档脱敏。
[保管核对](evidence/serve-backend-cleanup-custody-20261103.json)核验压缩成员、
原件SHA、Git暂存blob、产物与发布pair；没有为归档追加模型请求。

## 未完成边界

本批只是默认q4t的直接CUDA清理/完成调用；不覆盖外部共享库内部、
research reservation profile或其他独立程序的全部调用，不等于所有
分配/拷贝/kernel/CUBLAS错误均已分类。失败立即退出可截断活跃HTTP，
不能保证错误封套，也不能据此重放未知结果的工具。

下一批SRV-02h先审计媒体及host内存异常/错误响应路径。完整媒体合同、
物理RAM硬保护、实际鉴权/TLS/通知/多人策略、新ELF小时与天级长稳、
通用任务恢复和14个历史文档链接缺口继续待办。
