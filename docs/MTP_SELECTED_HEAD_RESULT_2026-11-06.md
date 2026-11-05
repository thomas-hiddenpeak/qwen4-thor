# MTP扩展末行head：有界试验结果

2026-11-06。结论为 **NO_GO_FEASIBILITY_TARGET**：首轮HTTP证据有效，
两种head均未达到事前继续投入条件。本轮停止，不进入条件B阶段，
不将候选作为已验优化合入主线或替换默认程序。

## 实现与身份

按[冻结判定卡](MTP_SELECTED_HEAD_PLAN_2026-11-06.md)执行性能优化路线，
必要构建后，第一项测试是本机Thor真实evalscope HTTP E2E。
无预跑单测、kernel探针、profile、warmup、测试连接或重试。

- 基线main：`def8cc96e7f94b50fbc917303c8570ec23e841ea`。
- 候选实现：`e99925b32edd4e0d9ce5886c33a7542773743829`，研究分支
  `codex/mtp-selected-head-rows`；C++23/CUDA13.3、SM110a、xgrammar开启，
  构建零警告。
- 实测父程序：`c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636`。
- 实测候选：`ea88fc726badfd58e8900e49ead6a2b0f763b8a8810f7a4a7c17776780295234`。
- 默认sidecar：`0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3`，
  协议仍为`9c8758c4`。退出后再次核对默认程序及sidecar，均未改变。
- subset为原已验65536行BF16产物，`head.bf16`摘要
  `21522eabb4ab4f816d9f76eefe317a17e7ba668ebe838809eb4be87d84ef75c1`。

候选仅在`MtpSpeculativeStepMulti`验证后extend的原T_ext=2..8固定
归约路径，跳过不再消费的head输出行。完整forward及状态更新仍执行，
单活跃槽直接取末行，多槽复用既有scratch打包；较大批量保持原路径。
未引入量化研究代码、QSA融合或客户端变动。源码中的逐行归约依据
不能代替独立数值验收；本次也没有新增路径命中率或kernel归因证据。

## HTTP观察及工程决定

顺序为完整父→完整候选→subset候选→subset父，每臂一次模型启动。
配置max_seq1、max_prefill8192、max_len208896、MTP k3、startup-plan off，
suffix/cache关闭。每臂1K档三请求、各256输出，加11个固定质量题
覆盖1K/4K/8K/45K/200K。短性能输入实际为998 token；质量输入精确
按题目档位核对。总计4启动、56HTTP，无新增或剔除请求。

| head | 父aggregate decode (tok/s) | 候选aggregate decode (tok/s) | decode变化 | 三请求总E2E 父→候选 (s) | E2E变化 |
|---|---:|---:|---:|---:|---:|
| 完整BF16 | 35.513407 | 35.469625 | −0.1233% | 24.438667 → 24.508252 | +0.2847% |
| BF16 subset | 38.658057 | 38.697832 | +0.1029% | 22.621663 → 22.625798 | +0.0183% |

aggregate decode使用`sum(output_tokens-1)/sum(latency-ttft)`，E2E为
三个请求latency之和。首请求保留在汇总中，逐请求原值如下：

| 臂 | decode三请求 (tok/s) | TTFT三请求 (s) | E2E三请求 (s) |
|---|---|---|---|
| 完整父 | 35.518895 / 35.542371 / 35.479013 | 1.071092 / 0.913980 / 0.912435 | 8.250369 / 8.088515 / 8.099783 |
| 完整候选 | 35.369563 / 35.460090 / 35.579848 | 1.093668 / 0.922320 / 0.924515 | 8.303256 / 8.113503 / 8.091493 |
| subset候选 | 38.693488 / 38.671605 / 38.728445 | 1.077886 / 0.889173 / 0.890189 | 7.668143 / 7.483158 / 7.474497 |
| subset父 | 38.567974 / 38.708613 / 38.697901 | 1.055062 / 0.890422 / 0.887289 | 7.666766 / 7.478104 / 7.476793 |

56响应成功，44质量检查符合期望。每个head内14对响应的prompt、
文本摘要、输入/输出token计数和finish均一致，共28对；完整head与
subset之间不要求相同输出。每臂14次admitted/end完整配对并释放，
四个服务正常退出，无MTP加载失败或plain回退标记。

事前条件为**两种head分别至少+3% aggregate decode且E2E下降**，
本轮均未满足。因此工程决定是结束这次尝试，而非证明实现存在
统计性能退化，也不声称两版统计持平。单启动和短档观察不足以定位
瓶颈或外推所有上下文；不因观察差值很小放宽门槛或追加采样。

条件B阶段的五档性能、plain对照、多槽、缓存/取消、独立全logits与
状态合同未触发；独立数值合同、完整质量和性能接受均不作通过声明。
不启动部署健康检查、默认替换或长稳测试。

## 环境有效性与资源释放

阶段耗时1195.215秒，在原1800秒截止内；整个阶段通过原仓库共享
GPU锁运行，租约1196.659秒，外层`COMPLETED`、子进程返回0。
1097次运行中轮询加一次preflight，1098次NVML查询均成功；未观察到
竞争或不可读进程。轮询仍不能排除短于采样间隔的干扰。

公共监控取得1196个完整样本，53个已发现来源全部可读；3项PSI来源
在发现时不可用，明确保留未覆盖。最终服务退出后有新鲜完整采样，
线程join后的summary/state摘要匹配；pending0、unflushed0、failure0，
原件封存后未修改。此次不是监控失败导致的ENV_ABORT。

进程请求CUDA记账峰值：完整两臂均105745766808 bytes，subset两臂均
103086840216 bytes。四臂析构时pending/unknown及合同/backend错误
均为0，charged=live=200953856 bytes；这是进程结束前仍存活的分配，
不将其称为零分配。之后进程退出、NVML计算上下文清空。以上不是
整机物理RAM或泄漏接受结论，完整原始统一内存采样保留。

收尾只读核对17个已观察自有PID均消失、8762端口可用、NVML pids为空，
原共享锁dev66305/inode3294067可非阻塞获取并释放。没有启动恢复服务
或留下常驻推理服务。

## 复核与保留入口

离线独立复核从8个只读SQLite数据库重建全部响应，与客户端派生记录、
服务请求ID、质量期望和计时边界逐项绑定，重新计算上述性能结果。
同时核对3232冻结输入、160原始记录以及197个模型文件的size/mtime/
device/inode；模型权重仅做元数据核对，不声称重新hash所有权重。

- [独立复核与原值](evidence/mtp-selected-head-rows-20261106/independent-review.json)
- [资源释放与默认身份](evidence/mtp-selected-head-rows-20261106/cleanup.json)
- [归档复核](evidence/mtp-selected-head-rows-20261106/archive-verification.json)

本机阶段目录为原仓库`.q4t-work/mtp-selected-head-rows-20261106/`：
`source/.q4t-work/feasibility/`保存plan、四臂原件、postflight及decision；
`review.py`为只读独立审核脚本，已成功执行，`review.log`保留输出。
原始记录与3232输入以3405路径/3128去重对象封存在`evidence.tar`，
重读每个归档对象摘要通过；完整文件447447040 bytes，SHA256为
`5e76620643ae2240c294a3edf379fa44ccec93e963bfbe112665a899ffd896f0`。
归档内`catalog.json`将原绝对路径映射到`objects/<sha256>`，包含原
审查脚本、二进制和subset；大文件留在本机，不上传Git。

研究分支保留独有实现与本结果，默认及主线运行时保持原身份。
下一项优化需另立有界假设，本轮不自动展开其他路线。
