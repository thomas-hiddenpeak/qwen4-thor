# SRV-10a TLS与独立客户端入口

用户已确认受控内网多个客户端，经TLS网关与独立API Key接入；长时间
验证由用户开展，agent完成开发及短验。本轮补独立网关，保持默认
runner/已接受托管包与SRV-18用户day计划身份，不修改其冻结源码。
本轮只监听127.0.0.1进行短验，实际内网地址/证书/客户端部署另交接。

## 冻结边界

采用官方nginx稳定版1.30.6 ARM64 Noble包，下载到.q4t-work，核验官方
签名InRelease、Packages及deb SHA，解包不安装、不运行maintainer脚本。
[官方分发](https://nginx.org/en/linux_packages.html)与
[安全公告](https://nginx.org/en/security_advisories.html)在本日核对；
这是本轮固定版本，后续部署需维护升级，不能据此宣称永久无漏洞。

生产入口为`tools/serve_gateway/gateway.py prepare/check/run`。prepare
只生成0700目录/0600文件，严格拒绝未知/重复字段、无效域名/地址、
重复客户端/凭据、过大配额及路径注入；核对证书/key配对、域名/有效期，
执行nginx -t。run先核对包、工具与证书身份，要求实际READY托管后端
及同namespace/固定ELF/loopback监听，再前台运行；没有timer或自启动。

- 仅支持TLS1.2/1.3，关闭tickets/early-data；显式loopback或RFC1918
  地址，拒绝wildcard/公网地址。域名与SNI均核对，不提供明文转发。
- API Key逐字节区分大小写、Bearer scheme允许字母大小写；1..64个
  客户端，每key32..128个URL-safe字符。生产key须用安全随机源生成。
  [nginx map普通字符串不区分大小写](https://nginx.org/en/docs/http/ngx_http_map_module.html)，
  因此使用锚定且区分大小写的正则，限制key字符集避免注入。
- 客户端仅允许GET models/capabilities及POST chat completions/
  preflight/cancel；严格原始路径/方法组合，不接收query/路径别名。
  config/metrics/健康端点由loopback运维独立访问。Authorization、
  cookie、转发身份头不传入runner；请求体与合法X-Request-Id保留。
- 完整请求头后，按已认证client限并发/速率；data默认每client2/
  全局8，preflight计入data。cancel及发现接口独立control每client4/
  全局8，rate独立；429附Retry-After。匿名随机key不占配额键空间。
  [limit_conn口径](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html)
  不覆盖慢头/TLS握手，worker连接256及头/体idle timeout另有限制；
  不声称能抵御全部DoS或提供公平token配额。
- [代理](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)关闭
  响应buffer/cache/自动重试，客户端断开向上游传播；请求体先有界
  buffering，默认16MiB，可能使用私有临时文件。默认上游idle1300秒，
  大于当前runner默认1200秒request deadline；idle超时不是总墙钟。
- 网关自身拒绝有固定JSON error/code与独立关联ID；上游错误与SSE原样
  转发，不补造DONE或重放工具。日志仅client/lane/status/字节/耗时/
  限流结果，不记key、URL、请求体或调用参数；原始诊断仍视为私有。
  stdout需由部署的有界日志策略承接，prepare不安装日志服务。

本机同用户/管理员属于可信运维边界，loopback不是同机租户鉴权。
启动时检查后端身份不等于持续阻止可信本机进程抢占端口。外部绕过
证明需结合实际网络部署；本批不开放内网端口或更新主机防火墙。
Key轮换采用显式停止/排空后生成新包再运行，旧包不覆盖；不宣称已
支持在线撤销在途请求、零停机换key、按token计费或租户数据存储隔离。

## 成组验收

先完成配置/包/证书CPU检查，固定来源后真实本机TLS传输夹具覆盖：
正确/缺失/错误/大小写key、重复Authorization、路径/方法/管理绕过、
体上限、错误证书/主机名、SSE立即转发/断连、两client配额与控制通道、
限速、上游断链不重试、停机换key。该后端为明确stub，无模型语义结论。
所有监听HTTP/HTTPS夹具按项目约定在单独GPU lease内，最长240秒，
最多4次nginx启动、96个客户端HTTP尝试，不自动重试或扩样。

传输通过后再复用已接受v3包160796c3，在完整同一GPU lease内做一次
真实模型短验：普通JSON/SSE2、strict1、真实资料工具及回填2、活跃SSE
取消1、另一client排队成功1、取消后恢复1，合计8生成（7成功/1预期
取消）。另2拒绝：无key与同client并发上限；控制≤96，模型启动1次，
GPU≤600秒/总≤720秒。按原件核对proxy不泄漏key、同后端资源回收、
正式停止后GPU/锁释放，只清理本阶段进程。

只执行短验，不启动SRV-18的day。失败保留原件，修复具体问题后只
重验受影响项；已经通过且身份不变的模型数值/直接SDK/CPU证据复用。
通过后交付包准备与运行/撤销说明、真实证据、状态/日志及阶段提交。
实际LAN客户端、真实证书信任/防火墙、常驻日志/通知/多人业务策略、
物理RAM硬保护与长期运行仍需单独证明，工业化目标继续active。
