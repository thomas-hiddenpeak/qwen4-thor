# 自动恢复生产接入与 CPU 证据

2026-11-08。承接 [A/B 边界](SERVE_SELF_RECOVERY_AB_2026-11-07.md)。
本页保留生产控制器和封装的 CPU 阶段及真实补验过程。最终已完成
[A/B 有界接受与交付](SERVE_SELF_RECOVERY_RESULT_2026-11-08.md)；下列
候选/待授权表述是当时状态，不替代末节及最终结果。

## 已实现

`tools/serve_recovery/service.py prepare` 将已接受原生引擎、独立
Guardian、独立 CUDA 小探测、nginx 与固定 Python 来源封装。模型
目录只读；每个元数据/权重/PLE 文件记录 dev/inode/大小/时间身份，
恢复前只读两端可用性探测，不把它说成全部权重重算摘要。原生构建
回执固定所需源文件清单及两个 ELF 摘要，封装验证精确来源与启动器。

包内 `bin/q4t service` 提供 `init/run/start --daemon/status/stop`。
`init` 仅创建 STOPPED 持久身份；`run` 前台随启动父退出停止，显式
后台等待完整业务 ready 并握手。停止失败不能谎报清理完成；缺少
活 Guardian 的 status 报 UNKNOWN，不读取旧 READY 文件当当前事实。
这是新包入口，现有 `build/q4t serve` 与旧封存包含义保持。

真实控制器使用同一控制循环推进私有事务、HTTP、心跳和清理；
NVML 每次通过独立有界 worker，失败退避 5/10/20/40/60 秒后每
5 分钟一次半开。未回收 worker 保留槽位，禁止再建。坏输出、未知
观测和日志返回错误不重启健康模型；完整设备错误优先于后续超时
或坏输出。单纯丢失核心 HTTP 观测关闭准入，不当作已确认模型故障。

Guardian 已获得模型授权且保有同一租约时，旧模型与辅助子树清空
后，每代至多授权一次 CUDA 小探测；权限不因 Controller 换代重置。
新 Guardian 不继承这一权限，冷启动仍须 NVML 成功和所有权核对。
探测只分配 1 KiB，上传、执行、同步、下载校验，绝不 reset GPU。
真实设备故障、身份损坏、清理不确定继续进入保护状态。

模型加载上限 300 秒，恢复生成自检 30 秒；自检一次请求，不重试
POST。使用已有客户端中第一个满足 prompt >=512、output >=64、
TPM 为 0 或 >=576 的客户端，prepare 即检查；每代消耗该客户端
一次普通请求及实际 token 配额，计入本次业务验收 Chat 上限。
不能通过创建免费内部客户端绕过配额。网关绑定前独立验证 ELF、
监听、协议与客户端策略；模型每代换内部凭据，旧请求不重放。

## 证据与有限结论

原件根目录 `.q4t-work/serve-recovery-b2-20261108/`：

| 位置 | 结果 |
|---|---|
| `attempt-01/build.json` | Guardian 零警告构建 |
| `attempt-01/runtime-result.json` | 35 项真实 CPU 生命周期通过 |
| `attempt-01/production.log` | 导入失败保留；新 http.py 遮蔽标准库，0 用例执行 |
| `attempt-02/production-result.json` | 改为 core_http 后 22 项生产合同通过 |
| `attempt-02/production-runtime-result.json` | 受修复影响两条实际控制器恢复路径重验通过 |
| `probe-build-01/build.json` | CUDA 13.3/SM110a 探测零警告，仅编译 |
| `package-attempt-01/receipt.json` | 新候选封装 3ec79256，原生引擎仍为 60b1c4c1 |
| `package-attempt-01/entry-check.json` | 实际包 help/init/status 三项检查通过，未启动服务 |
| `audit.json` | 累计 169 项不同 CPU；本组 59 次执行、57 项不同用例，复用 112 项 |
| `inventory.log` | 157 来源锚点/3 负控通过，不是语义认证 |

