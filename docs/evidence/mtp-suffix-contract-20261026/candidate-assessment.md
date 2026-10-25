# Nsight 单一固定版本候选评估（2026-10-26）

结论：现有官方证据足以支持**提出一个固定的 Nsight Systems 2026.5.1 工具依赖资格候选**，还不能认定它修复了本项目的采集失败。当前不是没有可检验路线的僵局；可检验路线是先核定平台与完整软件包身份，再对单一候选冻结一个新的有界工具资格阶段。上一阶段预算已经终止，这份评估不授权下载、运行工具或增加 GPU 测量。

本次仅阅读 `web/manifest.json` 与该 manifest 中四个已保存页面，共五个相关输入；没有执行已安装的 2026.3.1，没有执行 profiler、导出、测试、模型、HTTP 或硬件采样。下述本机行为沿用已完成的源码/收据审查，本次不重读原始数据。

## 官方证据能支持什么

- 2026.5.1 release notes 明确列出 multi-process capture ranges 在经 `cudaProfilerStop()` 结束时的 CUPTI buffer flushing 改进。这是与当前问题类别相关的、具体的版本变更，不是泛泛的“新版可能更好”。但该条目没有把本项目的 StopRequest rc=1、单 CUDA 进程警告或 6,381 个 memset 缺口列为已修复项。[NVIDIA release notes](https://docs.nvidia.com/nsight-systems/ReleaseNotes/index.html)
- NVIDIA support 350407 中，多 CUDA 进程训练出现与旧报告相同的两条警告；维护者用 DTSP-21073 记录非调用 `cudaProfilerStop()` 的其他进程未充分 flush 的问题，并建议手动停止采集；用户随后报告该 workaround 能采全四个 GPU。它提供了相关机制与负面症状，不能把相同警告字符串当成相同根因，也没有给出该 ticket 与 2026.5.1 条目的明确对应关系。[NVIDIA support discussion](https://forums.developer.nvidia.com/t/question-when-prifilling-megatron-lm/350407)
- 安装指南一般区分 Tegra Embedded 与 Arm SBSA Workstation 包；JetPack 官方页又明确说明 Thor 基于 SBSA stack，并要求采用 SBSA installer。因此“当前 2026.1.3 是 SBSA 包”本身不构成装错平台的证据。仍须核对**具体包、目标组件和当前软件栈**的支持范围。[Installation guide](https://docs.nvidia.com/nsight-systems/InstallationGuide/index.html)；[JetPack / Thor](https://developer.nvidia.com/embedded/jetpack)

## 与本机失败的差异

本项目 direct fixture 只有一个执行 CUDA 的应用进程；wrapper fixture 的 Python wrapper 也不执行 CUDA。Nsight 自己的 controller/launcher/daemon 不能因此算成多个 CUDA 应用 producer。旧 direct-stop 与 wrapper-stop 在已知工作量闭合后仍有实际 CUDA target 警告：这不同于 support case 中“调用 Stop 的进程成功、其他 CUDA 进程丢失”的直接解释。

本轮 direct-interactive 的 32 个 fixture step 和 ready 收据存在，随后外部 stop rc=1、shutdown rc=1；runner 进入强制清理并停止阶段。报告文件保留，但本轮没有 SQLite export、known-work 审计或 warning 审查，不能预先称其“事件完整但只有警告”。launcher 143、被收养 nsys-launcher 的 -15 均不是实际 fixture 的精确退出码。以上结果证明冻结控制合同未通过，尚未证明故障来自 2026.1.3 的哪一处实现。

一个新版本可以检验相关工具缺陷是否消失，但不得同时把命令选项、停止顺序、被测程序、CUDA/驱动和内存覆盖判定一起改动后归功于版本。若新版本官方 CLI 契约要求调整命令，应把具体差异单独列入候选合同，明确这属于新工具生命周期资格验证，而非单因素性能比较。

## 下载和测试之前的门槛

1. **先选一个完整身份。** 候选为 2026.5.1 的一个确定构建与 Arm SBSA 目标软件包；核对官方来源、完整版本/构建号、架构、发行平台、包清单及可取得的官方签名/摘要。动态 release-notes URL 不是下载物身份；下载后再冻结本地 SHA。已有 2026.3.1 不作为顺手插入的另一轮实验。
2. **确认兼容范围。** 用已有主机/驱动/CUDA 身份记录核对该候选对 Thor / SM110a、当前内核及 CUDA 13.3 栈的适用性；区分采集端、注入库、CUPTI 和导入器版本。通用“CUDA 10+”文字不能单独证明当前组合完全支持。若只能找到不匹配的软件包或必须更新驱动/CUDA才能使用，停止该候选资格准备，另定范围。
3. **隔离依赖。** 下载/解包只进入新的 `.q4t-work/` 目录，使用绝对路径，保留当前工具、默认程序、release metadata 和两种 head 产物；不覆盖系统包、不运行安装维护脚本、不改变全局 PATH。冻结实际执行文件与被加载的 target/collector/importer 库，防止新 CLI 混用旧注入库。许可处理按实际包要求记录。
4. **另行冻结有界资格方案。** 在新 GPU 阶段前明确总墙钟、两种 fixture 路径、请求/启动数、锁、退出与清理合同、唯一命令配置、日志容量和停止条件。必要的离线 CLI/schema 适配与纯 CPU 合同先统一验证，再冻结；任何失败不自动换版本、改选项或扩样。以前的 32 个 CPU mocks 证明 runner 逻辑边界，不能代替新 collector 的真实行为。
5. **collector 先过门，模型才有条件启动。** 新工具的 direct/wrapper fixture 均须重新取得实际 target 身份、32 kernel / 64 copy / 32 memset / 33 NVTX 的双向覆盖、CPU 输出校验、负控拒绝、实际 target warning 零容忍、报告生成及真实目标退出/清理证据。旧 controls 可以作固定历史对照，不能当作新工具资格已通过；wrapper 的精确零 CUDA/NVTX 例外仍须原规则证明。通过后才按新冻结预算运行子集 8K/200K GPU 对照，不自动复测未改变身份的普通吞吐收益。

## 仍然独立存在的 memset 门槛

旧 model trace 的 6,381 个成功 `cudaMemsetAsync` API 没有对应已识别活动，仍为 `UNKNOWN_CAPABILITY`。其中 verify 6,216=74×84 的规律仅提供定位线索，缺少 API 参数/操作语义，不能据此宣布是合法 CPU 路径或真实丢记录。固定 fixture 的 512/16,384 bytes memset 即使在新工具中全部覆盖，也只证明这两种 fixture 操作。

每份新模型 trace 必须重新完成同进程相关 ID 的 API→GPU 与 GPU→API 双向核对；任何新增字段、合法无 GPU activity 情形或工具线程排除，均需独立可验证的语义与负控，不能为了过门加入猜测白名单。若新 collector 正常退出但模型缺口仍存在，停止 GPU 归因，保留未知能力结论；不能以 wall−thread CPU 代替 GPU busy，也不能用“新版”或“fixture 通过”补齐缺失覆盖。

两种 head 的已通过普通重复场景收益，在程序、head、输入与执行依赖未变的前提下仍可复用。本候选评估只给工具资格路线，不新增 suffix 接受结论，不改变准入规则，不部署或替换交付程序。

## 保存的来源身份

页面抓取时刻 `2026-10-25T17:00:25.240029+00:00`；本次使用的 URL、长度和 SHA 以同目录 `web/manifest.json` 为准。四页面 SHA：

- release notes：`61d434ad650fd0d622e56b42504762e2b0e6c7dcc1b7888f12b6f29340546adc`
- installation：`af7b3b1c22ca150e927e574f306d2fefec361dfdffb162dc73f81d24e3a2663c`
- support 350407：`b5313d67100fb68fa77822d3002d735ddc32743767c1cb9e4cb1ebf869b1009b`
- JetPack：`054b6dee9ad6d00497f89ca048c91805101362e15af311ce99deef97908f6b9c`
