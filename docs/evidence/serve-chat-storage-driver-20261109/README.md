# Chat持久真实业务驱动CPU验证

2026-11-09。2项Python负控/2项实际官方SDK桥接CPU通过。
流式对比拒绝丢choice/概率/usage和身份漂移；最终存储故障必须有真实
输出前缀、唯一unknown错误，无成功finish/usage。桥接对实际HTTP
核对CRUD方法/nullable查询/错误与一次请求，流式保留全部choices和
原字节并拒绝缺少DONE。没有GPU模型运行，不算业务质量验收。

下一步冻结1模型启动/7条Chat/至多9choices/600秒，同一严格租约；
只暂停自己的准确对象worker，模型不重启；全部拥有资源/端口/锁
清理，默认保持。短验通过后交付并继续全部剩余主计划。
