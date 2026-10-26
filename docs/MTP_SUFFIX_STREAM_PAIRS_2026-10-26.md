# 固定k3 suffix：流式GPU补测与交付决定

2026-10-26用户恢复目标后，监控修复的CPU阶段已通过，GPU补测仍不完整。
工程决定为**PERFORMANCE_NO_GO_UNCHANGED**：保留当前默认程序与发布元数据，
不部署研究候选；本次阶段归档不表示完整目标完成。

## 范围与身份

首阶段CPU修复见[日志流结果](MTP_SUFFIX_LOGSTREAM_2026-10-26.md)，已提交
`3def28c`。默认`build/q4t` SHA仍为
`f356b2c96330101abf1e5fa3fced495a37f4ea5a270ed4457037e0d55735d313`；
实际观测程序仍为`f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914`。
推理源码、固定k3/min32准入及65536 BF16子集产物没有改变。

每个GPU阶段事前冻结4启动/6HTTP、4200秒、单步900秒、0额外预热/参数扫描；
完整阶段分别持同一共享独占锁。顺序8K OFF→ON→200K ON→OFF，200K均带
固定8K前驱；自然继承文件缓存，不清缓存/改频率/绑核。具体依赖漏绑修复后
新身份仅一次验证，累计上限5启动/7HTTP；实际两阶段合计2启动/2HTTP，均8K OFF。
日志固定level1/window20/单线程/checksum，逻辑8GiB、压缩256MiB，固定工作区
12.25MiB/worker AS64MiB。cleanup独立60秒，其中流关闭最多10秒；原失败锁存。

受限GPU规则保留严格分析器和原资格拒绝，仅允许已冻结六条错误、采集与退出
两组完成标记及其他结构/关联合同通过时比较已记录区间。不证明完整trace或
零丢失，也不作为正常性能验收。新增错误必须停止，不能事后扩展白名单。

## 真实执行与缺陷

| 阶段 | CPU验证 | GPU实际 | 结果 |
|---|---|---|---|
| 日志流/清理 | 首构建零warning；44方法，2.378410秒；旧746MB往返61.565586秒 | 0 | CPU修复通过 |
| 流接入 | 20方法，0.325713秒 | 84.066596秒；1启动/1HTTP | 准备器遗漏/opt驱动，0导出 |
| 驱动继承/错误分类修复 | 16方法，0.213947秒 | 0 | CPU通过，未改历史失败 |
| 修复身份验证 | 8方法+1保存证据核对，0.213881秒 | 166.094081秒；1启动/1HTTP | 导出1次，资格INVALID，后3组未执行 |

第一次实际映射`/opt/nvidia/l4t-gpu-libs/openrm/libcuda.so.1.1`没有进入新计划。
其SHA `c052fd6522969504193195cb06b6f3044b4b11b16f9e227357ced12c9d45ac1d`
与历史ledger/机制计划一致；原因是prepare继承过滤器漏掉/opt根。修复保留
已有冻结SHA，在GPU前拒绝没有驱动的计划，实际映射缺失/变更传递ENV_ABORT。
旧原件仍为STOPPED/COMMAND_FAILED，终审另记ENV_ABORT_UNFROZEN_DEPENDENCY。

新身份实际驱动核对通过。首8K输入8170、输出256，输出SHA仍为
`66bb6d8c428668a991608e634c39eabebd85918b0cdd400c7b22919214ad436e`。
独立host计数74次T4 verify、148次neural T1、74次extend/252行、22次restore，
与冻结参考一致。报告及55MB SQLite导出正常，分析79.340343秒。

目标在收尾阶段新增四条错误，原始行和SHA保留于collector-rejection.json：

- `'session connection' shared pointer is still in use`；
- `ProfilerApi expired`；
- `Failed to handle Agent notification`；
- `2CommsProcessor@AsyncProcessor: Error occurred`。

这些超出事前六条模式，因此受限资格INVALID。strict仍FAIL，6381条缺少活动的
memset实参仍未知；没有跨进程填参。未发现显式loss标记不等于已证明零丢失，
可见的2716对CPU/NVTX和278842条GPU活动也不能补齐未运行的OFF/ON对照。
不根据单个8K的局部时间作200K/8K退化归因或运行时修改。

## 生命周期与资源

两阶段监控分别85/85、167/167次落盘，无监控失败，pending0；模型、launcher、
shutdown/worker正常退出，未强杀。监控记录的11/13个进程身份在终审时均ABSENT，
共享锁已释放。轮询不能排除短于采样间隔的干扰。

两个真实流分别746099822→30482789字节、746073161→30597560字节，EOF/尾帧/
fsync/逻辑及压缩SHA闭合；后一个经完整逐行解码。此为采集字节保留证据，
不是Nsight事件完整性证明。worker报告maxRSS52676/52884KiB，固定工作区不是
全部RSS，也不是统一RAM增量。全机MemTotal−MemAvailable峰值分别
113639669760/113441050624字节，swap峰值2074869760/2147479552字节；PSI三源
不可用。保留原值，不把分配量相加，不用采集期RAM作为正常部署验收。

## 证据与决定

| 字段 | 结果 |
|---|---|
| 证据保存完整性 | 两次终审PASS；失败原件保持 |
| 测量有效性 | 前轮ENV_ABORT_UNFROZEN_DEPENDENCY；新轮INVALID_COLLECTOR_DIAGNOSTICS |
| 测量完整性 | 新GPU有效配对0；200K和8K ON未执行 |
| 统计结论 | NOT_EVALUATED_INCOMPLETE_PAIRS |
| 工程决定 | PERFORMANCE_NO_GO_UNCHANGED；不替换默认，完整目标未完成 |

原250 HTTP/18启动五档已经测齐，保护项未通过，不能写成“五档尚未测”。
两head重复收益、直接合同和22/22质量证据依身份复用；最新固定重复场景收益
完整head +14.028%/+14.607%，子集head +6.948%/+6.448%，不以此抵消其他保护项。
完整边界继续以[原完成矩阵](evidence/mtp-suffix-contract-20261026/completion-matrix.md)为准。

前轮522文件75316058字节独立归档，manifest
`0b0b7e1fd9d357c76c6d5e93fb9cdbba33dbdeda0dd4f02567ea938043f9d136`；
新轮144文件135503494字节，manifest
`fa15db5e75213d8b03e993cf5739e8b4d23fc779812c42e38982dba847808dcb`。
它们是依赖原路径/历史工具包的增量研究归档，不是自包含发布包。
[证据索引](evidence/mtp-suffix-stream-pairs-20261026/index.json)列出收据SHA；原始
数据分别位于`.q4t-work/suffix-stream-pairs-20261026/`与
`.q4t-work/suffix-driver-pairs-20261026/`。本轮不再采样；下一步只能研究上述
具体收尾错误，不能直接扩展豁免来取得通过。
