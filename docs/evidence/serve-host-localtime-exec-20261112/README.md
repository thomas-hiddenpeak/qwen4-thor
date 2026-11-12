# 公共CI剩余沙箱与父死亡夹具修复

基线12951dcd的两次远端原始结果完整保留为gzip，解压身份见
raw-log-identities.json。PR运行43234247688为57/58，push运行
43234236958为56/58；Batch旧失败本轮已过，不能说整套CI通过。

两份production sandbox诊断均为native/pdf ready=1、legacy ready=0。
全部列举路径中`/etc/localtime`实际解析为root所有、mode0777的普通
文件。SystemPath明确拒绝组/其他可写的非/dev/null资源，故CI步骤
补这个确定路径的go-w，只修改临时CI机器，未修改Thor或生产策略。
完整文档沙箱/实际解码断言保持，真实远端结果仍须续验。

push还出现Guardian父死亡用例失败：SpawnProcess按设计不阻塞等exec，
夹具却在刚拿到PID后报告可杀父进程。修正为先核对exec-status管道
EOF，再报告PID；仍要求实际adopted子进程以SIGTERM退出并确认回收，
不放宽到任意退出码。18项Guardian进程合同0.084秒全部通过，shell
语法检查通过；已有生产身份不变。冻结预算/实际输出及来源均保留。

0GPU、默认rev94保持。下一步为实际CI通过后合PR23，再继续全部master。
