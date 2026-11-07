# 托管服务NVML降级：有界实现与验证

用户在说明NVML运行中间歇失联后要求继续推进。本阶段属于服务可靠性，
不修改推理内核，不证明厂商故障已经修复，也不补算视觉优化性能验收。

## 冻结合同

- 新包显式`prepare --nvml-policy degrade`选择v7策略；未选择及旧包仍严格。
  启动前NVML未知拒绝启动，运行期NVML错误/超时可降级继续服务。
  首版只允许`restart-limit=0`，避免可见性未知时自动启动替代模型。
- 共享flock全程保持，已知外部竞争仍中止。HTTP健康、CUDA错误、内存
  保护、身份、日志写入及清理错误仍按原合同处理，协议损坏不降级。
- 状态分别记录业务与GPU监控；未知PID不得解释为空闲，不缓存旧值冒充
  新观测。保留失败、恢复、累计缺口和最近成功观测的时间。
- 每次连续故障最多5次重试，分别等待5/10/20/40/60秒；成功后结束该
  缺口。重试耗尽或查询worker无法回收时，本代次停止新增查询，业务
  可继续，状态持续未知。持有原Popen对象非阻塞回收，不堆积查询进程。
- 监控缺口发生过即使用`COMPLETED_WITH_MONITOR_GAPS`终态，不能通过
  原严格性能审计的COMPLETED门。停止后自有进程清理与GPU可见性分开。
  未回收查询worker仍记清理失败，不伪造GPU上下文为空。
- 降级只由托管代码显式传入；开发GPU guard CLI没有降级开关。
  真实短验外层仍使用严格guard、同一原锁，真实监控失败仍ENV_ABORT。

## 有界验证

先成组完成代码，再统一运行CPU故障注入与受影响回归：启动未知、
运行超时后恢复、持续失联/重试耗尽、不可回收查询、竞争/PID复用、
日志及协议错误、正常停止/退出状态、包配置防篡改与旧包兼容。
采用真实CPU子进程验证租约和业务存活，不用空集合模拟监控失效。

CPU通过后，最多1次模型启动、4次Chat、300秒严格GPU租约。使用已发布
默认ELF和新v7包，通过独立验证驱动向内层监控返回明确的合成超时，
外层NVML正常工作；验证同一模型PID完成JSON、SSE及工具业务并可正常
退出。故障来源与真实NVML日志分开，不能声称复现或修复驱动故障。
不重跑数值/五档或长稳；具体真实短验驱动与来源在运行前冻结。

仅定向修复具体失败并重验受影响项；不自动追加模型或扩样。旧默认
程序、旧服务包与原视觉ENV_ABORT证据保持。阶段提交可包含未验准备，
但只有CPU和真实短验均满足合同才能声明限定业务接受。

## 实际结果与限定交付

实现来源`bb2a3fd0`；新v7包manifest
`f69cb406a80b64fc7c51a23e212c4d183dca86c0dbcebee3e4fe3b3593e3accd`，
位于`.q4t-work/serve-nvml-degradation-20261107/package`。使用当前已接受
ELF ea1b474d与sidecar 3eb46385，未修改默认pair或封存旧包。

实际1模型、4Chat、1次显式metrics控制，严格外层原锁租约49.199秒；
监督器自身控制请求没有另行逐条计数，事前上界244，不冒称实际244。
内层一次合成TIMEOUT造成5.164秒监控缺口；前三请求在UNKNOWN期间
完成JSON答案42、SSE答案42和strict项目资料工具调用。工具只读实际
STATUS前16行一次，恢复后第四请求返回精确引用
`[docs/STATUS.md:L1-L16]`。同一模型PID，无重启或请求重放，原始wire
独立重解析、工具原文/来源SHA及4成功/0失败/0中止计数均核对。

外层47次真实NVML全OK且无竞争；内层41次真实查询全OK，合成超时
明确标记，未诱发厂商驱动故障。托管STOPPED、cleanup_confirmed=true，
guard保留COMPLETED_WITH_MONITOR_GAPS；末次自有查询worker已回收。

**原阶段仍保留FAILED/外层COMMAND_FAILED。** 唯一失败是驱动最后的
普通bind把服务端TIME_WAIT误判为占用，发生在4请求、正常停止与
最终空GPU观察之后。只读核查时该端口12条TCP记录均为TIME_WAIT，
无LISTEN；不复用地址的bind失败，按服务SO_REUSEADDR策略bind+listen
成功。修正仅涉及验证驱动，补2项真实TCP测试（活监听拒绝、TIME_WAIT
可复用）均通过，未重跑模型。新CPU后验再次确认94个自有PID不存在、
18147可绑定监听、原锁inode3294067可用；GPU空的证据复用原租约内
最终查询，没有在修正时重新查询GPU或宣称厂商根因已修复。

独立复核24份冻结Git来源、184次CPU执行中的130个不同用例，封存
108份阶段原件与3份修正来源。首轮实现的两项公共host CI通过；端口
修正只重验受影响2项，本阶段新增模型/HTTP/GPU重验为0。运行期持续
失联、重试耗尽、无法回收worker及附加配置组合只具CPU证据；不扩展
为这些场景的真实GPU接受或长稳结论。

[独立决定](evidence/serve-nvml-degradation-20261107/decision.json)限定接受
上述托管业务能力和退出处置；[原件绑定](evidence/serve-nvml-degradation-20261107/bindings.json)、
[CPU后验](evidence/serve-nvml-degradation-20261107/postflight.json)保留。
新包的managed_service_accepted仍为准备时false，接受由精确manifest
绑定的伴随决定表达，不改写旧清单；回退使用原严格包或重新prepare
时省略`--nvml-policy degrade`。无常驻服务、开机项或自动重启安装。
视觉rowmax完整MTP比较/部署健康仍缺失，本项不是其性能接受。
