# Base音色主/轻profile实际HTTP驱动CPU验证

新增8项及4项直接受影响合同全部通过，12项/2.517秒，
0GPU/模型加载。固定输入SHA/完整PCM/独立反例、启动前3客户公平
容量、24业务POST预算、两profile命令Base依赖、CLI、返回ID与
无提示ASR、错误成功/错转录/缺历史拒绝和请求上限均有针对性检查。
CPU响应脚本只测试验证器与协议执行，不能作为真实业务或恢复资格。

profile_acceptance prepare增加--tts-base-config和--voice-fixtures，
前者同服务包native_tts_base结构，后者format为
q4t-profile-voice-inputs-v1、clips含consent/sample/different/negated，
各条为绝对WAV path及sha256。四条完整原始输入不同、总量≤8MiB，
仅作为测试输入，不预先假定它们符合授权或同声要求。当前英语请求
使用固定授权语言en，真实ASR由服务执行，调用不传原文/语言提示。

每个profile实际执行12个业务POST和5个元数据请求：先验证授权、
跨客户、异声/配额反例，获取本次真实ID后合成两句新文本，独立ASR
检查两份音频并GET存储Chat历史，再撤销并确认不可继续使用。健康、
实际usage和自然EOS一起生成voice证据；任何中途失败保留原始wire。
配置不足3队列槽时在启动前拒绝，须显式选择被验profile容量，驱动
不会静默扩大包或部署的预算。Base与ASR/VAD/speaker固定身份一起绑定。

来源审计940锚点/17负控通过，新增helper也自动入表。
默认rev316保持。实际两profile完整业务、Base-only已有音色消费资格、
保存状态回退及原合成音色不匹配仍待闭合，总目标继续active。
