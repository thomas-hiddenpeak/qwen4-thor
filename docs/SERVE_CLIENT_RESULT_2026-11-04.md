# SRV-10c客户端隔离与请求预算限定接受

本机真实TLS双客户端短验完成限定接受。初版027deebc，终态额度
修正3743b4c8，最终候选7619966e；共享缓存夹具修正e374294e不改
运行时。当前完成的是协议与资源隔离、业务闭环及本机接入证明，
实际LAN部署、多机与新托管包E2E、长稳仍待后续。

默认已交付7619966e/sidecar a2f9cb8f，protocol63b29f13，222来源；
原a479d8ab/9ed141ad作为可核验回退pair保留。交付0新增模型/HTTP，
CLI与同ELF已验业务复用，guard COMPLETED/finalized DELIVERED。
首个相对guard argv拒绝在begin前发生，未改默认，原失败保持；
修正入口仅用绝对路径并写新目录。详见[实际交付](SERVE_CLIENT_DELIVERED_2026-11-04.md)。

私有固定policy支持最多64客户端，网关覆盖内部身份头，后端验证
独立随机凭据；启用策略仅loopback文本。data/control分别计额到
完整清理，响应收尾仅允许后继最多等100ms，active饱和仍429。
输入正文、真实模板token和输出预算在昂贵工作/SSE前检查，省略
输出取server/client较小值。请求/取消/私有session及共享prefix键
按client隔离，现有存储/模型数值不变。包绑定、发现及固定指标
同步，凭据和客户端明细不出现在公开发现或本批日志中。

## 证据与失败边界

- 原独立HTTP target漏链失败保留，修正后零警告。280公共host、
  246必需host、9结构化后端、28真实SSD检查点、70包回归通过；
  handoff新增3项，29相关host在off/on分别通过，其余按身份复用。
  原短验驱动4项及cache修正4项CPU通过。
- 首轮27原生HTTP通过；2模型/10 HTTP生成尝试，4成功、1取消、
  5生成前拒绝。鉴权/预算/preflight/伪造头/同ID取消和额度通过。
  strict JSON后工具请求在上一handler清理时误收429，原FAILED和
  COMMAND_FAILED保留。修正finishing交接仍完整持有资源。
- 第二轮重验2原生HTTP；1模型/11尝试，9成功、1取消、1预期quota
  拒绝。strict JSON立即真实项目工具/引用回填成功，grammar3/3回收；
  私有缓存beta miss=0、alpha hit=8234，分支答案均43。共享只读
  请求均miss=0，夹具省略写点却期待命中导致原FAILED，完整保留。
- 第三轮相同ELF，显式8192写点；1模型/5成功，alpha及beta用同scope
  分别首次miss=0，后续各自hit=8192，四答案43及普通恢复710003。
  正常停服、所有请求/grammar租约归零，guard COMPLETED。

累计4模型启动、26生成HTTP尝试、18成功、2取消、6生成前拒绝，
真实资料工具1次，20显式控制；27个不同HTTP合同执行29次。三个
GPU窗口分别77.661390/72.087636/50.248852秒，73/68/48次观察，
无已观察到的竞争；前两轮仍是COMMAND_FAILED，不能追认整轮通过。
轮询不排除采样间短暂干扰。业务记录的elapsed是HTTP头到正文读完，
不是完整E2E耗时，尤其非流式不可用于性能结论；本轮不声称性能非劣。

9个直接自有进程身份均退出，GPU空、原66305/3294067锁可用，
SOAR恢复unit仍masked/inactive，容器空，10秒后无自有进程复起；
默认pair与两份day在业务验收结束时不变且day未执行。
[交付步骤](SERVE_CLIENT_DELIVERY_2026-11-04.md)独立记录最终默认状态。

419份原件无损封存，9个本机ELF身份另列，压缩包485496字节。
[Git证据](evidence/serve-clients-20261104/manifest.json)由
tools/serve_clients/report.py离线重新解析wire、取消、输入错误、缓存
用量、工具引用、guard和累计计数；核对通过。原归档扫描把自身字面量
误报成私钥标记的失败也保留，已改为完整PEM头匹配，未发生凭据发布。
私有policy/API Key/证书私钥与模型缓存内容不入Git。

未覆盖token/minute或长期公平调度、按client物理RAM/SSD硬配额、
多机TLS入口与实际来源限制、新SDK/媒体，以及小时/天级长稳。
客户端配额之外仍受全局额度限制，host同用户管理边界不变。
