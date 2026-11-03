# 结构化响应终态与资源交接修复

SRV-14实际托管首轮失败保留在
`.q4t-work/serve-managed-service-20261103/execution`。首个后端普通JSON、
普通SSE、严格JSON通过；第四次工具请求立即503
`structured_capacity_exhausted`。严格JSON响应已完整可读，但grammar
worker及外层lease约6ms后才完成回收。客户端按Content-Length读完后
立即发起下一条请求即可触发，不应要求客户端sleep、metrics轮询或重试。
本轮4生成/3负面/1启动，43.718秒，整体FAILED；清理已确认。
停止临时systemd单元使原guard记录ENV_ABORT/managed service stopped，
不能追认为原guard COMPLETED。旧默认280b95dc保持。

## 有界修复

独占状态分为free/running/finishing，指标structured_active仍为0或1。
终态字节发布前标记finishing；保留原lease直到grammar、请求、输出
缓冲全部析构。后继只在finishing时等待交接，running仍立即503。
等待者受已有max_inflight_requests约束，不创建grammar、不持有GPU
slot；每5ms检查既有请求deadline、显式取消、断连和服务停止。
原owner释放后同时竞争时只允许一名进入，其他可按原容量合同拒绝，
不承诺FIFO。慢响应仍受既有写超时约束；等待不绕过请求期限。

HTTP上下文的非拥有、单次terminal hook覆盖JSON成功/错误、SSE错误
及异常fallback；SSE成功在finish frame之前调用。hook先于lease析构
解除。异常展开仍先释放全部资源，再由外层生成fallback。
capabilities公开handoff等待合同，不改变grammar预算或输出格式。

## 冻结验证范围

这属于业务可靠性bug修复，无数值/模板/内核改动，不作性能接受声明。
CPU检查覆盖活跃拒绝、终态后保有所有权、释放交接、四类取消及超时，
以socket完整payload已读而owner故意保留的夹具确定性复现窗口；不靠
缩短/延长客户端sleep碰运气。再执行公共host及必需合同、零警告构建、
现有HTTP生命周期检查。

新候选GPU补验单独计数，不把重验追加算进原8生成预算。单次模型启动、
最多10次生成/3负面/40控制观察/600秒：普通JSON/SSE、严格JSON紧接
真实资料工具与答案；严格输出不足产生错误后立即严格JSON恢复；
严格SSE完成后立即严格JSON。完整wire、状态、资源归零、grammar
started=reaped、共享锁与停止清理均保留。完整驱动持有同一GPU lease。
通过后才替换默认并形成新的托管包，原失败包与回退包不修改。
托管自动恢复的未执行部分另按新身份冻结补验，不声称本修复等于其通过。
