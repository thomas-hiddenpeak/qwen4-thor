# 视觉 attention 行最大值：首轮达标，完整验收因环境中止

2026-11-07。[判定卡](VISION_ROWMAX_PLAN_2026-11-07.md)、
[完整阶段范围](VISION_ROWMAX_ACCEPTANCE_2026-11-07.md)保持原样。
**当前决定：HOLD_ENV_ABORT_INCOMPLETE_ACCEPTANCE。** 实现保留在
`codex/vision-attention-rowmax`，未合入运行时、未安装；默认仍为
ea1b474d/sidecar 3eb46385。先导收益成立不等于完整优化接受。

## 改动与已完成部分

c6eef3c2候选34e7ceef只改变vision.cu的AttentionKernel行最大值：每线程
局部最大值加256线程归约，复用原sum的1KB共享scratch，保留读取/复用
屏障。原-1e30f初值、fmaxf、QK、exp、sum树、归一化、V累加与BF16输出
均保持。父程序982bba07与当前主线运行时相同；计时开关两侧均关闭。

第一项测试为真实tools/evalscope HTTP，ABBA四启动、64Chat、8控制，
515.889秒租约COMPLETED；所有同题message/stop/usage一致。固定热请求：

| 场景 | AB块HTTP耗时降低 | BA块HTTP耗时降低 |
|---|---:|---:|
| 小图地球 | 1.332% | 1.166% |
| 大图木屋 | 3.098% | 3.235% |
| 多图 | 3.510% | 3.589% |
| 8帧视频 | 1.124% | 0.928% |
| 纯文本保护 | -0.447% | -0.649% |

大图、多图各自两块均达到事前3%投入门。文本慢3.640/5.304ms，原值保留。
结构化SSE在校验完成后才发送，原decode估计只覆盖0.059～1.453ms发送
尾部，不能解释为模型decode。两个启动/身份不足以建立可信区间或统计
非劣；没有通用损失容差。完整原值见[先导](evidence/vision-rowmax-20261107/pilot.json)。

随后直接CUDA的26组、19,312,128个BF16输出全部逐位相同，覆盖S=1到8192、
warp/block边界、多个时间组以及零/极端/非有限输入；非有限用例只保持旧
行为，不表示这些输入任务质量合格。两实现均48寄存器、1024B static
shared、0 local，未把数值用例耗时当作性能证据。独立HF small/rectangular
各image/video四组保持原max_abs/rel_L2<0.05，且与已验四份BF16逐字节相同。

必要构建零警告；283项required host通过，未变的其他host合同沿用原证据。
117来源锚点/38字段/10路由及3项mutation检查通过。最初误用--self-test的
原错误日志保留，正确参数--self-check另有成功记录，不改写首命令。

## 普通模式原始观察与未完成项

完整阶段在521b6431来源运行，普通decode两侧各五档×3请求及11质量题
完成；26组成对输出、token与终态一致，22质量答案精确一致。以下只是
**ENV_ABORT阶段内已收到的观察**，不能将整阶段改成通过：

| 名义长度 | TTFT父→候选(s) | decode父→候选(tok/s) | HTTP父→候选(s) |
|---|---:|---:|---:|
| 1024 | 0.8111→0.7936 | 18.5371→18.5898 | 14.5673→14.5108 |
| 4096 | 2.5593→2.5556 | 17.8412→17.8462 | 16.8521→16.8444 |
| 8192 | 4.9536→4.9600 | 18.0559→18.0515 | 19.0765→19.0862 |
| 45056 | 29.6668→29.6898 | 17.8494→17.8202 | 43.9529→43.9994 |
| 204800 | 155.7343→156.3927 | 16.9949→17.0183 | 170.7388→171.3766 |

8192/45K的TTFT和decode负点，以及200K的TTFT+0.6584秒、HTTP+0.6378秒
全部保留；视觉收益不抵消这些保护项。此处使用普通未缓冲流，decode为
总(输出token-1)/总(HTTP-TTFT)，并非逐token ITL。无跨启动统计证明。

默认MTP候选仅完成1K/4K六条；8K首条取消、另外两条未发出；MTP父版本
未启动。未齐的五档/质量和安装健康仍是未完成项，不能称“统计不足但
已验收”。原26组数值、四HF和普通模式记录保留供后续按来源审查复用；
不以此声称本次完整租约有效。

## 中止、计数和资源现场

