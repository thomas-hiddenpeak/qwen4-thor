# 固定k3 suffix观测：日志流与清理缺陷修复

入口81cd789。上轮真实8K NVLOG746229179字节超256MiB，同时阻止shutdown
spawn；压缩前两必要逐事件logger占99.64%。本阶段仅修具体工具缺陷，
不修改f0ff运行时、suffix准入或草稿head，默认f356与发布元数据保持。

scope `.q4t-work/suffix-logstream-20261026/scope.json` SHA
`e4598380c40669996f719d857c20f1ab6f89c0d9facd4d9fda6894b66bc07c5a`。
整体3600秒；0模型/HTTP/GPU；native最多2次构建、合计180秒，第二次仅
具体首构建失败修复。CPU≤52方法、合计180秒、最多2suite，第二次仅
具体失败合同修复；通过不重跑。一次固定旧NVLOG上下文提取≤30秒，
一次固定完整压缩/解压往返≤120秒，终审归档≤180秒。源与原件只读，
新构建/证据只进`.q4t-work/`，不扫描压缩参数或扩大容量到通过。

唯一日志候选：zstd level1/windowLog20/nbWorkers0/checksum开启、无字典，
完整字节流保留，逻辑上限8GiB、压缩上限256MiB。native固定工作区与
I/O缓冲≤16MiB；单独RLIMIT_AS64MiB及实际RSS，不将工作区冒充全部统一
RAM。FIFO受管reader先于writer，keeper保护writer间歇，身份与EOF/尾帧
显式核对；单帧校验、逻辑SHA/字节与压缩SHA闭合。容量/路径/解码失败
保留部分原件、拒绝资格。逐行解析≤1MiB/行，thread/session和保留证据
有界，不把多GB日志整体读入bytes/list。CPU模拟不证明真实NVLOG支持
FIFO，未进行任何新的Nsight资格或GPU验收。

清理候选只修改现阶段driver：首个测量失败锁存，正常report绑定与
cleanup分开；cleanup总额60秒、shutdown一次kill=none，目标/owner/
祖先关系/exe/start_ticks/pidfd身份门保持。测量超限不能再次阻止清理
启动，清理成功不能擦掉原失败；失败后禁止export、资格分析及下一臂。

6条额外错误完整保留：4条辅助activity context-disable，2条报告后
MemoryStorage析构且打印分配/返回相等。公开枚举和时间顺序仅能帮助
分类，不能证明零丢失/无副作用；本阶段不放宽原资格门。任何新GPU
阶段须先明确受影响的证据门和剩余机制问题，不直接重试旧四臂。

已通过且依赖未变的两head重复收益、直接合同、质量及原250请求完整
五档原值/NO_GO明确复用。本阶段成功也不构成性能接受或完整交付。

## 2026-10-26恢复后的结果

显式暂停区间在`resume.json`单独记账，旧scope不改；此前活动时间按
541.308580秒保守扣除，恢复后剩余3058.691420秒，不重置构建/测试次数。

首构建1.696516秒、零warning。44项CPU首suite通过，驱动总2.378410秒，
434项冻结身份不变，无临时目录或自有子进程残留。一次固定上下文扫描
2.349981秒；唯一完整往返61.565586秒，其中编码及完成收据1.272450秒。
746229179字节/2066081行逻辑SHA与原件一致；压缩30205327字节，24.7052倍。
worker固定工作区12845056字节，地址空间限67108864字节，maxRSS21376KiB，
FIFO内核容量65536字节；这些是CPU工具观测，不是模型全机RAM收益。

6E仍为UNRESOLVED并阻资格：4条辅助activity context-disable错误，2条
SaveReport之后MemoryStorage计数相等。错误分类不等于无丢失证明。
严格分析器及推理程序未变；真实NVLOG对FIFO的兼容性尚未验证。

终审1596检查通过，801文件/47668236字节独立复制；manifest
`7d1172a82fcad805d568d26cba60055aa9063a4df801fc96de91caff0cf09e7b`。
归档不是含模型/所有历史证据的自包含发布包，继续保留原路径和历史锚点。

| 字段 | 结论 |
|---|---|
| 证据有效性 | VALID_CPU_TOOL_REPAIR_ONLY |
| 测量完整性 | 本CPU阶段完整；新GPU对照未运行 |
| 统计结论 | 不适用CPU工具缺陷修复；没有正常性能主张 |
| 工程决定 | CPU修复接受；真实采集接入待验证，原性能NO_GO保持 |

[小型证据索引](evidence/mtp-suffix-logstream-20261026/index.json)；完整原件在
`.q4t-work/suffix-logstream-20261026/`。下一GPU阶段须先接入有界流并冻结
自己的实际兼容性/错误处置边界，不能直接重复已失败的四臂驱动。
