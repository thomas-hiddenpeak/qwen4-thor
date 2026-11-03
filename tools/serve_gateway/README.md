# TLS/API Key 网关

用户已确认用于受控内网多个客户端。本轮开发/验收范围见
[SRV-10a合同](../../docs/SERVE_GATEWAY_2026-11-03.md)，实际部署接受以
[STATUS](../../docs/STATUS.md)为准；prepare成功不代表业务已验收。
网关依赖固定nginx1.30.6（含SSL模块），当前验证包仅在工作目录解包，
没有系统安装、timer、自动重启或内网监听。

持续运行入口已补齐[托管网关](MANAGED.md)：固定包内源码、每秒核对
依赖身份、正常排空/异常回收、每代次8.5MiB有界日志与systemd模板。
本机真实模型4类生命周期已通过，实际LAN配置和常驻安装仍待交接。
下面保留基础配置与底层prepare/check/run说明。

2026-11-04已完成[本机TLS与双客户端短验](../../docs/SERVE_GATEWAY_RESULT_2026-11-04.md)：
普通/严格输出、真实工具与引用回填、限额、取消、排队及恢复通过。
首轮取消路径错接及修复前失败保持；修复已与正式API合同交叉核对。
已有验收目录不覆盖、不重复启动模型；核验原件使用只读入口：

```bash
python3 -B tools/serve_gateway/report.py verify-archive
```

准备私有JSON policy（0600），字段示意如下。示例Key是占位符，
不能直接使用；每个客户端用安全随机源生成独立32字节以上的随机Key，
例如Python `secrets.token_urlsafe(32)`，通过受控渠道分发。

```json
{
  "version": 1,
  "listen_host": "127.0.0.1",
  "listen_port": 18443,
  "server_name": "q4t.internal.example",
  "upstream_port": 18123,
  "clients": [
    {"id": "client_a", "api_key": "REPLACE_WITH_RANDOM_CLIENT_A_KEY"},
    {"id": "client_b", "api_key": "REPLACE_WITH_RANDOM_CLIENT_B_KEY"}
  ],
  "per_client_inflight": 2,
  "global_inflight": 8,
  "per_client_rps": 5,
  "burst": 10,
  "body_bytes": 16777216,
  "upstream_idle_seconds": 1300
}
```

证书需匹配server_name、至少还有1小时有效期；私钥0600，支持路径字符
为ASCII字母数字、斜杠、点、短横线及下划线。客户端必须校验CA与域名，
禁止用关闭TLS验证作为部署方法。测试自签证书仅适用于本机夹具。

```bash
python3 -B tools/serve_gateway/gateway.py prepare \
  --policy .q4t-work/gateway-policy-private.json \
  --nginx .q4t-work/serve-gateway-20261103/vendor/root/usr/sbin/nginx \
  --certificate .q4t-work/certs/server.pem \
  --key .q4t-work/certs/server-key.pem \
  --output .q4t-work/gateway-package-001
```

prepare生成私有配置/证书副本及manifest摘要并真实执行nginx -t，不
加载模型或监听。原封包不覆盖；错误诊断含潜在私有内容，留在包内。
配置会包含完整API Key，禁止把nginx -T、policy、nginx.conf或私钥
贴入公共日志/证据。manifest不包含Key明文，但仍作为受控部署记录。

后端需先按已接受托管包运行，只有实际READY、固定ELF、同网络空间、
loopback端口/身份匹配才能经生产入口启动网关：

```bash
python3 -B tools/serve_gateway/gateway.py check \
  --package .q4t-work/gateway-package-001 --manifest-sha256 PREPARE返回摘要
python3 -B tools/serve_gateway/gateway.py run \
  --package .q4t-work/gateway-package-001 --manifest-sha256 PREPARE返回摘要 \
  --backend-run .q4t-work/managed/run-001
```

run前台执行nginx，没有后台自启动。通过拥有该前台进程的终端/服务
管理器停止；不要按遗留PID文件批量kill。持续运行使用
[managed入口](MANAGED.md)拥有进程及有界日志，结合
[既有运维留存策略](../../docs/SERVE_OPERATIONS_2026-11-03.md)。
底层run的stdout仍需外部收集。凭据轮换：停止/
排空网关，生成不含旧key的新包，再显式run；在途请求不作自动重放。

允许GET `/v1/models`、`/v1/capabilities`，POST `/v1/chat/completions`、
`/v1/chat/preflight`及`/v1/requests/cancel`。prepare/check交叉核对正式
OpenAPI定义并绑定其SHA，避免代理与夹具共享错误路由。管理/监控/健康
端点留在后端loopback。data与control
并发/速率分开，429附Retry-After；没有token计费或总任务幂等语义。
网关error.code以gateway_开头，响应另有X-Q4T-Gateway-Request-Id；
后端X-Request-Id和原始错误不被改写。proxy关闭响应缓存/缓冲与重试，
客户端断连转至上游；SSE EOF缺终态不能当成功或自动重做工具。

外部接入必须保持后端loopback，只开放指定TLS入口并限定实际来源。
同机用户属于可信运维边界，可以直接访问loopback；需要隔离不可信
本机租户时必须另行部署网络/用户边界。启动身份检查不代表持续后端
身份监控。限制连接只计完整头后请求，慢头与TLS握手不计每client额度，
仍受总连接/超时限制；本次不承诺完全DoS防护或任意代理性能。
