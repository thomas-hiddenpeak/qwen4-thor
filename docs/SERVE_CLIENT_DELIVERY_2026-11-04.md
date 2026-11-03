# SRV-10c默认交付与回退范围

依据用户已授权的阶段收尾，在客户端隔离组合短验接受后，将已验
7619966e替换默认a479d8ab，并同步sidecar。这里只安装同一已验ELF，
不增加模型/HTTP；CLI健康及精确ELF的已有真实TLS/工具/cache证据复用。
交付阶段同一GPU租约，固定600秒；先验证旧pair/缓存/来源，复制
candidate/previous与构建身份，保留独立回退包，再使用既有原子单文件
替换及持久恢复marker协议。两个文件不是跨文件掉电原子事务。

入口：`tools/serve_clients/delivery.py prepare/check`，经
`tools/evalscope/gpu_exclusive.py --output
.q4t-work/serve-clients-20261104/delivery/lease -- /usr/bin/python3 -B
tools/serve_clients/delivery.py execute`交付，guard完成后finalize。
失败仅接受已知旧/新身份并回退，未知身份拒绝覆盖。手动回退需要
新的GPU租约并调用同工具rollback --output，原delivery证据不覆盖。

回退pair位于`.q4t-work/serve-clients-20261104/delivery-package/previous/`。
实际结果在delivery/finalized.json，未出现DELIVERED前不能记默认已交付。
policy仍是显式启动选项；不安装常驻服务，不开放LAN，不配置实际
客户端密钥/CA/DNS/防火墙。已有两份用户day计划及旧包原样保留，
不会自动把旧计划解释成新ELF长验；新托管包E2E与新长验交接独立。
