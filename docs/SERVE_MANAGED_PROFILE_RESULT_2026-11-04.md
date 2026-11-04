# SRV-12i：可选托管组合限定接受

2026-11-04。当前默认pair的新v6托管包同时启用发布声明绑定、独立运维
入口、客户端策略及host资源域，已完成本机短验。生产源码与默认程序
均未修改；这是组合接线的证据，不替代长期稳定或正式LAN部署验收。
范围和预算见[冻结合同](SERVE_MANAGED_PROFILE_2026-11-04.md)。

## 实际结果

- 生产controller/guard/worker正常启动一个模型，后端PID始终保持。
  实际136个公共慢头占满18132时，监督器取得新READY样本；独立运维
  18133的诊断HEALTHY，TLS models/capabilities成功。实际capabilities
  全部字段与新包内冻结声明一致；host观测在压力期间也继续更新。
- 两个错误运维上游分别在nginx子进程启动前拒绝，后端保持健康。
  两代正确TLS网关18452先后启动，alpha、beta各完成2响应和1真实工具。
  两客户端都返回`Qwen4ExpForConditionalGeneration`及
  `[docs/MODEL.md:L1-L24]`，与实际源文件、工具结果、原始SSE核对一致。
- alpha512、beta64输出预算均满足：各两轮55/58输出token，输入
  564/890 token。四次响应读取耗时分别3.849/4.052/3.751/4.038秒；
  这只是有界业务记录，不是TTFT、五档性能或性能非劣结论。
- worker处于control子组，后端在exec前进入payload，租约controller
  在两者之外。88份采样观察到模型与四个真实grammar子进程均归属
  payload；四个worker均回收，最终业务/连接/grammar资源归零。
- 实际host high16GiB/max20GiB、pids512、swap0；control max512MiB/
  pids64。内核记账峰值17,098,186,752字节，约15.924GiB；采样期间
  pids最高141。high/max/OOM/pids拒绝事件均0。memory.peak覆盖该组
  生命周期，pids峰值仅覆盖READY之后采样；均不代表整机物理RAM。
- 两网关与后端均STOPPED/cleanup，payload为空并移除、scope退场，
  10秒未复起。双监听/TLS/GPU空、原flock可用；恢复unit保持masked/
  inactive/dead/MainPID0，容器空。没有安装常驻服务或执行day计划。

## CPU、首次失败与身份

9项原CPU首轮通过，其中一项覆盖host/运维/client的八种显式组合；
另含声明副本独立性、依赖摘要、host失败不能被READY掩盖、禁重启、
委派缺失在模型前拒绝及采样失败仍清理。资源核心与02k内核验收SHA
相同，本次不再制造OOM/pids/high故障。

原`2d3c1305`准备入口误将nginx定位到内层gateway配置目录，出现
FileNotFoundError；当时只准备了后端包，零模型/HTTP/GPU。stderr、
私有输入、后端包及原驱动源码保持。`13cfaffb`修正为读取已校验外层
manifest的可执行文件路径，1定向检查通过；原9测试类及运行驱动AST
保持，直接复用。新网关在prepare-02目录生成；实机短验首轮通过。

| 项目 | 身份或结果 |
|---|---|
| 实际驱动提交 | `13cfaffbb84553b51ae00f6579c50cd1be53f6dc` |
| 新组合包manifest | `7a5d411c58a533ff4e218424f27037e8c783c259978563c2f431dac635f37083` |
| 默认ELF，保持 | `7eff020de66b96f3175b3e123909be9e2f58142d54d1dd5992750d23cbe9c769` |
| 默认sidecar，保持 | `cc83bf3a36c2b9658221554f39a7e6bb807f2618cb37b87b2f19fd7d7ea91d04` |
| 协议，保持 | `ddfba8c002d8fb899b4c00d42612b0a26669036a1acc65e8292a10c0a9b69342` |
| 模型/成功网关/拒绝网关/生成 | 1 / 2 / 2 / 4 |
| CPU/父控制 | 9复用通过＋1定向通过 / 13 |
| 生产guard | COMPLETED，69.852316秒，65次观察 |
| 同flock保管至退出 | COMPLETED，70.047570秒，额外24次重叠观察 |
| 驱动总时间 | 81.418349秒，含停止后10秒观察 |

同flock副本不是第二把锁；原dev66305/inode3294067保持，重叠观察时长
不能相加。监控没有发现竞争，轮询不能排除采样间隔内的短暂干扰。
监督器HTTP事前上界204、两个成功网关绑定共8调用另列，不当作实测
父控制计数。所有运行来源、旧包、原Key及三份day计划保持且day未跑。

## 封存与使用边界

[机器结果](evidence/serve-managed-profile-20261104/result.json)、
[清单](evidence/serve-managed-profile-20261104/manifest.json)及
[152份原始记录](evidence/serve-managed-profile-20261104/records.tar.gz)
已离线重建。压缩405,309字节，SHA-256
`79abffc932e744a754f17a53ac83c53280f3c7d5300a18395a11ea15a9e4edba`。
296份本机引用逐项核对；API Key、内部/取消token和TLS私钥不公开。
未入归档的二进制/私有请求只保留本机身份，不宣称可离机恢复全部状态。

首份148记录归档在内部核对后仍向自己的archive.stdout写入成功信息；
独立进程复核发现该文件已与采集时的空文件摘要不同。首份归档三文件
完整保存在本机archive-01，错误收据及修正说明入新归档。未修改生产、
驱动或核对器；等待原进程关闭日志后重新采集，输出只返回调用端。
随后独立进程重新核对通过；没有追加GPU/模型/HTTP，原成功业务保持。

```bash
python3 -B tools/serve_managed_profile/report.py verify
```

新包在`.q4t-work/serve-managed-profile-20261104/backend-package`；两代
配套网关在同目录的prepare-02下。只对这些精确摘要和本次配置接受，
不修改原prepare的未验标记；接受依据是独立结果文件。原12h无host
包仍保留作为已知回退入口，须先停止新网关/后端并确认GPU/锁释放，
再在新运行目录启动；不在本轮重复回退测试。

高级配置仍按需启用，未启用不代表缺陷。实际LAN、正式容量/留存/
通知策略及用户小时/天级运行仍按选定部署完成；memcg漏计CUDA的
边界不变，不能称为全部统一物理RAM硬保护。持续工业化目标active。
后续优先补一项源码已确认的启动缺口：托管监督器目前只预检主端口，
独立运维端口的已存在冲突要等模型加载后才在原生bind处失败；应在
启动模型前也检查该端口，且不能把预检成功宣称为消除后续竞争。
