# Chat保存及未知提交隔离真实业务组合验收

2026-11-09。最终补验1启动/2Chat/44.749秒通过，严格租约
COMPLETED/child0。暂停精确自有存储worker后，原生恢复回收worker；
实际SSE返回object_commit_unknown及原completion_id，无成功终态，
同PID模型随后MTP回答42。此前持久对象在跨进程及worker恢复后仍一致，
删除/404有效，计数1成功1失败、准入/worker资源归零。

累计3启动/8Chat/141.076秒，其中两个原FAILED不追改：首轮SDK
分页驱动误读和第二轮公开错误码缺陷。正常JSON/SSE多choice/logprobs
落盘、真实项目资料工具/回填JSON、原历史及metadata通过证据按不变
依赖复用；最终只重验修正错误出口和同PID业务。4生产文件变化均为
公开错误投影，相关5CPU已通过；没有重跑无关推理。

138次NVML全部OK；每次模型退出0、1606启动分配全释放、拥有进程/
端口/锁清理。最终配置2份严格匹配schema。最新候选/协议身份见
decision.json，默认仍保留上版，接着可回退发行及全部Serve余项。
这是针对性业务验收，不作全模型质量、性能或用户长稳通过主张。
