# 标准计数首轮真实业务与 nullable 合同补齐

2026-11-10。首轮候选05c1649e真实业务通过：1启动/4生成/15计数/51.451秒、50 NVML样本全OK；官方SDK省略/null/空body、actual count=usage、SSE/JSON、密文/持久/手工历史、真实docs/MODEL.md工具/引用闭环、会话不追加、计数不耗生成额度，实际仅4模型步骤。退出0，独立确认进程/端口/共享锁释放、默认保持。

持续合同审查发现SDK count的text/tools/tool_choice可null而create不可null；此前直接复用create解析导致该合法count组合失败，未把首轮短验当全部合同完成或默认交付。只在count归一化4个不同nullable字段，生成合同保持。3项直接C++（含新增10个nullable逐项/组合和create不放宽）及1项驱动通过；其余21不同C++/2schema/2原生入口/3驱动与首轮业务路径保持，复用其证据。必要构建零警告，最终候选身份见identity.json。

新增有界补验最多1启动/1生成/4计数/300秒；两轮累计不超过2启动/5生成/20计数/900秒。仅验证空/null与实际任务默认等价、actual count=usage及零额外模型/RPM/TPM；不重复工具或完整性能/质量矩阵。当前默认639c6b7b保持STOPPED，等待此补验后交付，A07.8及全部总任务继续。