生产控制器生命周期使用真实 Guardian、私有锁、exec 屏障和 HTTP，
但模型和设备是显式 CPU 替身；替身不进入生产包。已验证模型退出
保持控制器并恢复、控制器退出后清理恢复、NVML 未知时健康模型
保持 PID，以及单次探测路径。两条重验不重复计入新增覆盖数。
所有构建和验证均未运行真实模型、CUDA 探测或 NVML 查询。

默认 `build/q4t` 摘要仍为
`60b1c4c121d066789ebc44746316161320448d98095f32a7ec24e3fa00343ff3`。
候选 `recovery_accepted=false`，未部署。19c8b5aa 前一阶段 CI 已通过。

## 下一步与不扩展项

冻结最多 4 模型启动、20 Chat、30 分钟的单严格 GPU 租约驱动。
覆盖固定网关 JSON/SSE/工具闭环、辅助故障期间 PID 连续、模型退出
及控制器退出后的真实恢复、分钟配额保护、停止不复活和最终清理。
生成自检计入 Chat，外层监控仍严格；环境失败保留，不能用内层
降级策略掩盖验收 ENV_ABORT。通过前不安装候选、不改默认发布身份。

日志写入阻塞、全部诊断目录的长期留存治理、通用模型进度 watchdog、
grammar worker 回收、自动 profile 切换和 cgroup/开机恢复属于 C/D。
本组日志返回错误隔离和每代有界轮转，不代表长期磁盘占用已验收。
无外层管理器时，Guardian 整体死亡后不由子进程反向复活。

## 已冻结的真实验收顺序

驱动为 `tools/serve_recovery/acceptance.py`，明确的测试包装器不进入
生产包：只替换验证 Controller 的 NVML 查询结果/日志写返回错误，
在真实启动与 POST 前登记共享预算。外层 gpu_exclusive 始终严格；
模型、Guardian、网关和生产控制器源码不修改。跨进程预算 3 项
CPU 通过，原件 `driver-cpu-01/`；累计 172 项不同 CPU。

1. 初始模型生成自检后，通过实际 nginx 测 JSON/SSE/严格工具往返。
   注入内层 NVML 超时和日志 ENOSPC，确认仍用同一模型 PID；beta
   客户端 RPM=1 的第二条请求必须拒绝，alpha 保持可用。
2. 已开始 SSE 的自有模型被终止，原流不得补造 DONE；恢复中认证
   请求返回 503，坏凭据仍为 401。连续租约下执行一次实际 CUDA
   小探测，第二代加载/自检并等待完整分钟配额窗口，再验相同业务。
3. 终止自有 Controller，Guardian 清理后恢复 Controller 与第三代
   模型；固定网关保持。JSON/SSE 后另终止网关，确认重建只换网关，
   随后工具往返仍通过。第二、三代实际请求计数须各为 5（自检+4
   业务请求），排除在新代次重放旧流/POST。
4. 持久停止，确认模型、Controller、网关/后代、监听和锁清理，没有
   自行复活。外层租约最终记录必须 COMPLETED，监控全部成功。

计划 3 模型启动、19 Chat（含 3 自检、2 恢复期拒绝、1 RPM 拒绝、
1 中断流），硬上限仍为 4/20/1800 秒，执行在 1730 秒停止追加工作
并保留清理时间。动作前原子计数，Controller 换代不重置预算；失败
保留现场，不在同一计划下无限重试。loopback 明文及 process 范围，
不据此接受受控内网 TLS、整机故障、长期稳定或吞吐优化。

## 首轮夹具失败与剩余额度

`business-attempt-01/` 于 7851630f 执行：实际 1 启动/5 Chat/
46.991 秒，46 次外层 NVML 全部 OK。真实自检、JSON/SSE、beta
RPM 拒绝、辅助监控失能时原模型继续响应已通过。工具夹具把两个
integer 字段写成 numeric enum，现有合同仅允许 string/bool/null
枚举，服务正确返回 400；这是夹具错误，非模型恢复或工具解析通过。
原 FAILED/COMMAND_FAILED 保留，停止持久且清理确认，端口和锁释放。

