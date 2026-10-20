# Thor GDS最小实验与C128历史性能核对（2026-10-21）

用户明确要求在官方资格评估之后进行最小实机实验。本文新增的
授权覆盖有界cuFile能力/正确性验证；不把前一报告“暂不实施”当成
阻止实验的条件。默认程序不变，SSD唯一容量后备合同保持。

## 冻结范围

目标是测清本机栈能否完成SSD文件→device缓冲读取、读取字节是否
准确，以及cuFile报告原生P2P/NVFS还是POSIX路径。没有模型性能
候选，不声称优化；不以这个实验代替HTTP E2E验收。

- 单一GPU lease持有完整驱动；原NVML 5秒时限、1秒轮询保持。
- 最多4个探针进程，各最多一次cuFileRead，无重试；每个30秒超时。
  不执行gdsio、额外gdscheck、模型加载或HTTP采样。
- 三个固定模式：compat=false/P2PDMA=false、compat=false且请求
  P2PDMA=true、compat=true/P2PDMA=false。最后最多增加一个真实
  非对齐请求，采用前三项首个完整成功模式；没有成功则不执行。
- 选layer0专家0的权重范围：原始offset 39,525,408、length
  2,457,600 B；4KiB覆盖窗口offset 39,522,304、length 2,461,696 B。
  同一只读模型文件，未清缓存或预热；CPU真值pread在cuFile之后。
- 每次读取上限8MiB；device输出最多8MiB，两个host校验buffer共
  最多16MiB。cuFile GPU slab请求10MiB、POSIX slab请求约10.016MiB，
  另计CUDA context、库元数据、文件缓存和OS；不是整机硬上限。
- 配置通过实验进程的`CUFILE_ENV_PATH_JSON`指定，保存实际生效值。
  不修改/etc、驱动、内核、挂载、模型或默认程序，不安装模块。

`P2PDMA=true`只是对该路径的请求。ARM、GPU能力、驱动与库可能
不接受它；必须记录返回值和有效配置，不能把请求当支持证明。

## 判定与停止条件

每项保存API返回值、真实读取长度、D2H后逐字节对照、清理结果、
cuFile L3读取路径计数和/proc/self/io读取前后增量。统计缺失保留
unknown；`n_posix_reads>0`不能称纯原生；P2P/NVFS均0也不能仅因
API成功判原生。这里的库计数与进程I/O不替代完整专家SSD归属。

预期的不支持错误是可用性实验结果，可以继续固定的独立对照。
数据不一致、CUDA执行失败、超时、清理失败、身份变化或lease
ENV_ABORT停止剩余项；未知运行错误也停止，不盲试驱动选项。
单次耗时只作诊断记录，不比较热/冷样本宣称提速或性能非劣。

入口为`tools/verify/gds/probe.cpp`与`run.py`。构建与全部产物置于
`.q4t-work/gds-minimal-20261021/`，完整驱动通过既有
`tools/evalscope/gpu_exclusive.py`运行。只对实际工具缺陷修正，
已取得的失败保留，不能为取得成功无限补测。

## 结果

**实验完成；当前栈原生GDS未跑通，兼容路径的两次字节校验通过。**
这是可用性结论，不是优化或整模型验收。实际4个进程、2次cuFileRead，
未重试；单一lease正常完成，全部进程报告清理成功。

| 模式 | 结果 | 实际路径依据 |
|---|---|---|
| strict-current：禁止兼容、P2PDMA关闭 | DriverOpen=5001，未尝试读专家payload | 缺少nvidia-fs，不能开启严格路径 |
| strict-request-p2pdma：禁止兼容、请求P2PDMA | DriverOpen=5001，未尝试读专家payload | 库内部P2P设备地址探测返回801/CUDA_ERROR_NOT_SUPPORTED，随后无可用NVFS路径 |
| compat-control：允许兼容，对齐窗口 | 2,461,696 B逐字节一致 | L3增量POSIX=1、P2P=0、NVFS=0 |
| compat-control-unaligned：实际expert范围 | 2,457,600 B逐字节一致 | L3增量POSIX=1、P2P=0、NVFS=0、unaligned=1 |

两成功项的有效配置均为allow_compat=true、force_compat=false，NVMe
P2P flags=0。库日志明确将Thor判为不支持GDS，报告BAR1=0和GDR
unsupported。独立设备属性为`DMA_BUF_SUPPORTED=0`、
`HOST_ALLOC_DMA_BUF_SUPPORTED=1`，说明device与host导出能力不同。
这不证明host导出已经接入SSD直达，也不等于全设备DMA能力为零。

两次cuFileRead墙钟分别2.151ms和1.518ms，仅是诊断观察，**不计算
带宽收益或对齐优劣**：前次对齐读取后有CPU真值pread，后次继承其
缓存。紧邻cuFileRead的进程read_bytes增量分别2,461,696 B和0，
后者显示“cuFile逻辑读成功”不保证再次从SSD供给。该计数仍有进程
归属限制，不能据此宣布专家完整SSD流量已闭合。

### 实验暴露的库缓冲预算差异

请求GPU slab为10MiB，但成功项getter报告393,216KiB，库日志逐项
初始化`[1024,8192,16384,65536]KiB × [128,8,4,2]`，对应默认384MiB
配置。因此**GPU内部池10MiB限制没有得到证实，不能签资源预算通过**。
这也不证明384MiB已经全部分配；L3没有实际池占用字段。

