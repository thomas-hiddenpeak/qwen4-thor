# 完整会话前缀缓存：状态合同与有界首版

用户要求主 agent 等待工具/子代理期间释放槽位，槽位运行其它请求后恢复，
避免重算已有长前缀。本项同时是功能开发和性能优化，分别作决定。
基线 main cb4b085、已交付程序 f2a6de360e05676c5fee2f86fbd314c9a9ff3ea0378fa104e0185cef6b4ec164；
新阶段 codex/session-cache，候选不得提前替换默认程序。

## 完整状态及边界

- 只支持同一服务进程的纯文本、greedy、MTP k=3、显式槽位容量。
  完整 head 默认，65536 BF16 head 显式选择；suffix 独立开关不变。
  不支持媒体、跨重启/模型/head迁移、任意 token 中途抢占、SSD专家卸载组合。
- 每次正常请求结束释放槽位。保存点是最后一个完成的 MTP 边界：目标
  history[0:P)、position=P；草稿已消费 shifted history[1:P]+pending b。
  普通 decode 收尾最多4个输出 token（最多3次forward）不在快照内，下次随新增内容计算。不是
  每 token 写盘，也不声称完全不计算任何旧尾 token。
- 保存全部目标层 SSM、短卷积、PLE卷积，完整注意力有效 KV[0:P)、
  index raw[0:P)、完整压缩组[0:floor(P/4))，三行RoPE和rope_delta；
  同样保存草稿有效 KV/index、rolling g、d0、pending b、最后一行目标
  trunk和完整全局token前缀。
  页表是启动时固定布局，恢复到目标槽位自己的页表和偏移，不搬源槽物理页号。
  权重、共享workspace、临时verify checkpoint不属于会话；恢复先reset目标
  槽并使checkpoint失效。suffix索引由已恢复且提交的history重建，成本计入。
- 当前EOS跳过draft extend；仅缓存请求所在的行需补齐最终extend，否则
  不能宣称draft状态完整。接受前缀/目标logits/输出终止语义保持。
- 客户端仍提交完整prompt/messages和64位hex随机session_id；服务逐token
  精确核对目标history[0:P)，且至少有一个新增token。若ids[P]不同于
  保存的pending b，利用保存的最后一行目标trunk重建draft位置P-1（仅
  一行，绝不重算目标长前缀）。这闭合工具返回替换预测下一token的边界。
  目标前缀不匹配、淘汰、过期、I/O或
  完整性失败均记miss并reset后正常prefill；GPU错误仍fail closed。
  同session并发409，排队/取消/异常用RAII解除占用；失败请求不发布新快照。
  修改历史、thinking/template重写可能miss，不能绕过前缀校验。

## 容量与生命周期

显式 `--session-cache-dir DIR`；目录须本用户0700，非tmpfs，匿名
O_TMPFILE|O_DIRECT文件0600。首次版固定8项、24GiB总SSD（含正在写入项）、
30分钟TTL、LRU淘汰空闲项，前缀至少8192 token。无跨进程恢复；退出/淘汰
关闭fd自动回收。客户端session_id是不可猜测的能力标识，不输出到日志。
统一RAM新增预算32MiB：单个共享8MiB对齐staging、最多8份token/history、
分块SHA256及少量元数据；传输持有model_mu，既包含CPU拷贝/hash/I/O，也
会阻塞其它槽位，必须在混合4路E2E计入。文件页缓存不能当成免费RAM；
O_DIRECT不支持即启动失败，不静默变成buffered I/O。记录字节、保存/恢复
时间、命中prefix、实际跳过prefill、总RAM/页缓存、其它请求等待。

## 有界验证与出口

必要构建后首测必须是真实tools/evalscope HTTP E2E。随后CPU存储/准入/
失效及直接GPU状态合同；通过后才进行性能矩阵。全部GPU阶段驱动持有
共享独占锁，竞争/监控失败ENV_ABORT，只停自身进程。

上限：生成HTTP 240次、服务启动14次、直接GPU fixture4次、GPU阶段总墙钟
21600秒（首次加载开始计）；最多2次具体失败修复，未用预算内重验受影响项，
不自动扩样。正式清单在开测前绑定源码/程序/head/输入、请求顺序和绝对截止。

必须覆盖两head：保存A→不同请求B复用同槽→A带增量恢复；迁移非零槽位；
接受0/1/2/3、suffix/神经切换、EOS/3token尾部、prefix不符、取消、重复
session、淘汰、截断/损坏/I/O失败。参考是独立保留的同一接受前缀全状态，
与fresh prefill的浮点shape差异另报告，不能以目标verify代替状态证明。

性能冻结目标：两head各自长主agent(128K)+3子代理(32/48/64K减输出预算)，
工具等待期间其它请求占槽后恢复，相比关闭缓存，包含首次保存及之后恢复
的整个两轮事务净耗时减少至少10%，恢复请求TTFT减少至少50%。重复代码/
文本分别报告，suffix收益旧证据复用，新增缓存路径须测净收益。五档
1024/4096/8192/45056/204800，每档3请求，cache关闭父候选同配置；保护
普通decode、TTFT、质量。短前缀不保存，保护成本仍实测。只作描述性
配对，不把3样本说成总体非劣；已确认可归因退化或关键合同失败不交付。
预算耗尽分别记证据有效性、测量完整性、统计结论、工程决定；通过才按
既有授权封存回退包、适用程序/元数据、健康、文档、提交推送。

已通过且未改动的suffix索引/子集映射、容量分配、32请求准入证据复用。
跨槽完整状态、终止draft更新和新HTTP缓存生命周期均不能用旧局部快照
合同代替。当前仅范围冻结，尚未构建/测试/交付。

## A阶段中间事实

09fe25ba零警告构建。首full HTTP 11/11及49 CPU存储合同通过；保存9227
token→槽位复用→恢复得到43，fresh参考也为43。取消实际已release；夹具
错误等待不存在的event=released，17请求（16成功1取消）后STOPPED，监控
无失败，263输入身份不变并封存。4项CPU回放修正为event=end released=1。
这是最多2次修复中的第1次，仅工具改变，程序SHA不变。子集18未执行请求
单独完成；full取消后恢复2请求并入原计划full性能服务末尾，不新增服务启动，
总240请求/14启动/21600秒上限及原绝对截止不变。旧17条不重跑。
