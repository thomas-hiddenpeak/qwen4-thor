# SRV-12h：独立运维托管与TLS生命周期限定接受

2026-11-04。按[冻结合同](SERVE_OPERATIONS_MANAGED_2026-11-04.md)完成
开发与短验，**本机限定接受**。本轮没有修改生产实现或默认程序，没有
常驻服务安装、实际LAN部署或长时间运行；持续工业化目标仍active。

## 已验证的部署链路

- v6生产controller/guard/worker实际启动同一默认ELF，主入口18132、
  独立运维18133，restart-limit=0。启动后inspect识别同后端两个监听。
- 缺少运维上游和运维端口不符的两个托管网关，在nginx启动前拒绝，
  均形成FAILED/cleanup终态、释放包claim，后端保持运行。
- 正确TLS网关固定绑定两个监听。136个公共慢头占满主入口后，**实际
  监督器新采样**仍READY，记录同后端与公共连接136；运维诊断HEALTHY，
  TLS models/capabilities均200，最后metrics仍136，随后释放慢头恢复。
- alpha经第一代网关完成资料工具与最终回答，明确停止网关；第二代
  网关启动后beta完成相同闭环，后端PID/start身份保持。两个client各
  55/58输出token，分别遵守512/64预算，prompt为564/890。
  实际读取`docs/MODEL.md:L1-L24`，得到正确架构
  `Qwen4ExpForConditionalGeneration`及引用。合计4成功响应、2真实工具。
- 最终active/public/business/body/grammar owner归零、slot归还，
  grammar开始/回收4/4，0请求错误/中断。先停止两个网关再停止后端，
  三个托管终态STOPPED/cleanup，10秒未复起。独立停后inspect完整，
  所有已记录进程身份消失，三个监听和GPU计算上下文为空、原锁可用。

## 精确身份与证据

| 项目 | 身份或结果 |
|---|---|
| 冻结实现/驱动提交 | `8213896c` |
| 默认ELF（保持） | `7eff020de66b96f3175b3e123909be9e2f58142d54d1dd5992750d23cbe9c769` |
| 协议（保持） | `ddfba8c002d8fb899b4c00d42612b0a26669036a1acc65e8292a10c0a9b69342` |
| 已验v6包manifest | `c4775a10cf6c2d308729a0e6decd0f1178d4aa8f27f50391d7b090e96e1a349a` |
| 模型/成功网关/拒绝网关启动 | 1 / 2 / 2；拒绝网关0个nginx子进程 |
| 生成/父控制计数 | 4 / 13；监督器和网关绑定HTTP单独计界 |
| 生产guard | COMPLETED，64.564790秒，60次monitor观察 |
| 同一flock副本保管 | COMPLETED，24次观察；自原取得锁到最后观察64.776892秒 |
| 驱动总时间 | 75.954421秒，包含停止后10秒不复起观察 |
| CPU | 5项验收驱动故障/预算检查通过；不重复未变生产回归 |

生产guard和custody覆盖时间重叠，不能把两者时长相加；复制的是同一
flock描述符，原锁device/inode为66305/3294067，未替换或删除。
所有观察无记录竞争/不可读进程；轮询仍不能排除采样间隔内的短暂干扰。
监督器HTTP事前上界204，两个成功网关绑定各4次另列，不称为实测总次数。

[结构化结果](evidence/serve-operations-managed-20261104/result.json)、
[逐文件清单](evidence/serve-operations-managed-20261104/manifest.json)与
[原始记录归档](evidence/serve-operations-managed-20261104/records.tar.gz)
共115份原件、938,167字节；242本机引用逐项核对。原始SSE重新解析并
与请求身份/存储响应、真实工具来源及预算对账，终态和释放记录核对。
API Key、内部token、取消凭据和TLS私钥不进入公开归档；省略原件的
摘要仅是本机引用，不是可离机恢复备份。

```bash
python3 -B tools/serve_operations_managed/report.py verify
```

现场记录在`.q4t-work/serve-operations-managed-20261104/`。实际已验包
仍为上一阶段的`accepted-backend`，配套接受记录以本页及结构化结果为准，
没有追改原prepare声明。两个新网关包各有独立摘要，详见结构化结果；
不能把本次结果套到其他包或配置。容器为空，SOAR恢复unit保持masked/
inactive/dead、MainPID0，旧Key、默认pair及三份未执行day计划保持。

## 仍待完成

本次包不包含host memcg域，v6+host资源组合仍需独立核验，不能代替
完整统一物理RAM硬保护。实际LAN地址/来源网段/DNS/CA/防火墙及远端
客户端、正式通知/容量/备份策略、常驻安装与用户小时/天级验证仍待。
未登记fc63db28→ddfba8c0任务协议迁移，不自动放行旧任务；模型/模板
相同不能代替兼容审核。此次显式重建网关没有重放未知请求，不声明
请求跨网关的透明恢复或通用exactly-once。
