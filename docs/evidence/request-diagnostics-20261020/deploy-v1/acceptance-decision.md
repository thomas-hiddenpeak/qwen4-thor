# 请求诊断默认部署

状态：等待本轮验收通过后执行。

用户明确授权本任务“具备条件后直接收尾”，并已有AGENTS持续授权。
旧默认d4232b63，候选3eb9b2a032db927988468e38855d67892a2a21482e7e2142ae64ce6de513a9a8。
仅补handler请求诊断；运行时源码必须与封存快照完全一致。普通/MTP
质量、host和定向HTTP诊断通过后才由部署器替换默认程序。

发布包.q4t-work/releases/request-diagnostics-20261020；现场
.q4t-work/request-diagnostics-20261020/deploy-v1。旧binary、原sidecar
与真实缓存完整备份并核验。新程序及sidecar分别原子替换，不承诺
跨文件原子事务或断电自动恢复。

同一NVML lease内最多两模式各1条1024-token短健康请求，默认MTP
k=3、普通显式关闭；无性能矩阵或自动重试。失败仅清理自己的服务，
按已知身份回退，拒绝覆盖未知改动。健康检查完停止自己的服务。

回退（部署后可用，未演练）：

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/request-diagnostics-20261020/rollback-v1/lease -- \
  python3 -B tools/verify/inference_metadata/deploy_diagnostic_accepted.py --rollback
```

该命令恢复d4232b63；不可用旧部署器回退本次版本。