06:19:07左右，NVML worker 4117333只记录到nvmlInit begin；5秒超时后
kill加1秒回收仍未完成，查询返回CLEANUP_FAILED。守护器以ENV_ABORT
中断驱动，原exit/result/failure和缺失的MTP custody/reaped回执均保留。
06:19:13内核同时记录NVIDIA驱动NV_ERR_NO_MEMORY；这是并发证据，尚未
证明根因，不归咎于新视觉内核，也不把监控错误转换成空GPU或跳过检查。

完整阶段实际2284.222秒、3模型启动、3原生worker、4控制；Chat台账预留
61、服务器实际收到59，数据库58完整记录，1取消、2预留未发出。已完成的
5个原生/普通模型生命周期有完整退出回执；MTP模型waitable rc=0、正常
shutdown但guard失败使custody缺失，不能事后伪造成功回执。

独立只读复核：六自有模型/原生PID、驱动/守护器与卡住的查询worker均已
不存在，18146可绑定，NVML后来返回OK且计算PID为空。原锁inode3294067
已由另一任务PID4103260接管，未干预；只证明本任务释放，不宣称此刻
GPU租约仍可用。默认程序与sidecar SHA均保持原值。

独立审计核对2316个Git来源，并封存3881个路径/3548个CAS对象。
[失败审计](evidence/vision-rowmax-20261107/acceptance-abort.json)、
[原件绑定](evidence/vision-rowmax-20261107/acceptance-bindings.json)、
[退出观察](evidence/vision-rowmax-20261107/abort-postflight.json)、
[保持默认的决定](evidence/vision-rowmax-20261107/decision.json)可追溯。
正常接受审计明确拒绝此FAILED阶段；archive-abort仅保存事实，不放行。

### 既有采样的有界离线分析

[两假设计划](evidence/vision-rowmax-20261107/abort-memory-plan.json)限定读取
现有2243份内存采样、2084次NVML查询和原内核日志，新增模型/HTTP/GPU
采样均0。[结果](evidence/vision-rowmax-20261107/abort-memory-analysis.json)
与[脚本/原件绑定](evidence/vision-rowmax-20261107/abort-memory-bindings.json)
保留；分析脚本和副本位于原阶段abort-diagnosis-01目录。

失败nvmlInit前0.536秒的采样：MemAvailable 22.624GiB、MemFree 11.249GiB，
CmaFree 3152KiB（约3.078MiB），CmaTotal 512MiB。普通模式两个身份的
MemAvailable最低约31.14GiB，MTP阶段最低约19.91GiB，CmaFree和SwapFree
都曾降至0。但已有52次成功init的最近CmaFree采样小于或等于3152KiB。
MTP成功init中位4.788ms、最大93.635ms，失败调用未返回，不能纳入成功
调用耗时均值。

这不支持“总可用DRAM已经耗尽”的简单解释；特定驱动分配/CMA压力仍是
待查方向，现有记录不足以确定根因。MemAvailable估计、1秒采样和CmaFree
总量不证明瞬时分配可行性，也没有失败分配大小/顺序或调用栈。不能据
候选一次失败反推CMA准入阈值、延长超时、降低容量或跳过NVML；已到本次
离线分析停止条件。后续须在可用窗口取得能区分机制的驱动层证据，再
决定修复和受影响重验，不能把重试成功当作根因已经解决。

## 已准备的交付工具与恢复条件

rowmax_delivery.py已实现精确pair回退、真实JSON/SSE/资料工具健康门和
外层guard完成后才清除恢复标记。5项真实临时文件事务检查通过，包括
第二文件替换失败、未知身份拒绝及guard失败保留标记；**未创建发布包、
未运行部署健康、未安装**。工程决定必须显式PASS才可prepare，本次HOLD
不能通过该门。工具保留为待验准备，不当作生产交付证据。

后续先定位并处理具体NVML/驱动内存环境问题，取得可用独占窗口，再冻结
只覆盖不可用项的新有界阶段；不能只因结果未齐就重复同类采样，也不能
把这轮ENV_ABORT改写为通过。补齐默认MTP比较、独立完整决定后，才可进入
原最多1启动4Chat的部署健康。此刻不合入未验运行时，不自动重启GPU。
旧托管/任务/Key/day包保持固定；全局media_acceptance仍false，不扩
nvJPEG、硬解码、视觉并发或长稳。整体优化交付仍未完成。
