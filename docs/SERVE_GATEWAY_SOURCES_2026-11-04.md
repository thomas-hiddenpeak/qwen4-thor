# SRV-10g 显式客户端来源边界

2026-11-04冻结，业务接入/可靠性开发，不是性能优化。用户已选择受控
内网、多客户端TLS和独立Key；实际地址、网段、DNS/CA与防火墙策略未
提供，本阶段仅开发并做本机短验，长稳由用户开展。

## 合同

- 新policy v2必填`allowed_client_cidrs`：1至32个规范IPv4 CIDR，
  必须完全位于RFC1918或127/8，拒绝主机位、非规范写法、重复、重叠、
  默认路由、公网/链路本地/组播/IPv6与未知字段。每个来源网段都需
  显式填写，不默认放行loopback。列表全局适用于所有独立Key；不代表
  每Key网段绑定或网络地址就是用户身份。
- nginx `geo $remote_addr`默认拒绝，以TCP peer决定。未启用geo proxy、
  realip信任或PROXY protocol；`X-Forwarded-For`、`X-Real-IP`和
  `Forwarded`均不能授权。来源检查在应用Host/SNI、Key和路由判断
  之前；合规HTTP请求拒绝为403 `gateway_source_denied`、固定JSON、
  request ID、no-store，无上游调用。HTTP解析错误可能更早被nginx
  拒绝；TLS握手/慢头不属于此应用来源检查的防护范围。
- 现有v1可解析，新prepare/render仅允许v1监听127.0.0.1；新建RFC1918
  监听必须用v2。旧包使用其固定旧入口，绝不原地改配置。包格式仍是
  内层v3/托管v4，新增策略随generator/source SHA封包；无新运行依赖。
- v2日志增加有限`source_allowed`（0/1），不记录IP、凭据、URL或body。
  后端仍loopback，入口仍需有效Key，quota/control/data/TLS/流式与
  断连语义保持。变更网段必须停止排空、准备新包、显式启动，不热改
  在途请求。真实部署仍需主机防火墙与远端绕过检查；NAT/上游代理会
  改变实际peer，不能通过伪造头“修复”，应另行审查网络拓扑。

依据：[nginx geo](https://nginx.org/en/docs/http/ngx_http_geo_module.html)
支持按客户端地址/CIDR决定变量，配置proxy才会信任转发来源；
[realip](https://nginx.org/en/docs/http/ngx_http_realip_module.html)
会按显式信任改写地址。本实现不配置这些信任入口。

## 冻结验收

先完成CPU配置/包/实际nginx -t/TLS来源观察兼容及驱动失败清理，
不监听。保留原CPU记录；仅失败项允许具体修正后定向重验。
检查旧固定托管入口仍可只读生成Restart=no的unit，不启动它。

固定提交和全部依赖后，一个完整GPU lease中只启动1个当前默认模型、
1个新policy v2 TLS网关（只监听127.0.0.1）。直接启动已校验托管包的
内层配置；新的托管启动/排空生命周期未重新验证，复用10b/10e来源
不变的行为证据，不冒充实际LAN或systemd常驻部署。

21项TLS探针：16个来源拒绝（真实绑定127.0.0.2及127.0.0.3），覆盖5
公开路由、3种伪造头及组合、无/坏Key、管理路由、坏JSON、声明body
却只发送头、错误Host；5个允许来源探针覆盖伪造拒绝地址仍可发现、
无/坏Key的401、管理路由404、beta输出预算65拒绝400。记录真实
socket来源、CA/域名验证、leaf、返回体与关联ID，403必须无上游ID，
网关日志无upstream且前后后端chat/grammar计数保持0。

alpha512/beta64各执行1次真实`docs/MODEL.md`查询与最终回答，合计
4个完整SSE响应、2次只读工具，准确architecture和引用必须正确。
这些允许请求同时携带伪造拒绝网段头；成功仍以真实peer判断，Key
和预算独立生效。合计25个TLS请求，含3个拒绝chat尝试+4个实际生成；
最多7 generation_attempts、80控制、1模型启动、240秒整段。195秒
触发停止，预留清理；失败不自动重试、不补模型。最后核对日志25项、
grammar4/4、零在途、所有自有进程退出、GPU/原锁/两个监听释放。

默认ELF/sidecar、222模型运行来源、原Key/旧固定包和三份day计划保持；
不恢复SOAR、不自动启动服务、不改变主机防火墙、不发送外部通知。
通过后封存可重建原件、更新交付说明、阶段提交推送；实际LAN接入、
正式容量/运维/备份策略、物理RAM边界、源盘丢失灾备及用户长验仍待。