只移除这两个整数枚举，保留 integer 类型与固定期望参数，模型包
1d414218 与引擎 60b1c4c1 不变。续验固定引用此前普通 JSON/SSE、
配额及辅助缺口证据，首代只补工具；第二、三代均完整做 JSON/SSE/
工具。模型恢复期仍发一次 POST 验 503，控制器恢复期用 GET 验
503，两个阶段都保留坏凭据 401。所有恢复动作在一个新的严格租约
内完成，首轮失败不拼成成功租约或覆盖。

剩余 3 模型启动/15 Chat，计数文件从此前 1/5 继续，动作前拒绝
超额；累计总上限仍 4/20/1800 秒，时限也扣除先前消耗。CPU 补充
这一不重置边界后 4 项预算通过，累计 173 项不同 CPU；其余已通过
证据按未变生产身份复用，不追加完整模型试验。

## 第二轮结果与已备妥的最后补验

`business-attempt-02/` 基于 200be393，2 次模型启动/10 Chat/
118.355 秒，累计 3 启动/15 Chat。真实首代工具闭环、辅助故障下
PID 连续、旧 SSE 无 DONE 中断、恢复期 503/401、一次 CUDA 探测、
同 Controller/网关的模型替换及 60 秒配额窗口通过。第二代完整
JSON/SSE/工具通过，实际 requests/success=5/5，无旧请求重放。

随后的即时 metrics 快照为 grammar started=3/reaped=2，驱动因此
FAILED/COMMAND_FAILED。代码表明响应与请求成功计数先发布，局部
StructuredDecoder 的析构才调用 GrammarProcess::Stop/wait4；
瞬时响应完成不能证明 worker 已经回收。原样本并不能证明 5 秒内
会回收，因此也不追改为通过。驱动已改为最多 5 秒等待实际齐平，
始终未回收仍失败，多出请求/迟到成功也不能通过；未改变原生引擎。
8 项 CPU 预算/回收时序合同通过，累计 177 项不同 CPU。

112 次外层 NVML 全 OK，末次 compute PID 为空；持久停止、子树清理
和锁/端口释放确认。两个 FAILED 和不完整回收快照原件均保留。
尚缺真实 Controller 与网关故障恢复，生产包仍为 1d414218。

已备妥 `--controller-completion` 驱动，仅新建一个测试服务，初代
生成自检并确认 worker 回收后终止 Controller；Guardian 清理并
启动第二代，等分钟配额结束，完成 JSON/SSE，替换网关而保留模型，
再做工具调用/真实文档结果/最终引用。第二代实际 5 请求及 worker
齐平、持久停止和清理均须通过。计划 2 启动/6 Chat，完整使用同一
严格租约，不重跑已通过的辅助/模型故障组。

**这段补验仍待预算修订确认，尚未执行。** 原 A/B 冻结总上限为
4 启动/20 Chat；已消耗 3/15，最后补验会累计 5/21，分别多 1。
源代码准备与 CPU 验证已完成，得到确认后才冻结这一具体计划并运行。
此前消耗/失败继续计入，GPU 总时间仍限制 1800 秒，不扩展 C/D。

## 用户授权后的最终补验与交付

用户明确同意上述 2 启动/6 Chat 补验，累计预算修订为 5/21；
`business-attempt-03/authorization.json` 与冻结 plan 保存这项边界。
6dee72df 的 8 项 CPU 与远端 CI 通过后，执行实际 2 启动/6 Chat/
115.996 秒；Controller 和网关故障恢复、JSON/SSE、严格工具/真实文档
引用、5 请求无重放、持久停止与清理全部通过。109 次外层 NVML 全 OK，
末次 GPU PID 为空，外层 COMPLETED。grammar 实际在 51.820 毫秒内
由 3/2 齐平到 3/3，未改变生产程序，也不追改此前即时样本。

三轮累计 5 启动/21 Chat、281.342 秒，完整租约累计 284.712 秒。
离线核对 870 Git 来源、38 封装 Python、16 原生构建来源和 19 个已
退出角色身份；267 次 NVML 全 OK，端口/锁释放。282 份原件和失败
归档，详情见最终结果。新 `build/service` 入口与发布元数据交付，
私有状态只初始化为 STOPPED；原默认和旧包保持，回退副本已核验。
安装入口检查 0 模型/0 Chat，不额外扩展业务测试或 C/D。
