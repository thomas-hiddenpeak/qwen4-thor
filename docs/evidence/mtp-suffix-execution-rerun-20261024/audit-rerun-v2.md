# 执行观测重跑独立审计

**完整阶段未通过；普通/CPU 已完成前缀可独立分析，profile 为 ENV_ABORT / INCOMPLETE。**

v2 保留 v1 输出及脚本；仅修复 proof scope 文本适配、请求 event 与 exec-capture event 区分，以及本段条件化措辞。

证据文件完整性：PASS，实际错误 0；资源监控环境错误 0。
阶段 STOPPED / 1880.104795 秒，guard COMMAND_FAILED/1 已释放。10/13 启动，37 普通 + 6 CPU 请求完整闭合；另1个 profile 响应及目标/launcher退出成功，但trace被拒绝。共44/49完整响应，不能把它写成完整阶段43或49请求通过。

- 1317 冻结文件、585 raw文件及443源快照文件核验；f0ff/553运行时、f356/sidecar/子集身份保持。
- 所有44响应逐条核对 SSE、输入数、256输出、length、完整hash及服务器请求identity；10个服务退出与释放记录闭合。
- 1880/1880资源样本写出，0未flush，无监控/清理错误。最终实际样本在最后目标/launcher退出后；成功路径的final_monitor_barrier={}未执行，不伪称通过。
- 进程观测 {'OK': 7350, 'EXITED_DURING_IO_READ': 3}；全部缺io恰3条同身份Z态，原EACCES/raw复查保留，无补零。新pidfd等待次数0，新50ms等待分支本GPU阶段未触发。
- 只读/proc核对50个历史身份，活跃同身份0，历史进程组残留0。
- Nsight保留6个warning，实际q4t有CUDA/NVTX各1条“不一定全部采集”；不能以wrapper无CUDA/NVTX、HTTP输出正确、退出0或表非空放行。精确UM unsupported单列为辅助特性缺失。
- 正常37请求可作开发材料描述性收益比较；CPU6请求可作有插桩开销的局部等待/提交分析。共享B与单启动pair不能证明总体非劣；本次不更新完整优化验收/交付结论。
- 上轮ENV_ABORT不改、不合并样本。旧合同按原数学/状态范围复用；39项CPU修复证明不冒充本阶段新等待分支真实触发或整模型质量。

JSON SHA `fbf7f43cadcb645d73abb403117c1ae9a3c028fc7cb0e59f3ccee0ea5100fb03`；脚本 SHA `32dac6cf693a00222a5bd7d0a872361837bb4b8e8e381de52a57288ce9cb5337`。
本审计没有执行任何GPU、HTTP、NVML、传感器查询、测试或信号。
