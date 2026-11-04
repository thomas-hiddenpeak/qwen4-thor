# SRV-10e：TLS材料校验与网关证书生命周期

2026-11-04，业务可靠性推进。承接已确认的受控内网、多客户端、TLS
网关和独立Key范围；实际DNS/CIDR/CA仍待用户提供，本批只做本机验证。

## 代码发现与冻结范围

当前gateway.certificate_check只验证密钥配对、checkend 3600和主机名。
notBefore、链中其他证书时间与服务器用途没有覆盖；managed循环只读
文件哈希及监听身份，证书自然过期不会改变READY。准备成功和监听
存在不能证明客户端仍能完成TLS。

1. 新准备的网关记录完整有界PEM链的有效期交集、leaf指纹和固定
   校验工具身份；密钥配对、主机名、服务器用途、notBefore/notAfter
   及启动至少一小时剩余时间均验证。验证所复制的字节，避免原输入
   重读与复制内容不一致。私钥不进入公开manifest/诊断。
2. 显式`--trusted-ca`支持一个固定CA锚点，使用OpenSSL严格验证链/
   签名、用途和主机名，禁用环境/默认CA路径补全。未提供时明确
   `not_configured`，只声明材料/时间检查，不假装客户端信任已证明。
   证书撤销、在线OCSP/CRL、ACME签发与任意企业信任库不自动加入。
3. 新内层包v3、托管包v4固定TLS检查源；旧包及day原件保持，旧工具
   仍通过各自固定入口使用，不原位升级。每秒检查已绑定证书有效期，
   私有status区分VALID/EXPIRING/NOT_YET_VALID/EXPIRED/UNKNOWN。
   到期前七天显示预警；失效时有界QUIT排空，超时回收自己的进程组，
   终态FAILED带固定原因。绝不停止后端、自动重启或重放请求。
4. 显式同CA换证采用新包/新run：先排空旧网关，再启动新网关；私有
   CA和业务Key不变。允许有界接入间隙，不声称无损热重载。真实客户端
   核对leaf指纹、CA和主机名后完成业务。CA更换及任务绑定迁移另处理。
5. 顺带修复该监督器已确认的早期child身份失败时logger未建立的
   清理路径，使用真实子进程验证；不把清理未知记为成功。

## 有界验收

先成组CPU：真实OpenSSL证书、SSL MemoryBIO握手（无监听/HTTP）、
链缺失/错误CA/错误用途/未来和过期、边界、包完整性与旧入口、实际
子进程排空/超时/早期失败。时间跨越使用明确标注的时钟接缝，不能
冒充实际等待到期；不改系统时钟。未变模型/保护证据直接复用。

随后最多1模型、2个顺序TLS网关、4成功生成/2真实资料工具、4次
有界TLS检查；同一受管后端guard持完整阶段，最多240秒/GPU210秒/
64父控制。同CA两个不同leaf均实际握手和资料工具，后端不重载；
验证旧网关退场、新证书实际生效、独立客户端预算、所有自有资源清理。
必要定向修正保留原件，只补受影响项，不用反复推理代替定位。

本批不接外网、不部署LAN、不发送通知、不运行用户长验。此处私有
TLS状态尚不是Prometheus告警投递；正式采集及到期运维仍需后续接入。

依据：[OpenSSL有效期/用途验证](https://docs.openssl.org/3.0/man1/openssl-verification-options/)、
[x509 checkend语义](https://docs.openssl.org/3.0/man1/openssl-x509/)、
[nginx证书链顺序](https://nginx.org/en/docs/http/ngx_http_ssl_module.html#ssl_certificate)。
