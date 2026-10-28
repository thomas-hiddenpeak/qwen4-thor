# 跨会话 prefix 共享：完整状态合同与有界方案

本任务是功能开发及性能优化，分别记录结论。父版本 d52fa6b，交付程序
09fe25ba5db65ae0bd343a110f2b7042de06f1bc7184bba51d10704638ad26b3。
沿用 codex/session-cache；候选在验收前不替换 build/q4t。

## 接口、边界与完整状态

- 显式 --prefix-cache-dir DIR，默认关闭；请求 prefix_cache_scope 为
  64位小写hex能力标识，同一agent组共享，不同组不查找对方快照。
  session_id仍是各自独立会话标识，两者可并用，客户端仍提交完整历史。
- --prefix-cache-checkpoints 默认8192,32768,65536；可配1至4个严格递增
  的1024至131072整数。业务请求长度不固定。只在这些位置保存状态，
  不从后面的递归状态截断出更短前缀，不动态扫描/调参。短于检查点或
  未提供scope的请求不保存共享快照。配置性能仅在实测范围取得接受。
- 选择同会话快照与同scope共享快照中最长的逐token精确匹配，平局优先
  会话快照；只恢复一次。键含scope及完整前缀SHA，hash不代替精确比较。
  共享快照不可变；读持有租约，写入/取消不暴露半成品，同组冷请求不等
  待彼此生成，允许冷miss重算。正常HTTP完成才发布该请求新建的快照。
- 同进程、纯文本greedy、MTP k3、显式槽位容量；完整head默认，子集
  显式选择。模型/head固定在服务进程，不跨重启或身份迁移。媒体、router
  trace、SSD专家卸载组合拒绝。token均为全局ID，suffix按提交历史重建。
- 共享快照在已完成target和shifted draft的prefill边界P产生：目标
  history[0:P)、全部SSM/conv/PLE、有效KV/raw/完整compressed组/RoPE；
  draft KV/raw/compressed、末行g、最后target trunk、实际pending ids[P]。
  复用SessionStateRanges与O_DIRECT/SHA路径，目的槽使用自己的页表。
  此时d0明确为未计算哨兵-1，必须至少再prefill一个token、计算最终g/d0
  才可进入decode；不为中间快照运行完整draft head。pending改变时重建
  draft最后一行，不能影响目标前缀。快照不含临时verify检查点。
- 恢复失败reset目标和draft后fresh prefill；GPU错误fail closed。
  并发分叉各有独立槽状态，不共享可写KV；快照引用不阻止执行槽释放。

## 资源及代价

共享池独立8项、8GiB SSD（含正在写入项）、30分钟TTL、空闲LRU，超过
预算跳过保存。沿用匿名O_TMPFILE|O_DIRECT，单8MiB staging，新增RAM预算
32MiB；与会话缓存同时开启时两池总SSD上限32GiB、新增RAM预算64MiB。
配置检查点最多4个，一次请求最多4次保存。不得称为释放预分配槽RAM。
查找最多8候选精确比较，复制/hash/IO持model锁，排队代价包含在HTTP。

## 冻结验证及决定

必要构建后首测tools/evalscope真实HTTP质量；随后CPU存储/选取/隔离/
取消/TTL/LRU/预算合同及两head独立完整状态GPU参考。新增参考重点是
prefill边界、不同pending的分叉、源/目的槽被覆盖、后续logits及全部
持久状态；不凭verify证明任意fresh prefill位级相同。

首轮总上限200 HTTP、14服务启动、4直接GPU fixture、首次GPU加载起6小时。
最多一次具体运行时修复与一次夹具修复，在未用总预算内重验受影响项；
ENV_ABORT停止自有进程并保留现场。无自动扩样/参数扫描。每阶段完整驱动
持共享独占锁；输入、身份、请求顺序、绝对截止在首测前写入机器计划。

A：两head各11固定HTTP质量及共享/隔离/分叉/会话优先生命周期，最多40请求。
B：两head直接状态参考，2次GPUfixture；CPU覆盖配置、读写、损坏和租约。
C：默认检查点，主128K先完成后3子32/48/60K并发，8K及32K公共前缀，
代码/文本两类，每类每配置4请求，OFF/ON各16请求，两head共64请求。
完整head OFF→ON，子集 ON→OFF；每服务初始空cache、不drop系统页缓存，
无warmup，首轮保存全部计入事务。主/子不同session ID，同组scope。
OFF保持同会话缓存开启，只不启用共享请求，隔离新增特性净收益。
输出统一256，合法EOS单列、不伪装等长吞吐。目标：两head分别在32K共享
代码/文本事务耗时减少≥10%，8K共享事务减少≥3%；子代理TTFT分别报告。
D：父/候选full、subset、plain各五档×3请求，共90请求/6启动，单流保护
普通decode/TTFT/质量，开启服务功能但无scope/session的请求不触发保存。
剩余最多6请求/2启动用于已定位受影响重验或安装健康，不自动用满。

测量是描述性配对，跨启动总体非劣和通用容差均未建立；明确保存成本、
未命中成本、命中分布、跳过prefill、输出/接受分布、SSD及统一RAM峰值。
已有suffix/子集映射、接受0/1/2/3、32请求排队与完整会话数值证据仅对
未变依赖复用；共享选择与新prefill分叉不能用旧证据替代。
正确性/质量关键失败或确认可归因保护退化则不交付；满足目标及保护后
按现有授权封存回退包、安装适用程序/元数据、健康、文档及提交推送。
预算结束分别记录证据有效性、测量完整性、统计结论、工程决定。
