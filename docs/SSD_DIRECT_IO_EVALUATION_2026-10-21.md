# NVIDIA SSD直读与本项目的适用性（2026-10-21）

结论：**原生GPUDirect Storage（GDS/cuFile）当前不作为Thor项目的
实施主线**。按公开支持矩阵，Jetson Thor不在原生GDS受支持GPU范围；
本机也没有直达路径的实测证据。减少SSD读取中的重复缓冲、页缓存
占用和内存复制有研究价值，但不需要以GDS为前置，不能转成CPU容量
卸载。统一内存边界遵循[SSD offload合同](SSD_OFFLOAD_CONTRACT.md)。

本轮为文档审计、官方资料核对及本机只读盘点，不是性能原型验收。
未初始化CUDA/NVML/cuFile，未运行gdscheck/gdsio或模型，未更改驱动、
挂载、cuFile配置或默认程序；不作新性能、容量或运行健康结论。

## “SSD直接读取”指什么

本次按NVIDIA GPUDirect Storage/cuFile评估：受支持组合中，存储DMA
可把数据送到GPU缓冲，减少CPU bounce buffer及复制。CPU仍可发起和
管理I/O，不意味着每个GPU线程直接发SSD请求。cuFile也有使用POSIX/
CPU中转的兼容路径，API调用成功不能证明走了原生GDS。
见[NVIDIA概览](https://docs.nvidia.com/gpudirect-storage/overview-guide/index.html)。

普通`O_DIRECT`、io_uring、GPUDirect RDMA及相机输入是不同接口。
O_DIRECT可绕过文件页缓存，但不自动把数据送到GPU最终计算缓冲；
注册io_uring host buffer也不是GDS。不要把它们的支持情况相互代用。

## 官方资格与本机事实分开判断

查询日期2026-10-21。CUDA语义使用与本机一致的13.3归档；GDS支持
矩阵页面当时更新于2026-09-17，包含新于本机1.18的发行说明。

| 官方资料 | 可确认范围 |
|---|---|
| [GDS支持矩阵](https://docs.nvidia.com/gpudirect-storage/release-notes/index.html#support-matrix) | 原生GDS支持Data Center及Quadro类别，其他类别仅列compatibility mode |
| [矩阵链接的NVIDIA GPU分类](https://developer.nvidia.com/cuda/gpus) | T5000/T4000列在Jetson类别；因此按该矩阵Thor不在原生GDS支持范围，未找到Thor例外 |
| [CUDA 13.3组件表](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-toolkit-release-notes/index.html) | cuFile 1.18.0.66与nvidia_fs 2.29.4有arm64-sbsa包；包存在不认证具体GPU/内核路径 |
| [Jetson Linux 38.2发布说明](https://docs.nvidia.com/jetson/archives/r38.2/ReleaseNotes/Jetson_Linux_Release_Notes_r38.2.pdf) | Thor对齐SBSA并使用OpenRM；不能笼统说“Thor不是SBSA”，也不能把SBSA包当所有子系统均支持 |
| [CUDA for Tegra 13.3](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-for-tegra-appnote/index.html#memory-management) | host/device/managed共用SoC DRAM，Thor具备sysmem full coherency；访问方式有区别，但CPU不是第二容量池 |

这是**已发布支持资格**的判断，不是证明硬件绝不可能实现某种直接
DMA。文档明确DGX Spark仅compat也只能作为平台差异的例子，不能
直接替Thor下硬件结论。实际兼容模式能否在本机工作本轮也未验证。

本机盘点来自只读包、模块、sysfs、挂载及配置查询：

| 项目 | 实际观察 |
|---|---|
| 系统 | L4T R39.2.1、Ubuntu 24.04.5、kernel 6.8.12-1021-tegra |
| 驱动 / CUDA / cuFile | 实际加载nvidia 595.78；默认CUDA 13.3.0；libcufile 1.18.0.66-1 |
| JetPack名称 | 未安装可识别的nvidia-jetpack元包，不从L4T猜营销版本 |
| GDS工具 | `/usr/local/cuda-13.3/gds/tools`下已安装，未执行 |
| nvidia_fs | 未加载；当前内核modinfo、模块与设备节点未发现 |
| 当前cuFile配置 | `use_pci_p2pdma=false`、`allow_compat_mode=true` |
| 模型文件系统 | 本地ext4，`/dev/nvme0n1p1`；未验证全部GDS挂载要求 |
| SSD | WD PC SN5000S，PCIe 16 GT/s ×4；不是实测带宽 |
| 拓扑 | GPU在0000 PCI域，SSD在0005域；静态拓扑不能认证直达路由 |

CUDA `version.json`里的driver 610.43.02、nvidia_fs 2.29.4是工具包
组件记录，不是实际加载版本。**缺少nvidia_fs单独不能判死**：官方
概览说明部分新版受支持NVMe路径无需它；这不构成本机可用证据，
也不要求为本轮评估安装模块或改变系统。

Thor上的旧Tegra nv-p2p与新的DMA-BUF能力必须区分，host/device分配
的导出能力也不同；旧RDMA属性不能直接代替GDS支持门。见
[CUDA 13.3 RDMA说明](https://docs.nvidia.com/cuda/archive/13.3.0/gpudirect-rdma/index.html)。

## 当前模型读取的具体适配成本

`src/io/safetensors.cpp:113/253`使用只读buffered fd与pread；专家
路径`src/quant/moe_residency.cpp:719`读权重、原始scale和标量，
`:841`在CPU合并gate/up scale并swizzle，`:1269`起分别H2D，
`:1294`保留GEMM需要的host标量，`:1299`起用事件保护源缓冲。

只读抽取两个真实分片header，共256个专家；显式读取404,328字节
header，未主动读权重payload，不排除内核header预读。范围为
layer0专家0–127和layer47专家384–511，不能外推全模型布局一致。

| 单expert文件区域 | 两样本中的长度 | layer0 / layer47绝对offset mod4096 |
|---|---:|---:|
| down/gate/up三块权重连续区 | 2,457,600 B | 3104 / 1864 |
| 三块原始scale连续区 | 307,200 B | 3104 / 1864 |
| 六个float标量 | 24 B | 随expert变化，均非完整4KiB请求 |

权重及scale长度都是4KiB整倍，但绝对文件offset包含safetensors
header，256个样本均不满足4KiB对齐，甚至不满足512字节对齐。
设备目标还分为down与gate/up等区域，不能把整段直接放到任意槽
并忽略最终布局。**用cuFileRead替换pread不足以完成适配**：

- 需要对齐窗口、额外device临时区/拷贝，或另行批准的可再生SSD
  布局副本；原模型目录始终只读，当前不创建全量转换副本。
- 大权重可与小scale/标量分开设计；CPU swizzle保留就仍有host
  路径，迁移到GPU则须验证精确字节布局与数值合同。
- 新I/O完成、错误/短读、取消、slot所有权与GPU消费顺序必须闭合；
  现有H2D事件不能自动代表另一条I/O已经完成。

官方说明非对齐读可能使用内部GPU bounce buffer再D2D，故“支持
非对齐”也不是“直达最终buffer/零额外内存”。见
[GDS非对齐I/O说明](https://docs.nvidia.com/gpudirect-storage/best-practices-guide/index.html#io-pattern-4)。

PLE是另一条路径：`src/ple/page_reader.cpp:187/279/339`将160字节
行拆成4KiB页并去重、io_uring读host页池、CPU scatter；
`src/ple/ple_embedding.cpp:173`后H2D并在GPU转换FP8。
若研究直读，PLE还需要device gather/scatter设计，不能共用一个
“换读取API”结论。若未来GDS资格成立，MoE大权重块是更集中的
首个适配对象；这是工程推断，尚无性能测量支持。

## 对项目的价值与优先级

从统一DRAM和当前多副本路径推断，值得争取的是减少页缓存副本、
host/device重复缓冲、内存复制与CPU开销。Thor没有独立显卡那条
host DRAM→独立VRAM搬运，不能套用其GDS收益数字；GDS也不扩大
RAM、消除scale处理或提高SSD本身随机读取能力。热页缓存命中可能
比真正读SSD更快，所以两种方案必须在相同资源条件下比较。

| 方向 | 当前工程决定 |
|---|---|
| 原生GDS/cuFile接入 | 暂不实施。当前官方支持门不满足；不为尝试它升级驱动或加载模块，不成为SSD主线阻塞项 |
| cuFile兼容路径 | 不是SSD→GPU直达成果；缺少相对现有pread的具体优势，不为API统一而接入 |
| 有界SSD读取工作集 | 优先。先把device槽、host中转、文件缓存和真实SSD读取归属放在同一资源合同下 |
| O_DIRECT或其他有界读取方式 | 可在上述合同下研究，处理文件/缓冲对齐与CPU scale转换，避免额外大页缓存；并非现有实现或已证明收益 |
| Thor共享可访问buffer减少复制 | 有限后续研究方向；仍占同一DRAM，必须满足GEMM布局/访问能力/生命周期，不是CPU offload |
| 共享主机专家缓存 | 已搁置的历史研究；不因GDS暂不可用而恢复为替代容量方案 |

建议下一阶段先收敛**SSD容量与I/O合同的具体缺口**，再为有界读取
原型冻结预算和出口。无需等GDS支持才能推进，不用额外盲测证明一项
目前没有平台支持承诺的功能。

只有出现明确Thor支持资料或针对本机栈的有效资格依据，才重新评估
原生GDS。届时先核对实际路径和内存成本，再决定是否实现性能候选：

- `allow_compat_mode=false`可关闭通用兼容回退，但不是所有I/O直达
  的充分证明；官方列有覆盖它的文件系统设置。这里并非本机使用
  Lustre，而是不能把一个开关当全局证明。见
  [cuFile兼容模式](https://docs.nvidia.com/gpudirect-storage/api-reference-guide/index.html#compatibility-mode)。
- 结合有效配置、每类I/O的`posix`/`unalign`计数及GPU/POSIX缓冲池
  统计判断路径，并计入内部缓冲，不只看API成功。见
  [官方统计字段](https://docs.nvidia.com/gpudirect-storage/configuration-guide/index.html)。
- 设备能力诊断和任何GPU操作遵守统一lease；实际性能候选的首测
  仍为HTTP E2E，按[EVALUATION](EVALUATION.md)完成适用对照，不用
  gdsio/微测作为优化接受。未知或预算耗尽时保留结论，不自动扩测。

## 可核验记录

原始只读盘点：`.q4t-work/ssd-direct-evaluation-20261021/local-io-v1/`。
19条allowlist命令、header偏移、来源代码SHA与采集脚本由seal绑定；
seal SHA256：
`a110b3e2cfea6d7bcc08c69fd600e1d72bad3a9edb9987b8e8627a3d30d81699`。

派生完整报告：同根`local-review-v1/summary.json`，SHA256：
`5a5a63e33cfb2132aca8a0738d473eb88cec364b94a0e5be602148d8e19f8972`。
仓库保留[精简盘点与封存复核](evidence/ssd-direct-evaluation-20261021/inventory.json)。
本轮只修正文档，默认5a456351及其发布/回退材料保持原身份；既有
启动可靠性通过不提升为SSD低RAM或GDS性能通过。
