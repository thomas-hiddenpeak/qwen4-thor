# RPM/TPM 与客户端公平准入：限定接受，默认交付待补验

2026-11-05。状态 **SCOPED_ACCEPTED_PENDING_DISABLED_CHECK**。
开发、CPU合同及启用v2策略后的真实业务短验通过；未宣称完整交付。
原预算2次模型启动已用完，默认关闭策略的独立JSON/SSE实机检查待做。
已请求将启动上限增加1次，尚未收到答复；原24生成/480秒GPU上限不增加。
默认程序和sidecar仍为a56bd448/914e067f，未安装候选、未合入main。

## 已验证能力

| 能力 | 实现与证据 | 边界 |
|---|---|---|
| RPM/TPM | v2每身份可选；实际模板prompt+可输出预算原子预留 | 单进程60秒固定窗口，重启清零；非滚动任意60秒限额 |
| 拒绝合同 | 真实TLS在SSE前HTTP429；client_rpm_exhausted / client_tpm_exhausted | 没有自动重试；preflight不预留将来额度 |
| 取消结算 | 等槽取消全退54 token；活跃取消按27输入+5输出结算 | RPM不退；完整逻辑prompt含缓存命中，非GPU计算计量 |
| 公平准入 | 实际次序active-alpha → beta-b1 → alpha-a1 → alpha-a2 | 只对已准备的槽位准入轮转，不保证准备/GPU时间/网络/SLO公平 |
| 等候份额 | CPU验证每身份floor(global_queue/client_count)上限、取消和不匹配绕行 | 不借用其他身份份额；无抢占 |
| 窗口与回收 | 真实到期后两身份恢复；剩余预留0；CPU验证跨窗未完成预留不丢失 | 结算进入完成窗；非分布式计费 |
| 工具/输出 | 首轮4完整响应、2真实docs/MODEL.md工具与引用独立复核并复用 | 不重跑身份未变的证据；首轮整体FAILED仍保留 |
| 旧行为 | v1严格解析及默认关闭的CPU合同通过 | 本候选无策略的实机检查尚缺，不以CPU代替 |

配置用法见[网关说明](../tools/serve_gateway/README.md#可选-rpmtpm-与准备后公平准入)。
首次v2策略为alpha RPM6/TPM10000、beta RPM20/TPM2000；每身份4并发，
全局8并发/6等槽，单槽8192、prefill2048、默认MTP3/full head。
运行来源223项中仅10项serve变动，213项保持；数学、kernel、tokenizer、
模板、缓存计算实现不变。无性能优化、全模型数值或正式LAN的新接受。

## 验证与保留的失败

零警告本机构建；303公共host、269必需合同、9结构化通过。
初组21 Python通过；后续只修驱动并复用其余来源，最终6驱动检查通过
（合计24个不同Python案例）。协议生成及静态库存检查通过。

| 尝试 | 结果 | 资源与复用 |
|---|---|---|
| package初次 | 网关夹具12超过既有全局上限8，静态拒绝 | 0模型；原package/private不覆盖，改8并定向检查 |
| execution | 相对路径不满足既有guard祖先身份门，COMMAND_FAILED | 0模型/0HTTP；保留原lease，改绝对路径调用 |
| execution-02 | 首轮4响应/2工具后preflight夹具漏request包装，HTTP400，FAILED | 1模型/4生成；52.470秒驱动；4wire独立复核、局部复用 |
| execution-03 | 修复夹具后仅续配额/公平，PASS_BOUNDED_CLIENT_GOVERNANCE_SCOPED | 1模型/12生成；8成功+2取消+2拒绝；98.911秒驱动 |

累计2模型/16生成/28控制，12完整响应/2取消/2拒绝；两次业务租约及
零模型入口失败累计155.816秒。最终租约100.929秒、94观察COMPLETED，
未观察到竞争。4自有进程退出，端口/GPU/原锁dev66305 inode3294067
释放；SOAR仍masked、容器空。轮询不能排除采样间隔内的不可见干扰。

续验普通短答handler观察约326–428ms，队列短答含等待约737–1056ms；
本轮是业务治理，不声称吞吐提升或统计非劣，不追加性能优化实验。
实际结算alpha177/beta87 token，由各成功响应usage及取消生成计数独立
重建；最终新窗口各计1请求。未启动长稳、内存压力、托管升级或常驻服务。

## 身份、证据和继续入口

- 候选：c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636。
- 协议：9c8758c4f85e1d4f7621bd8854c88f64d9cc739744669754aab18c7e183e9706。
- 续验来源：00a4d196；实现首提交ca93be03，后续修复均仅驱动/文档。
- [结果](evidence/serve-client-governance-20261105/result.json)、
  [清单](evidence/serve-client-governance-20261105/manifest.json)、
  [原件归档](evidence/serve-client-governance-20261105/records.tar.gz)：
  415原件/506本机引用，新进程独立重建通过。含密钥请求与ELF只留本机
  SHA引用，不是灾难恢复备份。
- 精确原默认/依赖与候选位于
  `.q4t-work/serve-client-governance-20261105/package-04/{previous,candidate}/`；
  已有三份先前包、两次失败现场均保留，不能覆盖或删除。
- [离线审查入口](../tools/serve_client_governance/scoped_report.py)重读raw
  wire、真实资料、额度账、窗口、队列次序及guard；不启动模型。

下一步只需决策已提出的额外1次启动，若授权则固定同一ELF的无策略
JSON/SSE两请求，补声明/精确默认替换及已验ELF的CLI健康，累计请求/GPU
仍受原上限约束。预算选择前不安装默认、不扩大范围；旧托管包41dc8fdb、
持久任务与day计划继续固定原身份。长稳仍由用户执行。
