# 资源准入修复默认部署

状态：ENGINEERING_ACCEPTED_RELIABILITY，等待部署健康检查。用户授权“具备条件后直接收尾”，适用
AGENTS持续授权。当前默认3eb9b2a0；候选
3ae53f268119d0b391e974d6989cc1ee6a182e38e79503f793d562f0a822ea98。

验收条件：53项host、普通/MTP共6条质量、7-handler回收合同、4种
受控故障合同全部通过，GPU lease和封存身份完整。非性能可靠性接受，
不宣称吞吐优化或统计零退化。见[审计记录](RESOURCE_ADMISSION_AUDIT_2026-10-20.md)。

发布包.q4t-work/releases/resource-admission-20261020；部署现场
.q4t-work/resource-admission-20261020/deploy-v1。保留旧binary、
sidecar、真实缓存，核验身份后分别原子替换程序和sidecar；不承诺
跨文件事务或断电自动恢复。新旧源码、验收快照及发布缓存必须匹配。

部署同一NVML lease内，默认MTP k=3/普通各1条1024-token健康请求，
失败清理自己的服务并按已知身份回退，拒绝覆盖未知改动。无常驻服务。

回退入口（部署后可用）：

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/resource-admission-20261020/rollback-v1/lease -- \
  python3 -B tools/verify/inference_metadata/deploy_admission_accepted.py --rollback
```

恢复3eb9b2a0；不能用旧阶段部署器回退本次版本。
