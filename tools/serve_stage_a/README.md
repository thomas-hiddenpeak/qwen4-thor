# Serve 阶段 A 验收

`stage.py` 复用已验身份绑定的默认 pair 替换/回退；`verify.py` 冻结真实
项目工具闭环、Python SDK 3.42.1、HTTP 错误/追踪头、7 秒 idle 代理、
普通/严格混合取消、deadline、活动关闭和实际 TCP EAGAIN。纯传输
schema 验证使用本机 `jsonschema`，版本记录在结果中。

总上限为 30 分钟、3 次模型启动、12 次生成尝试、16 次负例；计划
生产候选 7 生成/9 负例、故障构建 2 生成、安装健康 2 生成。
不自动重试。各轮和失败均保留，修复后需要显式按剩余预算冻结续验。
启动使用完整 head、默认 MTP k3、单槽、max_len=8192。仅验证业务
正确性与运行边界，不作性能优化接受声明。

故障构建只缩小发送缓冲，并在真实 `send` 返回 EAGAIN 时发出有界
握手；取消由真实控制接口执行。生产交付不包含故障宏。该证据证明
已观察到背压下的回收，不代表所有内核缓冲、代理或租户负载都已覆盖。

```bash
python3 -B tools/serve_stage_a/stage.py prepare
/usr/bin/python3 -B tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-stage-a-20261102/execution/lease -- \
  /home/rm01/models/dev/qwen4-thor/.q4t-work/application-service-20261102/sdk-env/bin/python -B \
  tools/serve_stage_a/stage.py execute
python3 -B tools/serve_stage_a/stage.py finalize
```

`docs/serve-transport-v1.schema.json` 只冻结本阶段响应封套和传输约定。
SRV-08 完整输入 schema、JS 和其他 agent SDK 矩阵仍待后续完成。
