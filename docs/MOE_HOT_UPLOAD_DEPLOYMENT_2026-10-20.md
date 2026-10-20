# MoE热路径可靠性修复默认部署

状态：DEPLOYED_ACCEPTED_HEALTH_VERIFIED。默认build/q4t已为d4232b63。

2026-10-20，用户在获知候选满足替换条件后明确要求：
“收尾吧,以后的工作具备条件,你就进行收尾工作.”
本次将默认bc348458替换为已验收d4232b63；未来同类具备条件的
当前任务直接完成收尾，授权已写入AGENTS.md，不再单独请求确认。
验收标准不变，不自动部署未通过的研究候选。

修复源码7bb190f，验收binary：
`d4232b63e53bece8c82bd87d8dcac628fb089f4f7eddc163535030133107f7a6`。
源码与测试快照仅有已记录的一处日志语句换行差异，执行逻辑相同。
复用281份证据、两模式22条质量及真实MoE数值/所有权合同，
见[验收记录](MOE_HOT_UPLOAD_AUDIT_2026-10-20.md)。
本项按可靠性任务接受，不作性能优化或统计非劣主张。

部署器tools/verify/inference_metadata/deploy_hot_accepted.py；
在同一NVML lease内核验身份、备份旧binary/sidecar/真实缓存，
替换build/q4t及发布元数据，MTP默认k=3及普通模式各1条固定
1024-token健康请求，对照已验收同模式结果，无重试或新性能矩阵。
最多2次加载/2请求，单模式最多600秒；失败保留现场并恢复已知
旧版，拒绝覆盖未知身份。两个文件分别原子替换，不承诺跨文件
原子事务或断电自动恢复。结束后停止自己的服务。

发布包：.q4t-work/releases/moe-hot-upload-20261020。
现场：.q4t-work/moe-hot-upload-20261020/deploy-v1。
回退（保留备份后可用，未演练）：

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/moe-hot-upload-20261020/rollback-v1/lease -- \
  python3 -B tools/verify/inference_metadata/deploy_hot_accepted.py --rollback
```

此命令仅恢复bc348458；旧部署器不能用于回退本次默认版本。

## 收尾结果

部署及两模式短健康检查通过：默认MTP日志确认k=3，普通模式
显式关闭MTP；各1条实际1024-token请求，输出与同模式验收参考
一致，两服务exit0，无常驻服务。单一NVML lease约76.37秒，
guard COMPLETED/child0。86份部署文件封存复核，新默认binary、
release sidecar及真实构建缓存匹配。旧bc348458的binary、原
sidecar及缓存已核验，保留可回退，回退未演练。

[离线审计](evidence/moe-hot-upload-20261020/deploy-v1/audit.json)、
[部署状态](evidence/moe-hot-upload-20261020/deploy-v1/deployment.json)。
复核命令：`python3 -B tools/verify/inference_metadata/audit_hot_deployment.py`。
新增仅2条健康请求，无性能矩阵；本次部署收尾完成。
