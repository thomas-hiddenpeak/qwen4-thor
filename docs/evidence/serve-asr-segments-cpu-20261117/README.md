# 完整segment源码与针对性CPU

16/16 CPU通过（0.016秒）、3项HTTP验收器正/负控通过（0.214秒），
零警告独立CUDA构建。0GPU，本阶段不是业务验收或正式发布。
候选身份及源码见build-receipt.json；此前统计来源16项按原身份复用。

完整segment保留真实正文token/logprob/固定实际温度、独立训练
无语音标签后验和隔离zlib压缩率。单clip独立解码形成segment，
首末时间为实际词边界；纯标点使用真实clip范围。默认verbose和
word+segment请求已接入；空转录不伪造段落。

温度为0时按完整尝试的平均logprob低于-1升级，最多六次，
同一原请求累计输入/输出配额和deadline，额外输入单独预留但不
增加RPM。达到输出上限而未完成接受尝试必须报错。普通JSON/SSE
保持逐token自适应。实际模型posterior/响应正确性仍待GPU短验。

下一组已冻结1启动/7创建/600秒：双SDK默认/组合/固定温度segment、
真实中文token/logprob对照、静音、原词JSON与核心Chat保护；
不运行完整性能或模型质量矩阵。之后继续VAD/服务交付及全部master。
