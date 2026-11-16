# Chat审核针对性CPU接受

2026-11-17，26项C++和8种schema/native预检/双客户端病例最终通过。
原生预检读取只读checkpoint tokenizer并逐字节比较既有模板和token
IDs；没有加载模型。审核状态用显式分类夹具验证，不冒充真实分类。

覆盖输入/全choice输出score/block、分类错误策略、流式缓冲和重复
终态、过滤输出持久化、同一Batch预算、普通Chat/null兼容及受影响
独立Moderations合同。首轮构建两处API使用错误已修复，原输出保留。
首轮前端3/8通过；四个预期补回既有refusal模板头，一项修复JS对
合法缺省logprobs的误拒绝，仅补五个失败病例，最终全部通过。

726来源/7负控、生成物一致性及隔离host/engine零警告构建通过。
默认rev196及程序未修改，0GPU。继续最多4启动/18个外部生成请求/
900秒真实业务，内部分类另计实际token/步骤；尚未声明运行时通过。
全Serve任务仍active，不以本阶段CPU接受替代剩余实现和交付。
