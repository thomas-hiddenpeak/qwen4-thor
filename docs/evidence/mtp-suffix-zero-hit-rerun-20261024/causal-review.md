# 有效零命中复核：独立机制审查

结论：**没有足够证据选择唯一条件B运行时候选，建议B_NOT_RUN。** 本轮证据VALID、56/12完整、描述性复核有效；未证明总体非劣，也未识别可修复根因。没有新增GPU/HTTP/测试/硬件采样，原始与冻结文件不变。

正常重复中位收益重现：完整code/text +14.213%/+14.536%；子集共享B的描述性几何均值+6.800%/+6.283%，每个B/A均正。它们属于同启动3次重复和已见材料，不能抵消长上下文保护问题或批准部署。

## 容量、专家驻留和分配路径核对

12服务中同一head的budget报告和effective capacity逐字一致，均208896/1/8192。完整head启动计划1606项/96687337864B、SHA09f7ab7c…a8a72；子集1608项/97023144328B、SHAc1c402cb…8459f。所有计划策略stable_descending_bytes、consume/free完整。不是suffix导致计划或容量分叉。

源码先以MemTotal和固定配置计算budget，再分配248320B host分类表。分类表虽在device reservation之前，但BuildStartupPlan只按配置和shape建表，不按suffix或当前free-memory改变计划。命令均未设moe-resident-slots，默认0保持全驻留加载分支；未发现suffix相关专家slots变化。相同计划不能保证物理地址/cache/driver时序相同，但本轮没有证据指向其中一种，也不足以选择“移动分类表/重排分配”的修复。

## 正常与首long观察

- 子集200K：TTFT B−A1/A2 +275.549/+68.389ms，target-prefill完成+264.826/+66.019ms；A2−A1自身+207.160ms只能说明漂移，不能当容差。
- 子集45K：TTFT B−A1 -4.054ms、B−A2 +13.622ms，方向不一。
- 子集8K：decode -0.634%/-0.238%；完整单对+0.174%。同head目标工作相同，仍不能替代耗时保护；query+append+release仅0.103508ms，不足以直接解释15.766–41.988ms首内容后差。
- 首long45K ON更快41.895ms，200K ON慢34.276ms，其中target-prefill完成仅慢14.379ms。正的200K观察不要求本服务先经历索引请求，但单对不能量化allocator因果。两臂首long均比自身常规位置慢，首请求/预热/机器历史没有被完全隔离。

## 分层与原生计时边界

插桩8K HTTP首内容后+19.365ms；排除首发轮proxy的native wall/event+20.5ms，残差-1.135ms。draft+13.3ms的逐phase差舍入界±7.3ms；三phase总差界±21.9ms。该插桩对target layer总量反而-3.33ms（打印界±0.77ms）。它把观察定位到draft等phase，尚不说明原因，也不能搬到未插桩正常请求上。

插桩8K/45K/200K prefill layer event差为-9.76/-23.07/+29.17ms；handler-prefill区间差-9.84/-23.233/+29.755ms，差量残差仅-0.080/-0.163/+0.585ms。该组差主要位于event覆盖区间，但event包含stream idle/host提交间隙，且layer插桩引入同步。200K lin -69.69ms、full +99.09ms相互抵消，不能据此宣布full-attention缺陷或解释正常200K的264.826ms prefill完成差。

## 环境数据与停止理由

长探针报告的GPU/内存及CPU频率档位未变化；子集A2更热但200K快于B。read_bytes没有稳定的ON更多模式；首45K OFF有约8.95MB和16个major faults，而ON约0.545MB和1个。这些有spill、io_uring归属及1Hz限制；新request线程在前边界不存在，不能用common-thread差作为完整prefill CPU。PSI缺项不能写成0压力。

未发现冗余同步、额外目标工作、容量策略分叉或可由当前数据证明的allocator问题。移动表、索引池化、继续收紧match均缺乏解释当前零命中慢点的证据；PLE预取、调频和调度改造也不能作为临时条件B候选。建议有界收尾，根因仍未解决，保留f356和旧NO_GO。完整原值、源码及日志SHA在JSON。
