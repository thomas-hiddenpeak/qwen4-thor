# 固定 k3 suffix 的交付与阶段收尾

2026-10-27，**已交付，ENGINEERING_ACCEPTED_BOUNDED_OPT_IN**。
当前 `build/q4t` 为 `90f24f49ea93e7ddbb359e817f05a9d39f87a54f66433c01a8d46f198f489bd4`，
发布元数据 `45cfe74a761f9b3a9ab1db613b42e8fde64192d869f17a0596ea5925b73e61ec`，运行时源码
`86f4cf2019768c6e4284f86f04b2a2e050723c17`。本次后续仅文档与证据收尾，
不将最终文档提交当成binary构建源码。完整head仍默认，suffix必须显式
`--mtp-suffix`；65536行BF16子集仍独立显式选择。没有常驻服务。

[验收及完整保护原值](MTP_SUFFIX_LAZY_ACCEPTANCE_2026-10-26.md)与
[决策账本](evidence/suffix-causal-20261026/decision-b.json)记录了接受范围。
完整head重复代码/文本配对GM收益14.100%/14.588%，子集6.756%/6.525%。
226生成HTTP、22/22质量、受影响CPU合同及复用独立状态证据完成；
没有声称所有档位统计非劣。子集1K/200K负吞吐点、8K正TTFT点及普通decode
五档−0.0020%至−0.3834%的点估计均保留，持续微小退化尚未排除。
按需分类确实删除无条件248320次启动Decode，不能称旧200K TTFT根因已修复。

## 实际交付健康与首次准备失败

先完成回退包和接受快照，才在完整GPU独占驱动下替换程序/sidecar。
首交付驱动的CPU预算校验误将三项 `(requests, starts, steps)` 与两项
`(3,2)` 比较，启动模型前抛出ValueError；0模型启动、0生成HTTP，程序
与元数据自动恢复到f356/422993b0。原COMMAND_FAILED、脚本、包封条和退出
记录保留在 `deployment/`、`delivery-package/`，未改写成功。

仅修校验预期为 `(3,2,2)`，直接验证实际冻结计划、一个正例和三个预算
负例后，执行原来尚未使用的健康预算。此为零加载准备错误修复，不重采
B或已经执行的健康请求；原900秒从首次准备开始计，不重置。新包和执行
目录为 `delivery-package-v2/`、`deployment-v2/`，同一推理SHA。

- 默认参数完整head（不加--mtp/--mtp-k、无subset、无suffix）：1请求通过。
- 显式subset＋suffix：2请求通过，各54次suffix命中；输出SHA、接受分布、
  三候选与普通尾部工作符合冻结参考，缓存/索引和槽位释放通过。
- 两服务退出0，2启动/3生成HTTP，实际阶段114.069秒；
  连首准备失败/CPU修复/准备等全部间隔287.752秒，未超原900秒。
- 115次监控全部flush、108次NVML查询均OK，退出pending清零，
  最终服务关闭后的flush屏障通过。外层COMPLETED，锁已释放，无自有残留进程。
- 2370项最终交付审计通过，已安装程序/sidecar/构建cache与封存身份一致。
  恢复标记已移除。健康请求不计入新的性能样本，不改变B统计结论。

整个A/B/健康实际合计 **289生成HTTP、19次服务启动**（60/6＋226/11＋3/2）；
首次零加载准备失败没有额外模型启动。03:53:16+08全任务截止未延长。
不再追加模型测试、参数扫描或Nsight。

## 使用和回退

```bash
# 完整head＋显式suffix；移除最后一项即回神经MTP
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 1 --max-prefill 8192 --max-len 208896 --mtp-suffix

# 独立子集＋显式suffix（本机保留产物，不随Git下载）
./build/q4t serve --host 127.0.0.1 --port 8000 \
  --max-seq 1 --max-prefill 8192 --max-len 208896 \
  --mtp-subset-head .q4t-work/suffix-causal-20261026/delivery-package-v2/artifact \
  --mtp-suffix
```

功能回退：移除 `--mtp-suffix` 后重启；完整head/subset选择不变。若需普通
decode，移除suffix及subset参数后用 `--no-mtp`。suffix不支持非3的k、
多流、media、容量大于262144或非greedy；相应启动/请求明确拒绝，非静默启用。

整版回退包：`.q4t-work/suffix-causal-20261026/delivery-package-v2/`。
`previous/` 保存f356二进制、422993b0元数据及原构建cache；`candidate/`保存
90f候选和实际构建cache；另有独立子集副本、tested-source、接受快照与封条。
包封条SHA `4702bfe327786b6ba64ae71af7e406321154a1eceb0f68c2455fa46f8d8d7114`。
逐个文件原子替换，不声称跨文件掉电事务；未知安装身份拒绝覆盖，保留恢复标记。
使用现有同身份入口回退时，GPU锁范围覆盖整个回退动作，fresh目录不能复用：

```bash
python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/suffix-causal-20261026/manual-rollback/lease -- \
  python3 -B /home/rm01/models/dev/qwen4-thor/.q4t-work/suffix-causal-20261026/delivery-package-v2/delivery_b.py \
  deploy --rollback-output /home/rm01/models/dev/qwen4-thor/.q4t-work/suffix-causal-20261026/manual-rollback
```

该脚本还依赖本工作区保留的 `tools/verify/startup_delivery/` 原子替换辅助代码；
本机原构建cache路径也继续保留。若搬移工作区须先核对包/工具及发布路径，
不能把绝对路径包冒称可任意目录直接运行。上条回退命令仅文档化，实际当前
保持新交付版；首次准备失败已实际验证恢复前程序和元数据。

## 证据、分支与边界

- B完整独立副本 `archive-b-complete/`：2284文件、575137844字节，封条
  `ae2b1c55cf5dccadd25229e29ea8af3ba715ff5b7d76fe2f450cdff7d921caae`。
  包含首失败、CPU修复、批准、补跑原始HTTP/监控、分析、决定及程序身份。
- A及原GPU状态/数字门/min32/区间材料按原身份保留，不重新授予旧失败资格。
  旧Nsight完整性未被此健康检查洗成通过，日志大小不单独决定材料是否可用。
- Git可审查摘要：[B结果](evidence/suffix-causal-20261026/b-summary.json)、
  [4078项验收审计](evidence/suffix-causal-20261026/audit-b.json)、
  [2370项交付审计](evidence/suffix-causal-20261026/audit-delivery-b.json)、
  [发布元数据](evidence/suffix-causal-20261026/release.json)。完整原件路径和SHA在摘要中。
- 继续使用 `codex/suffix-k3`，本轮只提交本任务文档/证据。该分支有main未包含的
  独有实现、研究和证据依赖，保留工作区及分支；不自动合入main或删除远端入口。
- 接受的是当前身份和固定k3单流文本范围；不保证任意输入输出逐位一致，不授予
  总体性能非劣或公网SLA。普通日志的小波动不自动续开本目标；实际合同错误、
  可归因回归或新身份才触发相应重新评估。
