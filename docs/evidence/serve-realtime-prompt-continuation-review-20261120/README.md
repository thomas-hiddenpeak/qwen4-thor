# Prompt缺项续验：主配置通过、轻配置工具终态未通过

2模型、10次外部生成+1次内部CSV摘要、5注册HTTP、2WSS，
118.997秒，112次NVML正常，全部自有进程、端口及锁释放。
主配置4轮Prompt得到42/377/42/52，CSV实际1270 tokens一次RPM，
旧Responses模板返回42；主配置5次原Chat/Realtime证据按来源复用。

轻配置Chat schema及工具闭环完成，Realtime实际工具调用成功；
回填后模型把正常请求解释为拒答，128 token预算耗尽，服务如实返回
incomplete/max_output_tokens。答案原文及完整流式终态保留。
轻配置Prompt与旧Responses尚未执行，新资格仍FAIL，默认rev356不变。

一次资格续验已停止，不自动循环。失败答案直接引用拒答规则，
下一步核查全局拒答模板是否干扰正常任务；这是待验证假设，不宣称
唯一根因。已有拒答生成/解析/流式能力必须保留，不能删功能过验收。
所有master继续。原件见raw.tar.gz，摘要清单见index.json。
