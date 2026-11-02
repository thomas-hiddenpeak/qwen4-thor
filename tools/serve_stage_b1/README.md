# Serve B1 验收与交付

范围是[SRV-04消息profile](../../docs/SERVE_STAGE_B1_2026-11-03.md)，
不包含推理预算、stop或预检。`template_diff.py`比较CPU原生渲染与固定
checkpoint Jinja，逐字节、逐token-ID验证46正例及12拒绝形状；保留
21旧fixture。固定Transformers5.16.1/tokenizers0.23.2/Jinja3.1.2，
不是原fixture生成时5.17.0；旧21字节期望同时校验。

`verify.py`用Python SDK3.42.1严格响应解析，默认MTP和plain分组；
首条developer/system回答与usage对照、thinking SSE、真实资料工具
往返、12个错误位置与零生成副作用、安装后实际HTTP健康。
限制30分钟/3启动/9生成/12专门负例，无自动重试。

```bash
python3 -B tools/serve_stage_b1/stage.py prepare
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-stage-b1-20261103/execution/lease -- \
  /home/rm01/models/dev/qwen4-thor/.q4t-work/application-service-20261102/sdk-env/bin/python -B \
  /home/rm01/models/dev/qwen4-thor/tools/serve_stage_b1/stage.py execute
python3 -B tools/serve_stage_b1/stage.py finalize
```

prepare要求CPU记录、零警告、干净已提交源码；默认pair冻结为142bb9d4。
execute/rollback使用已有跨会话锁、未知身份拒绝和fsync恢复标记。
回退工具需在冻结源码环境（package/source.bundle的提交）运行，保留
原绝对证据路径，`rollback --output <新的lease父目录>`由同GPU guard
包裹；只能恢复已知pair，不覆盖外来改动。两文件各自原子替换，恢复
标记处理中间状态，不声称跨文件断电事务。
