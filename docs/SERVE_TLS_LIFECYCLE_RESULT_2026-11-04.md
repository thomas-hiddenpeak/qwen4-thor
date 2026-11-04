# SRV-10e：TLS材料、有效期退场与同CA换证限定接受

2026-11-04。业务可靠性推进，生产实现冻结于`e43761ce`。依据
[冻结合同](SERVE_TLS_LIFECYCLE_2026-11-04.md)，本机短验已完成；
实际LAN接入、自动签发/撤销和小时/天级运行不在本次接受范围。

## 实现与边界

- 新内层网关包v3、托管包v4，固定TLS校验源码与工具身份。校验包内
  实际复制材料，最多8张PEM证书；密钥配对、主机名、服务器用途、
  全链生效/到期交集与启动剩余严格大于3600秒。
- 可选`--trusted-ca`指定一个固定CA锚点，OpenSSL严格验证链、用途
  和主机名，不用默认/环境CA补链。省略时明确`not_configured`，不
  声称客户端信任已证明；不检查OCSP/CRL，不自动签发/续期。
- 运行每秒检查文件身份和缓存有效期，私有状态含VALID、EXPIRING
  （到期前7天）、NOT_YET_VALID、EXPIRED、UNKNOWN。失效时仅对
  当前网关QUIT排空，最多35秒后TERM/KILL回收，终态FAILED记录原因。
  不停止后端、不自动重启或重放。它不是已有TLS会话即时撤销机制。
- 修复child已创建、logger尚未建立时的清理路径。运行状态属于
  私有管理入口；TLS专属Prometheus采集/告警尚待接入。

## 验收结果

61项CPU首轮通过，包括真实OpenSSL/SSL MemoryBIO握手、错误CA/
主机名/缺失链、未来/过期leaf与intermediate、锚点时间、用途/密钥、
复制材料/包完整性，以及真实子进程排空、超时与早期失败回收。
时钟边界使用测试接缝，没有修改系统时钟或实际等待证书到期。

4项驱动首轮3通过，1项把预期RuntimeError误写成ValueError；保留
原件，仅修正该断言重验1项通过。生产清理逻辑未因该断言改变。
旧网关v3包通过自己的固定源码生成unit成功，无监听/模型/HTTP。

真实阶段只加载1个已接受v5后端，顺序运行2个新托管v4网关：

| 项目 | 实际结果 |
|---|---|
| TLS | 同CA、两张不同leaf各完成1次有界真实握手，校验CA、主机名和实际peer指纹 |
| 换证 | 旧网关正常排空并释放监听后启动新包；停止请求到新证书握手完成2.146141秒，非零停机或精确停服时长主张 |
| 后端 | 换证前后PID/start_ticks/boot_id一致，1次加载、0重启 |
| alpha | 输出预算512，2成功生成、1真实资料工具、1完成任务 |
| beta | 输出预算64，2成功生成、1真实资料工具、1完成任务 |
| 业务质量 | 均准确回答`Qwen4ExpForConditionalGeneration`并引用`[docs/MODEL.md:L1-L24]` |
| usage | 两客户端各两轮输出55/58 token，prompt 647/890；独立预算均满足 |
| 资源回收 | 4请求成功结束并释放；grammar started/reaped 4/4，终场无在途 |
| GPU租约 | 原共享锁一次持有62.235038秒，59次guard观察无竞争、COMPLETED后释放 |
| 总预算 | 64.271214秒、7父控制、2 TLS探测；未跑day |

host memcg峰值16,545,222,656字节（15.408939GiB），high/max/OOM/
pids事件为0。此值不是Thor完整物理RAM占用或硬限额证明。
轮询不能排除短于采样间隔或不可见的外部干扰。

离线报告最初把换证身份与终态已清空的`worker.backend=null`比较，
误拒绝汇总；实际原始`attempt-1.json.owner`与换证前后身份完全相同。
保留错误日志，只修正离线比较目标，不重跑模型/HTTP。报告工具最终
身份与历史运行plan分别固定，不把新报告源码冒充原运行源身份。

## 清理、交付与回退

10个记录的自有进程身份均退出，3代次SEALED；host scope、GPU、原锁
（dev66305/inode3294067）及18128/18448监听释放。容器为空，SOAR
恢复单元仍masked/inactive/dead/MainPID0。独立旧`vllm_proxy`代理未
被当作本次自有服务操作，GPU计算进程为空。

默认ELF仍`7619966ef2a5101e9a4bbc56d20805d980f85324e0313a71c248c701df6aa501`，
sidecar仍`a2f9cb8feb2fab94917b4d4ad1b22020efe42c2416b87306873d15d26b71a263`，
222个运行时源码身份一致。原客户端Key、02k包与三份day计划不改，
day均未执行。本次交付默认prepare工具，未安装常驻服务或替换C++程序。

仅新准备的包具有本批TLS能力；现有固定包不会自动升级。准备与操作
见[网关说明](../tools/serve_gateway/MANAGED.md)。新包只显式启动，
同CA换证保留CA与Key；CA变更/任务迁移、实际地址/访问来源另验。

保留旧网关回退包：
`.q4t-work/serve-host-envelope-20261104/gateway-package`，manifest
`db0aaaa1a2232bc45d671bb7767cec87c95d97e1840be220622dc0e242cc125e`。
使用它自己的包内入口；旧包没有本批运行有效期保护，不自动切回。

新验收包位于`.q4t-work/serve-tls-lifecycle-20261104/`：

| 对象 | SHA256 |
|---|---|
| plan.json | `5aaa2adc9254dc1bd749242b382c650b5156933e6351fcfd4c6bda05073cd83e` |
| gateway-1 manifest | `8a8c0849dc665e98720c8db630ca2a43ad1d11c715e7c6cfd38786d96df11c02` |
| gateway-2 manifest | `1ae1e52812be033fbb5480bacd7c77858886c5a63a5a18048592bdfbb9887657` |
| 固定CA文件 | `7b36048ba9a98210783304eadc4a458bd97513273fbea4be0256490ed35a8cfe` |

## 可复核证据与后续

[281份原件、5个本机二进制路径身份](evidence/serve-tls-lifecycle-20261104/manifest.json)
封存571,676字节，archive SHA256
`915701dfbd55641276c27db7e7d8ee7af5c3630b133174b1c6c6a954f171b8ce`。
不含API Key、gateway token或私钥。
[重建结果](evidence/serve-tls-lifecycle-20261104/result.json)核对CPU源、
固定包/实际TLS指纹、SQLite事件和请求日志、资源/租约及清理。

```bash
python3 -B tools/serve_gateway/tls_report.py verify
```

最终报告源码另固定为
`f93b26172604b548450823594c854ac711f85f1454bd5b4a49c6f41ee55eb1a1`；
281项离线重建已通过，不启动模型/HTTP。实际LAN参数、TLS到期告警、
正式通知/容量与备份介质、完整物理RAM保护和用户长验继续待办。
持续工业化目标保持active。
