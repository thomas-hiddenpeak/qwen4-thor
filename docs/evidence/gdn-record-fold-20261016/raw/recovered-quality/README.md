# 质量中断证据恢复：0 条可复用，11 条仍缺验收证据

仅在新的 db-recovery/ 对原 DB 与同名 journal 的副本调用 SQLite；
原件没有打开为可写，db-pristine/ 保留复制时的原始字节。
integrity_check=ok，result 表仍为 0 行。客户端目录只有该 DB/journal
和配置/进度日志，没有其他逐请求响应备份。

客户端配置 db_commit_interval=1000；保存的 evalscope 源码显示逐请求
INSERT 后到阈值或整组完成才 commit。这与中断后未留下持久行相符。
不能从 journal 或 serverlog 构造原 HTTP/SSE 响应、usage、finish 或时间。

server.log 可观察 auto-0..9 的 request_end 和 auto-10 的取消：
11 次服务侧 attempted、10 次 observed completed、1 次 cancelled。
这些只描述实际发生的服务请求，不能算作通过的质量题；不写成0请求。
本轮未进入任何 performance 组。

results.json=[]；validation.json 明确 response_identity、request_timing、
质量与 reference 检查没有可验证的行，逐条早于竞争启动/abort也无法证明。
inputs/ 是原11题完整缺失集合，manifest、requests逐字复用旧fixture，
另逐条核对 prompt SHA。没有改题、扩大正式矩阵或假定恢复10条。

下一正式矩阵仍为 on质量11 → on五档×3 → 通过门禁才off质量11 →
off五档×3，最多52正式请求。此前环境中断的11次attempt单独保留，
不计为正式通过。source dcb2a7d与候选binary不变；没有启动HTTP或GPU。
