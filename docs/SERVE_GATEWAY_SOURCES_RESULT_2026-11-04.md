# SRV-10g 来源网段与真实业务短验限定接受

2026-11-04，生产实现冻结于`e69640bd`。按[合同](SERVE_GATEWAY_SOURCES_2026-11-04.md)
完成开发和一次本机短验；结果为`ACCEPT_BOUNDED_GATEWAY_SOURCES`。
新policy v2要求显式来源CIDR，socket peer默认拒绝、独立Key继续认证；
伪造转发头不能授权。新建内网监听拒绝无CIDR的v1，旧固定包保持。

## 证据与实际边界

| 检查 | 结果 |
|---|---|
| CPU | 78有效项，含6驱动；策略/规范CIDR/真实nginx -t/包和旧入口/TLS观察器兼容通过 |
| 来源拒绝 | 16项全部403；真实peer 127.0.0.2与127.0.0.3，无上游状态或后端request ID |
| 允许来源 | 127.0.0.1携带伪造拒绝网段头仍可发现；缺失/错误Key为401，管理路由404，beta超预算400 |
| 真实业务 | alpha512/beta64各1查询+1回答，4个完整SSE、2个真实资料工具，architecture与引用正确 |
| 资源与身份 | 1模型/1网关，2自有身份退出，grammar4/4、零在途，默认/222运行来源/原Key/旧包保持 |
| 独占与时长 | GPU54.664095秒、52观察无竞争；驱动52.993340秒，7个chat尝试中3个入口拒绝、4个实际生成，22控制 |
| 结束状态 | GPU空、原锁66305:3294067释放、18131/18451无监听、容器空、SOAR masked/inactive/dead |

合计25个TLS请求。16来源拒绝覆盖5公开路由、X-Forwarded-For/
X-Real-IP/Forwarded及组合、缺失/错误Key、管理路由、坏JSON、只发头
但声明65536字节body、错误Host与第二个拒绝peer。每项保留实际
socket来源、TLS协议/leaf验证、响应及关联ID；前后backend chat/
grammar计数保持0。查询读取真实`docs/MODEL.md:L1-L24`，正文SHA与
冻结文档核对；两个客户端各输出55/58 token，prompt为564/890。

没有启动实际LAN监听；RFC1918仅检查规范校验与配置生成。TCP来源
测试不能代替实际远端接入、防火墙绕过和NAT/代理拓扑检查。新托管
包内层配置实际运行，包准备/固定入口兼容已CPU验证，未重新执行
托管生命周期；不将它记作新systemd常驻部署验收。独占轮询不能排除
短于采样间隔的干扰。不构成性能优化、全模型数值或物理RAM硬限额结论。

## 保留的失败与定向修正

1. 首轮CPU78项中1项使用本机未配置的10.1.2.3执行nginx -t。
   nginx语法通过后bind报EADDRNOTAVAIL，原私有诊断已保留；改为
   loopback实际-t加LAN配置生成检查，复验1项及调整门禁后的6驱动。
   未修改生产网关或主机地址，没有把该原始失败写成通过。
2. 实机21探针、4响应/2工具和正常停止均已完成，最后的日志断言
   把空upstream_status误判为已转发。原驱动FAILED、guard
   **COMMAND_FAILED/child_returncode=1保持原样**。原始25条nginx记录
   中来源/Key/路由拒绝的upstream为空；正常发现和4生成是200，预算
   拒绝是400。离线修正允许空串或缺省标记，并用真实记录做3项回归：
   无上游接受；有真实上游状态、缺记录/重复ID/错误来源标记必须拒绝。
   业务接受来自原始HTTP、SSE、工具、metrics、日志和清理证据重建，
   不是把guard终态改成COMPLETED；没有第二次模型加载或HTTP补测。
3. 首次离线归档还预期日志断言成功后才生成的gateway-rows.json，
   因该派生文件不存在而失败。原KeyError、报告源码保留，改为直接
   重建原始nginx日志，不补造原执行产物；无追加推理。

三份用户day计划均保持且未执行。小时/天级长稳仍交由用户。

## 交付与复核

生产入口为[网关配置](../tools/serve_gateway/README.md)与
[托管入口](../tools/serve_gateway/MANAGED.md)。新准备包沿用内层v3/
托管v4，policy为v2；它不会覆盖旧包或自动启动服务。来源变更按显式
排空、准备新包、启动交接，实际LAN配置/证书/防火墙仍需单独验收。

本机验收包：`.q4t-work/serve-gateway-sources-20261104/gateway/`，manifest
`e7bd54a7217cd5fe9dc90f6b49ff25287cffb4aacbd1a309bbc60330ff5d2e8a`。
证书和loopback CIDR只是短验夹具；不能原样部署到真实内网。
默认ELF保持`49cc78cd`、sidecar`055f4f3e`，旧固定包/回退材料未变。
[独立交付回执](evidence/serve-gateway-sources-20261104/delivery.json)
绑定包、接受结果与未部署边界，不修改原包manifest或原guard记录。

[558项原件与4个本机二进制身份](evidence/serve-gateway-sources-20261104/manifest.json)
封存并离线重建通过，压缩1,230,399字节。archive SHA256为
`494582f5d4cea8ca71d287827540210208ea65590bc0721131718c0e615bc89d`；
[结果](evidence/serve-gateway-sources-20261104/result.json) SHA256为
`d9dda35746d54bc5f34ab93cfa6eb3350e11789e0452cfb717c8b1d62a62f88c`。
不归档Key/私钥/nginx私有配置；来源快照、失败、响应与公开证书保留。

```bash
python3 -B tools/serve_gateway/sources_report.py verify
```

该入口不加载模型、不发HTTP。实际LAN部署、正式运维/容量/备份介质、
完整物理RAM保护、源盘丢失灾备与用户长验仍在工业化待办中，目标active。
