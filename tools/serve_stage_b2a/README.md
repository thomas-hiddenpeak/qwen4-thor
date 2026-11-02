# Serve SRV-05a 验收与交付

合同见[推理字段与隐藏](../../docs/SERVE_STAGE_B2_2026-11-03.md)。
不包含真实推理预算；不据此关闭工业化目标。`reasoning_field`默认
reasoning_content，可选reasoning；`include_reasoning=false`只隐藏
解析出的推理正文，不改变生成/usage，也不屏蔽content中的自述。
两个控制只接受parsed messages，null拒绝。assistant历史别名接受
string/null，非null冲突拒绝，非assistant携带任一字段拒绝。

`template_diff.py`复用B1的checkpoint oracle，扩展别名/相等/空历史，
120例含108字节/token-ID对照和12角色负例；历史冲突在native CPU
合同和HTTP负例中验证，不把JSON Schema当作可比较任意两个字段值。
`check_schema.py`把响应封套与推理子集分开校验，另核对请求指定的字段
与隐藏要求；这仍不是完整OpenAPI。

驱动固定30分钟/3启动/14生成尝试/12负例，计划13生成，无自动重试。
包含两种字段×JSON/SSE×公开/隐藏的内容和usage对照、真实项目工具
推理历史回填、hidden schema、plain隐藏及安装路径HTTP健康。
prepare要求所有CPU合同、零警告、干净已提交源码和旧f1133bcc pair。

```bash
python3 -B tools/serve_stage_b2a/stage.py prepare
/usr/bin/python3 -B /home/rm01/models/dev/qwen4-thor/tools/evalscope/gpu_exclusive.py \
  --output /home/rm01/models/dev/qwen4-thor/.q4t-work/serve-stage-b2a-20261103/execution/lease -- \
  /home/rm01/models/dev/qwen4-thor/.q4t-work/application-service-20261102/sdk-env/bin/python -B \
  /home/rm01/models/dev/qwen4-thor/tools/serve_stage_b2a/stage.py execute
python3 -B tools/serve_stage_b2a/stage.py finalize
```

交付/回退复用身份绑定、fsync恢复标记、未知pair拒绝与共享GPU锁。
回退需冻结源码commit（package/source.bundle）及原绝对证据路径：
`rollback --output <新lease父目录>`，全程由同guard包裹。两文件各自
原子替换，恢复标记处理两次替换间的中断，不宣称跨文件断电事务。
