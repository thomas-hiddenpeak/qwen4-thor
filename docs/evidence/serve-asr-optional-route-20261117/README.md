# ASR可选前置路由补齐

PR44原head `6068ec981052db9d1759db70f75c5caee99a9744` 的
[CI](https://github.com/thomas-hiddenpeak/qwen4-thor/actions/runs/44074522988)
编译成功，58组CTest中57组通过；`q4t_recovery_gateway_contracts`
发现可选nginx前置漏宣告`POST /v1/audio/transcriptions`。原失败保留，
没有忽略检查、删除API或缩小前置能力。

已将该接口补入standalone和supervised共用data路由。4项针对性CPU
检查通过，0.165899秒、0GPU：全部公开接口覆盖、缺失/重复拒绝、
新增API不能漏声明，且实际生成的路由表达式允许转录POST及query，
拒绝错误method、相邻私有路径、fragment和config。未启动nginx。

只变更可选前置配置生成器和测试；已发布原生入口/模型/ASR资产及
默认rev276不变，之前真实业务证据按身份复用。后续对齐开发不在
本修复提交中；PR44以修复后的最终head重新检查，通过后合main。
