# Megatron 可靠性加固默认部署

状态：AUTHORIZED_DEPLOYMENT_PENDING。

2026-10-20，用户在获知候选已满足替换条件后明确要求“那么收尾吧”。
本次授权将默认753d9769替换为已验收bc348458，运行时源码0494f21。
这是加载生命周期可靠性加固，不主张性能优化；复用已通过的22条
HTTP质量、50份MoE数值对照、51项host及333次视觉上传回读证据。
详见[验收结果](MEGATRON_RELIABILITY_RESULT_2026-10-20.md)。

部署器tools/verify/inference_metadata/deploy_load_accepted.py，先复核
465份封存文件及视觉证据，保留旧binary、sidecar和真实构建缓存，
新发布包位于.q4t-work/releases/megatron-reliability-20261020。
现场位于.q4t-work/megatron-reliability-20261020/deploy-v1。

同一NVML lease内替换build/q4t及release sidecar，默认MTP与显式
关闭MTP各1条1024-token固定质量请求，对照已验收同模式结果。
最多2次加载、2请求，无重试或新性能矩阵；单模式最多600秒。
失败恢复已知旧版，拒绝覆盖未知身份。两个文件分别原子替换，
不承诺跨文件原子事务或断电自动恢复。验证结束停止自己的服务。

回退命令（保留备份后可用，未演练）：

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/megatron-reliability-20261020/rollback-v1/lease -- \
  python3 -B tools/verify/inference_metadata/deploy_load_accepted.py --rollback
```

该命令仅恢复本次旧版753d9769，不使用旧部署器回退本次版本。
