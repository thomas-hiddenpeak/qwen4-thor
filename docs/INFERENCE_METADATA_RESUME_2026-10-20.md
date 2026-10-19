# 使用默认NVML后端恢复候选验收

用户2026-10-20要求“继续吧”，允许NVML作为默认后端。本次依据是
独立进程监督、明确错误合同和最小上下文验证；不宣称理论上必然
更可靠或已经修复驱动。默认NVML，显式--compute-backend nvidia-smi
保留，无自动fallback；默认路径27项host回归通过。

## 身份与复用

候选源码3d5564e（包含owner和容量拒绝），q4t SHA c8f4b241…；当前
src/include/CMake与该提交无差异。父版本73aab36独立构建。
run_resume_acceptance.py执行前核对候选历史构建摘要及父replay身份，
复制二进制和各自CMakeCache至阶段输出，记录完整SHA。默认部署
build/q4t保持44abd8b1，不替换。

对象实现未变，复用已通过host故障/GPU上传证据。容量修复新增拒绝
分支已过6项入口检查；合法容量分支沿用原四层28文件差分的依赖证据，
不将旧binary的全模型HTTP追认为新binary通过。新候选off/on各做一次
完整质量组，使用原冻结B1b题目与参考；旧ENV_ABORT和单次诊断不改标。

## 本轮有界清单

完整驱动由seal_stage经默认NVML guard持同一把锁，关停后封存。
输出.q4t-work/inference-metadata-20261020/resume-acceptance-v1。

1. 新候选质量off/on：各11条、各1加载，单模式1800秒上限。
2. 候选普通decode取消回归：原run_request_cancellation.py，1加载、
   20个生成POST（含无效请求）、9取消POST和不完整连接；1800秒上限。
   health/metrics原有有界轮询保留，不新增请求策略。
3. 五档性能：parent-off、candidate-off、candidate-on、parent-on，
   各1加载，每档3条，1024/4096/8192/45056/204800，固定greedy256；
   共60条，单服务7200秒上限。完整保留所有观测，不补采样。

合计7加载、102生成接口请求（含取消测试的拒绝项），另9取消POST。
质量与性能max_seq1/max_prefill8192/max_len208896；取消按既有夹具
max_seq2/max_len65536。启动上限300秒包含于子阶段时限。每一步成功
才进入下一步；失败或超时停止，无自动重试。监控失败仍ENV_ABORT，
不因NVML默认而放宽，不通过读陈旧结果继续。guard清理时间另计。

## 判定

质量须满足原题目输出/长度/终止合同；取消须无failure且服务exit0；
同模式父候选性能prompt/输出摘要必须一致。原值和逐档中位数仅作
观测，没有已建立分辨带，统计性能结论仍默认INSUFFICIENT_EVIDENCE。
不把测试完成、点估计接近或提交当性能接受；不套用B1a/B1b许可。
全部证据形成后，分别判断必要容量修复与owner重构的保留/回退和
具体风险，不预先承诺接受。新失败根据具体证据定位，不扩大预算。

## 执行终态

本轮MEASUREMENTS_COMPLETE_ACCEPTANCE_PENDING，guard COMPLETED。
7加载及固定质量/取消/五档全部完成，未重试；615封存文件复核通过。
NVML 3595查询全部OK。性能统计证据仍不足，默认模型不替换。
完整结果见[报告](INFERENCE_METADATA_RESUME_RESULT_2026-10-20.md)。
