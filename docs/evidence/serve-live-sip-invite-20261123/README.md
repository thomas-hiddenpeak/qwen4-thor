# 来电头字段与私有媒体 offer

Thor 上真实 TLS/SIP 对端分片发送 INVITE。所有者取得的字段与入站字节逐项匹配，包括原 Via 的 rport、大小写、重复名、顺序及折叠值。Sofia NTA 会在事务处理中补写 Via，因此在头部解析完毕、进入事务层之前保存原字段。解析器整项移除 Authorization 与 Proxy-Authorization，包括大小写变体、冒号前空白及续行。[9 项回环结果](peer-tests.txt)。

来电私有通知为 `INCOMING <scope> <live_id>\n<字段行>`。媒体正文独立保存。所有者通过 `OFFER <request_id> <scope> <live_id>` 读取当时的 SDP；跨身份收到 not_found，已提交拒接收到 conflict，无 SDP 的来电返回空正文。带正文且 MIME 非 application/sdp 的请求收到 415。来电头字段仍是对端声明，仅用于业务通知。

模拟所有者停止读取，连续发送含 32 KiB 以上头字段的独立来电。达到通知队列的准入水位后，电话端收到 503；恢复读取后逐笔核对已接纳通知，原来电拒接、ACK 和后续新来电均通过。队列的总上限为 512 KiB、512 包，准入在半数水位停止，为终态回执保留空间。对端永久失联后待处理来电走原终止路径。

构建采用 `-Wall -Wextra -Werror`。测试命令在仓库根目录设置 `Q4T_SIP_WORKER` 为该构建的绝对路径，执行 `python3 -B -m unittest -v tools.serve_live.test_sip_trunk`。当前证据覆盖私有信令进程；签名 HTTPS 通知、公开操作与接通后的媒体仍需独立验收。
