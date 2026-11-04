# SRV-19：发布声明一致性限定交付

2026-11-04。修正默认sidecar，ELF、222份运行来源、模型协议和此前
实际业务行为保持。新增声明校验用于新包准备和带声明扩展的pair交付。
本项为CPU/元数据验收，无新增模型、HTTP、GPU测试或长稳运行。
冻结初稿/首条日志及封存报告注释曾误写SRV-17，与失联核验重号；本项
正式编号SRV-19，已封存原件与历史日志不回写，不改变测试身份。

| 身份 | SHA-256 |
|---|---|
| ELF，保持 | `7eff020de66b96f3175b3e123909be9e2f58142d54d1dd5992750d23cbe9c769` |
| 旧sidecar，已保留 | `abfaeff90bf460f1ff0cf0ee1f32da70d186e5ada0ab735e4576fc0d51c78e53` |
| 新默认sidecar | `cc83bf3a36c2b9658221554f39a7e6bb807f2618cb37b87b2f19fd7d7ea91d04` |
| 新准备v6包manifest | `2c142bdd197206ad9faf025ed9ff57fb1f00cc804a0fc260287c1a87416b0823` |

实际修正四组声明：观测v5及双监听指标、HTTP连接profile v2/独立运维
入口、host内存错误、结构化输出终态清理等待。config/preflight/response
三个schema摘要更新，七份schema全部指向冻结副本。保留原业务接受
记录和范围，完整capabilities/config快照不等于声明所有能力均已验收。

新`q4t-release-declarations-v1`绑定ELF、协议、完整实际响应及九文件
摘要；检查双方共有字段和严格JSON类型，包内复制资料，校验不依赖
原目录。新prepare缺声明或不一致在输出创建前拒绝；旧已封存v1..v6
包无扩展时继续按原规则核对。该信任来自本地接受流程，摘要不是签名，
无法防止操作者绕过工具或自行重签一整套虚假证据。

62项唯一CPU回归全部通过、零失败/跳过，包含11声明、9基础包、10资源、
8客户端、2运维包、22交付测试；其中交付故障输出是CPU替身。七schema
合法性、已封存真实配置响应验证、真实旧sidecar拒绝以及原v6包只读
核对通过。额外隔离校验首次直接runpy遗漏包根路径，报ImportError；
记录保留，随后使用正式包内service入口生成unit文本通过。未安装unit，
未重跑CPU套件，未因此修改生产代码。新包仅准备/静态校验，其新的
Python包装不冒充12h原包的真实托管接受。

交付前完整旧pair/cache/接受记录保存在
`.q4t-work/serve-release-declarations-20261104/previous/`；原共享锁
dev66305/inode3294067下核对旧摘要后原子替换sidecar，CLI version一次
通过。锁文本和inode保持，末次GPU进程/容器均空，恢复服务masked/
inactive/MainPID0，flock可取得。没有恢复或新装常驻服务，旧包、Key及
三份day计划保持且未执行。历史12g/12h记录的旧sidecar保持其当时口径。

[证据清单](evidence/serve-release-declarations-20261104/manifest.json)、
[机器结果](evidence/serve-release-declarations-20261104/result.json)和
[62份原件归档](evidence/serve-release-declarations-20261104/records.tar.gz)
（153113字节，SHA `45b0fdb2fcfd04e9dd8624cfd4e60ed20dc124d3fe27bfd07429a72be61bcbae`）
已独立核对。归档不含ELF和私有网关token文件，两份ELF及凭据文件
仅在本机以身份记录保留。复核入口：
`python3 -I -B tools/serve_release_declarations/report.py verify`。

按用户本轮补充，高级接入/资源/通知配置按实际执行选择启用；未提供
正式部署参数不阻塞基础开发，启用时仍验对应合同。长期运行交由用户。
持续工业化目标active；下一步独立审核fc63db28→ddfba8c0任务协议迁移。
