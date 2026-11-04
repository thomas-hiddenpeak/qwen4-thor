# TLS运行观测与到期告警

绑定一个托管v4网关包和一次run，只读公开证书、包身份及私有运行记录。
同时报告服务生命周期和证书材料的当前有效期，二者分别判断。它不
发TLS/HTTP探测，不读取API Key/私钥或网关配置，不停止目标服务。
验收范围以[STATUS](../../docs/STATUS.md)为准。

## 准备与运行

使用本仓库build/或.q4t-work/内的新私有目录。以下大写摘要须替换为
实际值；nginx必须与绑定网关的固定二进制一致。

```bash
python3 -B tools/serve_tls/observer.py prepare \
  --gateway-package .q4t-work/managed-gateway-001 \
  --gateway-manifest-sha256 GATEWAY_MANIFEST_SHA256 \
  --gateway-run .q4t-work/gateway-run-001 \
  --deployment thor-internal \
  --nginx .q4t-work/managed-gateway-001/nginx \
  --port 19450 --output .q4t-work/tls-observer-001

python3 -B tools/serve_tls/observer.py watch \
  --state .q4t-work/tls-observer-001 --policy-sha256 POLICY_SHA256
```

prepare检查真实公开证书、冻结源码/解释器/配置并检查nginx语法，不
启动监听。watch重定位到包内隔离入口，每秒读取源；静态exporter
仅在loopback提供GET/HEAD /metrics。停止已确认身份的前台观察器
会回收它的exporter；检查observer-terminal.json的cleanup_confirmed。
unit只输出Restart=no、无Install的模板，不自动安装或启用。

status和metrics只读取已有快照，不刷新原采样时间。observe为无监听
的一次采样，与watch共享互斥锁。首次完整controller/目录绑定固定，
后续替换、缺失、失联或身份不符转UNKNOWN，不自动改绑。换证或换
代次需显式准备新观察包，先保留旧记录。

## 判断与通知

指标标签只有受限deployment、policy SHA generation及固定枚举；
不含域名、路径、PID、Key、请求或证书指纹。私有记录仍含本机身份。

| 指标 | 判断边界 |
|---|---|
| state / reason | WAITING、STARTING、READY、STOPPING、STOPPED、FAILED、UNKNOWN；READY为本机源身份与监听证据 |
| certificate_state / trust | 固定包材料的VALID/EXPIRING/NOT_YET_VALID/EXPIRED/UNKNOWN与显式CA配置，独立于服务是否运行 |
| evaluated_timestamp_seconds | 原观察时间；旧静态文本仍可被采集，须同时检查新鲜度 |
| not_before / not_after_timestamp_seconds | 整条证书链及显式CA的有效期交集；缺材料时省略 |
| source_age_seconds / cleanup_confirmed | 活跃源年龄或历史清理记录；缺失不填零 |

该接口信任同UID的固定私有源，不证明实际LAN可达、远端握手或撤销
状态。全链剩余7天进入EXPIRING；尚未生效或到期为无效；观察滞后
超过30秒或时钟异常为UNKNOWN。采集成功不代表业务健康。

在[通知工具](../serve_notify/README.md)policy中显式使用version:3，
增加tls_observer_port；可另选host_observer_port，所有端口须不同。
新notify包v3保留generation并按代次分组，旧policy v1/v2行为保持。
加载[10条TLS规则](../../deploy/serve-ops/q4t-tls.rules.yml)：

| 告警 | 先检查 |
|---|---|
| ExporterDown / TargetMissing | 目标端口/配置；missing仅表示完全没有该job，不能替代部署清单 |
| MetricContractMissing | 固定指标族/有限值/单选状态/代次标签是否一致 |
| ObserverStale | 观察器停顿、原时间戳及本机时钟；不能由旧材料状态推断当前有效 |
| SourceUnknown | 私有reason、原controller/目录/子进程与源新鲜度 |
| GenerationWaiting / GenerationTerminal | 预期代次是否已启动，终态是否计划停机；不自动恢复 |
| CertificateExpiring / CertificateInvalid | 固定包全链日期、显式CA有效期；安排新包/网关/观察器换代 |
| TrustNotPinned | 包是否配置显式CA；未配置不等同于已验证信任链 |

表中名称均带Q4TTLS前缀。规则5秒评估、目标15秒采集，告警还有各自
持续时间和通知分组等待，因此30秒新鲜度阈值不等于30秒交付承诺。
跨主机采集、正式接收渠道和小时/天级长稳仍需实际部署验收。
