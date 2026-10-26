# 并发首启动容量门失败：针对修复与预算调拨

F原件 feasibility/frozen-plan.json 5d23f839，a8717ec1 程序。4.626秒即
STOPPED；模型未加载、生成HTTP为0，首错误是原单流内存规划拒绝MTP
max_seq4。监控终态正常，无ENV_ABORT。原件和启动日志不覆盖。

具体修复：内存预算按实际槽数计算batched scratch、target checkpoint，
同时持有的prompt trunk/rolling g/host历史按槽增加，model_mu内串行的
DraftExtend chunk/forward temp不重复乘槽。容量搜索使用完整峰值，不能
只除state_pool。启动预留支持门也从单槽改成所有verify行必须容纳于
max_prefill；底层PlanMtpResources/PlanVerifyResources原已按槽计费。
suffix索引和分类缓存显式计入预算。保留MTP关闭与B=1原金额合同。

按Bug修复顺序，先有界CPU直接验证本次具体容量失败（180秒、零GPU），
再补尚未执行的HTTP及直接GPU合同。此修复使用第一次针对修复额度，
第三次构建，原总期限不变；不绕过--no-budget、不改数据或收益阈值。

启动预算调拨：首轮未加载启动仍计1次。F后续4启动保持；从原部署块2次
启动调拨1次，整任务仍至多20次。两head各3个取消/新请求合同移到F已有
ON服务（因此F剩余136生成请求）；部署块保留1启动/3健康请求。总请求
仍401（F136+B262+健康3），原F7200秒及任务23400秒期限保持。原失败已耗
时间及CPU修复时间计入F原剩余时间，不重置预算。没有新增重复性能样本。
