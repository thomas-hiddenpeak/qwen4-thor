# 元数据候选集成验收判定卡

状态：FROZEN_NOT_STARTED。质量quality-v1必须完整PASS才运行。
本阶段是源所有权结构整理后的质量/性能风险评估，不宣称提速；
已经完成的对象/四层直接合同复用，不再重复。

## 身份与范围

父源码73aab36导出，候选f01e8fa实现。实际二进制分别来自本阶段
parent-build/q4t及build/q4t，驱动启动前保存副本与SHA，所有子阶段前
核验保持。默认build/q4t不改动。MTP代码路径本身未修改，但新增
owner初始化随Model加载，故off/on分别验收，不能只验普通decode。

## 固定预算与顺序

完整run_acceptance_tail.py经gpu_exclusive持锁，输出acceptance-tail-v1，
锁acceptance-tail-lease-v1，全程不释放，不自动重试。

1. 复用现有run_request_cancellation.py跑候选普通decode：max_seq2、
   max_len65536、max_prefill8192，固定20个生成接口请求（含拒绝/取消），
   9个取消控制POST及一个不完整HTTP连接；保留原health/metrics有界轮询。
   health每次等待上限120秒，20ms轮询；RST counters最多100次。
   总阶段上限1800秒，1次模型启动。校验排队取消/FIN/RST/deadline、
   active prefill取消与幸存请求、decode取消、ID复用与后继canary、
   带活跃请求关停。不能将HTTP取消当GPU故障注入。
2. 性能顺序固定parent-off、candidate-off、candidate-on、parent-on。
   每服务五档1024/4096/8192/45056/204800，各3条固定请求，总60条、
   4次模型启动，每服务7200秒上限。tools/evalscope HTTP E2E，max_seq1、
   max_prefill8192、max_len208896，greedy256输出，fixture来自B1b
   candidate-off的固定性能记录副本。startup300秒，port8740。
   完整保存三个样本，不按文件整理或观察方向补采样。

## 合同、指标与出口

正确性：HTTP输出/长度/确定性合同必须通过；同模式父候选prompt与
输出摘要逐档一致。取消合同失败停止，不继续矩阵。数值/质量失败
不能用计时解释，先保留并定位具体失败。监控失败/竞争ENV_ABORT。

全部TTFT与decode_tps原值公开，首请求分列，两个模式分别比较，
不以平均收益抵消单档回退。报告中位数百分比只作为观察值。
分辨带：NOT_ESTABLISHED；现有三次样本不能估计真实回退上界，
跨启动顺序效应不能由本轮自动排除；没有随意填入0.5%/1%。
因此统计性能结论默认INSUFFICIENT_EVIDENCE，不自动宣称提速、持平
或非劣；有明确可归因回退则拒绝/修复，不能因缺分辨带而忽略。

本候选增加上传owner/event，不是仅删除重复计算/元数据获取/同步，
不得直接套用B1a/B1b“有限工程接受”许可。完整证据形成后明确决定
保留候选、回退或请求用户认可具体风险；提交/推送不构成接受。
默认部署保持。采样预算用尽不追加校准或追求有利结果。

前置quality-v1现为ENV_ABORT，完整质量PASS未满足，本计划未执行。
见[中断与关停封存审计](evidence/inference-metadata-20261020/quality-v1/audit.json)。
