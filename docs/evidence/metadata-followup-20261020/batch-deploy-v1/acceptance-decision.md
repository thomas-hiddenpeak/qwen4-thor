# 新入口元数据加固：接受决定与默认部署

## 本项接受决定

2026-10-20，用户在读取两模式五档实测摘要及计时不确定性说明后明确
答复：“可以接受这次加固并部署”。据此对本身份记录
ENGINEERING_ACCEPTED_WITH_TIMING_UNCERTAINTY，并授权默认入口部署。
这不是沿用旧3d5564e的接受，也不扩展通用删除冗余接受规则。

运行时源码b6870c0，候选binary：
753d9769f96e9848800eae88b4800bef65e20885fd2ec025b8f3bc91fc497542。
本项收益是剩余推理元数据源生命周期明确及输入边界保护；成本、
错误边界见[最终审查](METADATA_BATCH_FINAL_REVIEW_2026-10-20.md)。
已完成直接合同、78文件差分、两模式质量及普通取消验证；两模式
五档60请求测量完整。普通decode中位数观察+0.2320%～+0.5625%，
MTP为−0.1950%～+0.3694%，TTFT最大观察增加0.2312%。这些差值
不作为容差或真实损失上界，统计性能仍INSUFFICIENT_EVIDENCE。
完整原值与边界见[补测结果](METADATA_BATCH_PERFORMANCE_RESULT_2026-10-20.md)。

## 执行前冻结

当前状态：ACCEPTED_DEPLOYMENT_PENDING，默认仍c8f4b241。
沿用已准备且已验证的binary/真实缓存，不重编译生产代码。部署器
为tools/verify/inference_metadata/deploy_batch_accepted.py，先只读
preflight验证源码、准备包、当前默认pair/cache、四阶段892份封存
文件及模型stat；执行时在同一NVML lease内再次核对。

部署产物：.q4t-work/releases/metadata-batch-20261020；现场与证据：
.q4t-work/metadata-followup-20261020/batch-deploy-v1。保存本接受
决定冻结副本、上一默认binary/sidecar/缓存及摘要。新sidecar指向
真实构建缓存、本项接受决定和性能报告。binary和sidecar分别原子
替换，不宣称跨文件原子事务或断电自动恢复。

最多2模型加载/2生成请求：默认MTP（不传MTP开关，核实实际k=3）
及普通模式各1条固定1024-token质量夹具，复用batch-http-v1同模式
参考；单服务最多600秒含启动，启动等待最多300秒，无重试，无
新性能矩阵。服务正常退出0、输出合同一致及guard COMPLETED后
才标部署健康检查通过。外层seal_stage在清理后封存。

失败时保留现场，仅清理自己的服务。校验备份后恢复c8f及原sidecar，
拒绝覆盖未知身份；SIGKILL/断电可能留下部分替换，需新lease下
核对现场。回退只允许已知c8f/753d的binary和新旧sidecar组合，
不会因未知并发修改而强行覆盖。回退路径保留原c8f真实缓存。

## 回退命令（仅准备，尚未演练）

在需要回退时，以全新的输出目录取得GPU lease后执行：

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/metadata-followup-20261020/batch-rollback-v1/lease -- \
  python3 -B tools/verify/inference_metadata/deploy_batch_accepted.py --rollback
```

该命令恢复c8f4b241；旧rollback_accepted.py仍只用于c8f→44abd8b1，
不能拿来回退753d。默认部署不创建常驻HTTP服务。本项健康检查
尚未执行，最终状态与证据将在执行后追加。
