# SRV-08b 客户端可靠性收尾结果

2026-11-05，限定接受当前 Python 与固定 Node 24.18.0 / openai 7.47.0
客户端；原生默认 a56bd448 / 914e067f、协议 cc321239 不变。

Python 从 DNS、连接、TLS、请求写入至响应头/体共享单调时钟截止，
超时取消并回收解析子进程；拒绝 Content-Length 和 chunked 截断。
Python/JS 与 Python→Node 桥可显式指定 api_key/apiKey、ca_file/caFile、
connect_host/connectHost；IP 覆盖保留 Host/SNI 和证书主机名检查。
仍不隐式使用代理、不跟随重定向、不自动重试。JS CA 限定为有界正常文件。
用户回调/CPU 工作不被抢占，返回成功前仍复查期限；不声称硬实时。

首轮纯 CPU 83 Python、40 Node 全通过；共享解析/工具定义25项、223
原生运行来源均保持。真实 TLS 覆盖两语言34场景。两次夹具失败原件保留：
首次未读完 POST 就关闭；第二次正常关闭后的 TIME_WAIT 影响复绑检查。
分别补请求体排空与原监听 SO_REUSEADDR，未修改客户端或重跑模型。
复用8个已完成的慢响应观察、补齐26项，累计36网络尝试。

唯一模型启动产生6成功响应和2次真实只读资料工具；Python普通JSON、
工具JSON→最终SSE，Node普通SSE、工具SSE→最终JSON。最终回答包含
Qwen4ExpForConditionalGeneration 及 [docs/MODEL.md:L1-L24] 引用。
双Key与错误Key拒绝通过。累计工作63.026秒（含此前失败阶段；不含阶段间
人工修复时间），最终租约COMPLETED；模型/nginx/监听退出，GPU空、
原锁inode3294067可用。不将三个租约合写为首次全流程通过。

6412原件/来源归档已由独立进程逐字节核对；完整6响应离线重放一致。
Key/私钥未入归档。证据：[结果](evidence/serve-client-transport-20261105/result.json)、
[清单](evidence/serve-client-transport-20261105/manifest.json)、
[原件](evidence/serve-client-transport-20261105/records.tar.gz)，归档SHA256
`1d4cd7f4f47848e3a35b7694085f963834bfbd18450348070eced43abc3885a5`。

应用来源补到18文件，但本阶段尚未登记新的持久任务来源/协议迁移；
旧任务必须保持原身份或随后显式迁移。托管包仍按旧接受身份使用，
当前默认的托管短验随后独立执行。长稳、真实内网部署、配额公平与
整机物理RAM保护不在本次接受范围。
