# SRV-03b结果：严格长响应背压尚未证明

2026-11-03，**NOT_ACCEPTED_STRICT_BACKPRESSURE_UNOBSERVED**。
本轮完成同源专用构建、固定预算驱动及一次真实尝试，但长严格输出
没有进入发送正文的阶段，不能关闭A组保留项。默认5642eb68 /
sidecar6aca6a3e保持，未安装故障程序，没有性能接受或工业化完成声明。

## 身份与执行结果

冻结驱动0ba39744，构建起点507fba63。专用ELF为
`903c4f77b19e9a1d0802d22e6a64267afa2a357cc55cf20172628507962450d3`。
189个运行时文件与当前默认的SRV-07接受身份一致，生成协议文件相同；
Release、编译器和Q4T配置保持，仅既有故障宏启用，构建零警告。
协议SHA仍为`a6468bf23a7bfb9e870c047cda6d68565e968e13ac4e551fea983f9476f5f747`。

| 项目 | 本轮事实 |
|---|---|
| 输入 | 48×256个等号的有限数组schema，429字节schema、12443字节预期内容；CPU JSON/SSE合同与生产探针接受 |
| 规划分词 | tokenizers0.23.2固定编码243tokens；这是编码估计，不是受约束生成路径保证 |
| 真实启动 | loopback18100、单槽8192/prefill2048、完整head/默认MTP k3；strict仍走plain约束路径 |
| 真实请求 | 第一组SSE长响应，max_tokens1024、request_timeout_ms180000；未执行JSON组或恢复组 |
| 服务日志 | 55314.963ms以`structured_output_incomplete`结束，slot released=1、worker已reap；没有EAGAIN握手文件 |
| 驱动结果 | 等到原170秒观察窗口后失败；执行206.739秒，累计1生成/0专门拒绝/1模型启动 |
| 预算 | 原6/8/2/1800秒不扩大，剩余额度未使用；246.68秒写入未通过收尾记录，没有finalize接受记录 |
| 环境 | guard COMMAND_FAILED、child=1，未出现ENV_ABORT；模型正常退出、0 in-flight、启动分配1606全部释放 |

`stage=response`表示进入响应处理，不证明内容验证成功。此例随后为
严格输出不完整错误，不能用该日志或55秒耗时代替真实写阻塞证据。
当前记录不足以区分模型选择了较短token、空白/转义路径或其他合法
前缀；没有保留原始生成token，不能对具体原因作进一步断言。

## 取证缺口及已修复的开发工具

第一驱动仅在观察到EAGAIN后读取慢连接。此例提前生成失败，驱动在
超时清理时关闭连接，**没有保存错误响应wire**；只有服务日志能定位
错误，不能声称真实客户端已经核验该错误封套。原记录不补造、不覆盖。

后续驱动增加同一request ID的终态日志识别；handler已经结束后才读取
并持久化原始错误wire，遇到读取超时也保存前缀，并明确标为失败证据。
匹配ID、结束阶段、完整错误/截断前缀及不覆盖记录3项CPU测试通过。
该修复未重新加载模型，不能追认本轮网络取证完整。

内存采样发现Thor内核没有`/proc/<pid>/task/<tid>/children`，旧采样
静默漏掉worker。现改为`/proc/*/stat`的PPid发现和start ticks核对，
夹具及本机实际闲置CPU子进程两项通过且已reap；没有追加GPU验证。
首次8项驱动CPU门中7通过，另1项误将CMake导出的测试target也要求
故障宏；限定实际q4t_server后该项定向通过，原失败和初版脚本保留。

## RAM记录及边界

170次原始采样的系统MemAvailable最低33.140GiB，parent采样RSS最高
932.730MiB。**worker采样缺失，不是零**。服务结束日志另报告：

- worker post-exec VmHWM：234909696字节，约224.027MiB；
- wait4 lifetime高水位：4093374464字节，包含exec前继承峰值；
- worker reap成功，wait_status=9是现有所有者kill/reap清理行为。

这些数值不相加为独占物理RAM，不把RSS解释成全部CUDA统一内存，也
不把global MemAvailable的变化归因于单个请求。没有形成严格背压时
的并发RAM验收；worker地址空间限制与整机物理RAM边界仍是不同合同。

06:25:12 CST postflight确认GPU计算PID空、共享锁可用、无模型进程/
恢复标记，MemAvailable116.976GiB。没有drop_caches或干预其他服务。

## 证据及后续

[原始归档](evidence/serve-backpressure-20261103/README.md)按字节复制78
份记录，共82文件/1744489字节；含第一次enum夹具拒绝、构建、CPU首次
失败及定向修复、真实请求/服务日志/guard、采样和后续工具修复结果。
2份ELF/source.bundle仅登记本机身份，封包及执行seal保持可核验。
本地根目录`.q4t-work/serve-backpressure-20261103`不得删除；旧默认及
SRV-07/08回退/接受链保持。后续工具修复以当前源码和postmortem CPU
身份记录为准，不冒充冻结0ba39744已经包含修复。

严格SSE取消、JSON写截止和排队恢复组合仍未验证。先解决可可靠生成
的长载荷前提再单独冻结后续验证，不以增加token/反复启动寻找通过。
当前转入独立的[SRV-02b准入与容量适配排队开发合同](SERVE_ADMISSION_2026-11-03.md)：
明确可配置上限、容量适配FIFO、取消注销、配置发现与固定指标；先
开发和CPU合同，再冻结该任务自己的实际HTTP清单。它不替代03b证明。
长期“持续推进完善serve到工业水准”目标保持active。