POSIX slab日志确认`[4,1024,4096]KiB × [4,2,2]`生效，总配置10MiB
+16KiB，两成功项各记录分配一个4MiB POSIX buffer。库请求配置、
生效配置与真实物理占用必须分开。原始冻结文件保留，本次不为修正
库配置再开一轮GPU；后续若复用探针，须先处理这一配置验证缺口。

这个限制不改变已有字节一致和POSIX路径的实测结论，但禁止从此次
实验推导内存节省、严格池上限或GDS性能收益。

## 工程决定与其他缺口评估

当前本机栈的原生GDS验证结果为未通过；兼容cuFile已经验证能读，但
实际仍走POSIX。没有证据支持为了“GPU直读”引入这层依赖，不更换
默认读取器或程序。保留实验代码和证据，未来平台栈变化再定新范围。

| 问题 | 当前评估与建议顺序 |
|---|---|
| C128吞吐如何代表SSD能力 | 先明确基线配置。现有L2=1/单加载线程短请求是资源边界验证，不能当正常性能或持续冷SSD基线 |
| 总RAM与文件页缓存 | 优先。旧C128运行时专家页缓存观测约58.88GB，64GiB scope不是整机限额；先收敛持续工作集预算与归属 |
| 专家实际SSD读取 | 优先。应用逻辑读与物理读仍需分开，今回兼容非对齐读的零read_bytes正是可观测反例 |
| 原始文件offset不对齐 | 若研究O_DIRECT等有界读取，使用对齐覆盖窗口并切出有效范围；不能改只读原模型。覆盖范围的准确字节已在本实验核对 |
| CPU scale重排与重复复制 | 在供给/内存瓶颈明确后评估。可考虑GPU布局转换或共享可访问buffer；二者仍占统一RAM，必须维护数值和生命周期 |
| cuFile内部缓冲配置 | 本实验新增的具体缺口，后续使用cuFile前验证有效配置，不能信任JSON请求值；不作为当前默认程序故障 |

这是对后续工作的评估，不启动新的优化或性能矩阵。性能方案仍按
HTTP E2E优先与适用完整对照；CPU缓存不会因原生GDS未通过而重新
成为容量卸载方案。

## C128已有prefill/decode数据

有记录，但都是启动可靠性验收中的短输出质量请求，**未找到C128
正式五档性能矩阵**。以下从原始SQLite和服务请求阶段日志复核，
全部1024输入、7输出、普通decode、实际并发1。

| 版本与请求 | prefill阶段秒 | 阶段有效输入tok/s | 客户端TTFT秒 | decode近似tok/s |
|---|---:|---:|---:|---:|
| 正式5a，boundary-v2首请求 | 55.959 | 18.299 | 55.963 | 5.479 |
| 正式5a，同实例第二请求 | 26.578 | 38.528 | 26.582 | 6.001 |
| 旧研究1d66，boundary-lock-v3 | 32.433 | 31.573 | 32.437 | 5.780 |

prefill是服务`decode-entry − prefill-entry`的阶段墙钟跨度，含专家
供给、调度、计算和读回；不是纯GPU kernel耗时。有效输入速率为
1024/该跨度。decode近似为`(7−1)/(HTTP总延迟−TTFT)`，只有六个
计数间隔且含结束开销，不是精确首末token时间或长输出吞吐。

共同配置为C128、max_seq=2/max_prefill=2048/max_len=8192、L2=1、
mirror=0、load_threads=1、no-MTP。max_seq=2是容量，不是双并发。
新版本第一条在冷payload启动门之后执行，第二条复用同一实例；
运行中仍有大量文件页缓存，不能称持续冷SSD。旧版本使用完整热门
夹具且含研究逐调用日志，不能混成版本性能对照或据此计算回归。

检索了文档及3750份候选命令JSON，C128实际请求落在上述两阶段；
boundary-v1为准备失败、0模型/0HTTP。旧L2=128不等于GPU C128。
这是已检查范围内的结论，不保证不存在未索引文件。
原始证据、公式和SHA见
[C128历史数据](evidence/gds-minimal-20261021/c128-history.json)。

## 验证与交付

独立探针`-Wall -Wextra -Werror`构建零警告；Python停止门11项与
自身子进程超时回收检查通过。单一GPU lease中10次NVML全部成功，
最长0.068984秒，原5秒/1秒保护未改。记录的守卫、驱动、探针和查询
进程均已退出，未建立模型服务或systemd scope。

原始结果位于`.q4t-work/gds-minimal-20261021/run-v1/`，29个阶段
文件逐份复核；lease在同根`lease-v1/`。`frozen-v1/`保留执行前源
代码及方案，`build/`保留实际构建命令、输入库身份和探针。
二进制SHA256为
`3cbbba077bd436c648bb1027e56963c02d9aee9dd33383c5e1ae4a8c81ec471e`。
实际maps中的libcufile1.18、libcudart13.3和libcuda与构建前身份一致。

独立运行复核和路径/缓冲复核分别位于`independent-run-review-v1/`
与`path-review-v1/`；CPU停止门在`runner-host-review-v1/`。
仓库保留[实机结果与证据摘要](evidence/gds-minimal-20261021/result.json)，
其中绑定完整原始/派生文件SHA。未测项目不补作通过。

默认5a、发布元数据及/etc/cufile.json摘要前后保持，模型stat身份
保持；无驱动/模块/挂载配置修改。工具不链接正式q4t，不替换默认，
不追加GPU或HTTP采样；本轮作为有明确负面结果的能力研究收尾。
