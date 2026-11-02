# serve 观测与编译资源首组验收

仅此冻结阶段，8 个模型请求尝试（含显式取消、deadline、shutdown
各一次）/ 1 个503拒绝 / 2次模型启动；总上限10 / 6 / 3、30分钟。
正常完成为排队普通文本、真实项目工具两轮、安装健康两次。

未读 SSE 客户端占据严格请求租约，验证第二个严格请求503、普通请求
排队，显式取消后普通请求完成；再验短 deadline、真实工具读取与回填，
最后在严格请求中正常 SIGTERM。只通过创建时保存的 pidfd 停本阶段
服务，不操作发现的外来进程。对照指标与请求诊断，读取 RSS/HWM /
MemAvailable，不把重叠计量相加为物理峰值。

```bash
python3 -B tools/serve_resources/stage.py prepare
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-resources-20261102/execution/lease -- \
  /home/rm01/models/dev/qwen4-thor/.q4t-work/application-service-20261102/sdk-env/bin/python -B \
  /home/rm01/models/dev/qwen4-thor/tools/serve_resources/stage.py execute
python3 -B tools/serve_resources/stage.py finalize
```

compile 仍不可中途抢占，shutdown 可等待它返回，32/64 MiB 不是编译
峰值 RSS 硬限制；未读响应也不等于已写满内核缓冲。参见
[完整冻结边界](../../docs/SERVE_RESOURCE_BOUNDARIES_2026-11-02.md)。
