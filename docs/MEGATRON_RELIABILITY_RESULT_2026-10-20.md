# Megatron可靠性两项工作的本轮交付

状态：**冻结范围完成，加载期候选通过正确性/质量验收，未部署。**
运行时候选0494f21，binary
`bc348458db08f7d8006ab9416fd83dd4810fc905af9bd6d2a766fecab937790a`。
最终收尾只补文档、独立工具、host清单和一处注释语法修正；未改动
已验证的执行逻辑。默认build/q4t仍为已接受753d9769，未启动常驻服务。

## 两项目标交付

1. 数值回归与加载期生命周期：新增MoE行重排、零/有限大值无关行、
   scratch复用检查；统一8类dense/vision权重加载、MTP BF16专家和
   并行NVFP4 worker的源完成边界。确认的是API所有权合同缺口，
   没有将其声称为已捕获的真实模型数据损坏。
2. 调度状态提交、取消和复用：完成生产交接与字段归属审计，新增
   5项host状态合同并加入常规必跑清单。检查非单调slot、幸存请求、
   在途取消、Cancel/Finish竞争、旧ID/key清理与新请求隔离。
   不引入新的重叠调度或暂停/恢复功能。

详见[冻结计划](MEGATRON_RELIABILITY_PLAN_2026-10-20.md)、
[调度合同](MEGATRON_SCHEDULER_CONTRACT_2026-10-20.md)、
[加载期审计与范围外入口](MEGATRON_LOAD_OWNERSHIP_AUDIT_2026-10-20.md)。

## 验证与证据

| 项目 | 结果 |
|---|---|
| 构建 | 生产候选及独立工具零警告；新增target首轮未被旧Make入口识别，显式configure后成功，原日志保留 |
| 首组MoE回归 | 1次层加载、17次前向；10个保护行logits/IDs/weights/output逐位一致；T1/T4观测相同但不作通用逐位门槛 |
| Host合同 | 5项新增状态、8项序列、3项提交检查通过；7项驱动和7组加载源错误注入通过；扩展必跑清单51/51无跳过（部分项目重叠，不相加） |
| 真实CUDA上传 | 默认/非阻塞两stream各8字节，完成后query及内容检查通过 |
| 候选MoE | 与首组父版50份原始数据文件逐位一致 |
| GPU算术实现 | 父753d与候选119个函数的有序名称、SASS指令/地址及编码摘要一致；不含host代码及动态库内部实现 |
| HTTP质量 | v2普通/MTP各11/11，文本和长度与冻结参考一致，两服务exit0 |
| 视觉加载补项 | 1次tower加载，333次H2D、897862112字节全部回读SHA256一致；不是视觉任务质量验收 |
| 环境与封存 | v2完整外层lease约544.5秒，NVML 500次monitor无竞争，COMPLETED/child0；465份阶段文件摘要及视觉独立清单复核 |

[离线审计JSON](evidence/megatron-reliability-20261020/load-source/audit.json)、
[GPU终态](evidence/megatron-reliability-20261020/load-source/guard-v2.json)、
[host清单](evidence/megatron-reliability-20261020/load-source/release-host.log)、
[视觉回读](evidence/megatron-reliability-20261020/load-source/vision.log)。
复算工具见[运行说明](../tools/verify/inference_metadata/LOAD_SOURCE_README.md)。
原始binary、源快照、完整输出保留在`.q4t-work/megatron-reliability-20261020/`。
Git摘要不是替代本机原始payload的完整重建包。

## 首次中断及处理

v1普通质量已完成，MTP最后一条尚未完成时，本会话在lease外并行
启动q4t_host_tests。保守进程规则命中该host binary（无NVIDIA计算
context），guard正确记ENV_ABORT。不是外部用户工作或NVML查询失败。
保留[中断事件](evidence/megatron-reliability-20261020/load-source/abort-event.json)
及所有原始文件，不追认v1整阶段通过。

停止并行验证后执行一次完整v2，并把vision补项纳入同一锁。host检查
改为GPU前或同guard子进程顺序执行；未放宽监控规则、未改名绕过。
最终结果仅采用完整v2。51项host自身通过不抵消其造成的环境中断。

## 成本与工程决定

这是可靠性加固，不是性能优化。dense源直接读入owned storage，不新增
整tensor快照；每tensor和每expert复用前增加显式等待，可能增加启动
成本。v2实际普通model_load为26.038秒；MTP模式model_load为26.657秒、
mtp_load为3.273秒，startup_total分别26.742/30.789秒。旧参考启动
分别记录model_load 28.501/26.551秒、mtp_load 2.752秒；不同启动与
缓存状态不能作为配对性能结论，完整原值均保留。

按用户2026-10-20非性能任务原则，正确性、输出合同及适用质量满足，
以有界验证接受本轮可靠性候选。不宣称吞吐提升、统计持平或五档
性能通过，不为小幅计时差追加样本。此次未改变推理执行逻辑，未新增
性能矩阵。默认部署不属于这次冻结交付，753d保持。

## 完成范围与后续

本轮覆盖上述加载入口、固定MoE夹具及调度host合同，不保证所有文本
或形状逐位一致，也不替代整模型质量的未来验收。普通取消旧证据按
未变调度实现复用；没有新增完整MTP取消矩阵或视觉语义质量结论。
审计另记录MoERoutedForward局部offset_h及residency分块源等热路径
待审项，未修改或宣称已闭合；后续另冻清单，不能把本轮完成扩大为
全仓异步内存安全证明。
