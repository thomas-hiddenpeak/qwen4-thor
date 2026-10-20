# SSD offload基础修复默认部署

状态：DEPLOYED_HEALTH_CHECKED，部署及离线复核通过。
旧默认3ae53f26，当前默认19ea4e57（源码166a535）。仅在故障/驻留/质量/封存身份全部
通过后替换默认；可靠性接受不代表SSD策略性能或RAM目标已通过。

发布包.q4t-work/releases/offload-foundation-20261020；部署现场
.q4t-work/offload-foundation-20261020/deploy-v1。保留旧程序、sidecar、
真实构建缓存和摘要；分别原子替换程序及发布元数据，不承诺跨文件
断电事务。默认MTP k=3与普通各1条1024-token健康请求；失败按已知
身份回退、清理自己的进程，无常驻服务。回退入口：

```bash
python3 -B tools/evalscope/gpu_exclusive.py \
  --output .q4t-work/offload-foundation-20261020/rollback-v1/lease -- \
  python3 -B tools/verify/inference_metadata/deploy_foundation_accepted.py --rollback
```

恢复3ae53f26；部署与回退记录独立于随后offload资源基线。

部署实际完成两次加载、两条HTTP健康请求，MTP默认k=3/普通均文本
一致、长度一致、server exit0；单一NVML lease 70.97秒，86份封存
文件复核PASS。旧程序/sidecar/真实缓存回退身份核验通过，未执行回退
演练。无常驻服务，无性能样本；资源基线仍单独推进。
