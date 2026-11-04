# SRV-11j：持久任务客户端全程网络截止时间

2026-11-04。源码审计发现`transport.Client.request`在发起请求时建立
总截止时间，但仅`DeadlineReader`正文读取检查它。TCP/TLS、写入及
`HTTPResponse.begin`的状态行/响应头仍使用socket空闲超时；缓慢持续
传输可延长占用。chunk尺寸行和trailer也由库内部循环读取，正文外层
检查不能覆盖每次底层IO。这是已支持应用服务的可靠性缺陷。

## 冻结变更

- 每次请求使用一个monotonic截止时间；连接、TLS、分段发送、所有
  socket文件读取共用剩余时间，不因进度、100响应或chunk边界重置。
- 保留stdlib HTTP解析及SocketIO文件引用生命周期，支持JSON/SSE、
  Content-Length、chunked与Connection close。没有后台计时线程，
  没有自动重试、重定向、代理或新增信任源。
- 超时不能发布成功结果；生成已发出但未完整保存时维持REQUEST_UNKNOWN，
  不执行工具、不自动重发。预检失败不消耗生成预算，按既有规则显式恢复。
- 登记上一版SRV-11i的17项精确来源，允许显式应用源码迁移；旧历史、
  预算、协议审核、Key/CA与永久键保持。模型/模板/默认ELF不改。
- 网络期限不承诺Python代码或OS调度的实时抢占；本机CPU耗时若超过
  截止时间，返回前拒绝成功，不能用这项保护代替CPU/进程资源限额。

## 有界验证

先保存原17文件和既有278原件归档身份；CPU定向复现原缺陷后成组修复。
覆盖缓慢头/状态、连接与TLS累计、部分写入、100响应、chunk尺寸与尾部、
正文/错误正文、EOF和引用回收，以及任务UNKNOWN/已有结果不重放。

真实HTTP/TLS与模型验收在同一绝对路径gpu_exclusive入口内完成；
至多12个受控网络探针、1次模型启动、4个生成请求、2个真实资料工具，
总驱动240秒，GPU租约210秒。先核对空闲与原锁，发生竞争只清理自身。
不启动长验，不安装常驻服务，不修改模型、reference、旧Key/包/day。
正常业务记录双客户端512/64预算、答案/引用、wire和资源清理。

当前为开发合同，不宣称修复或真实验收已通过。原失败记录保持；只有
明确缺陷修正才重验受影响项，不能用反复延长预算取得通过。

## 执行前CPU记录

原慢头缺陷确定性复现：250ms预算到380ms才退出。成组16项有14通过，
两项夹具失败保持。成功正文夹具仅读10字节，未走完整慢正文，修正为
完整读取。原CA竞态夹具未观察到拒绝，原轮未记录前后stat，不能事后
确定失败时序；独立核对观察到4ms的mtime/ctime变化并正确拒绝。
定向夹具改为显式推进mtime，确保注入可观察变化，不修改TLS生产逻辑。
仅复验这两项通过，14项未变证据复用；底层截止时间生产代码保持。
另2入口检查通过：guard/worker绝对路径、无租约在任何HTTP/写收据前拒绝。

Python3.12.3本机源码核对：HTTPResponse通过SocketIO保留连接文件引用；
SSLSocket.sendall循环调用send。因此新实现保留原SocketIO，并在每次
底层readinto/send前更新剩余时间。标准接口参考
[socket文件与超时](https://docs.python.org/3.12/library/socket.html#socket.socket.makefile)、
[TLS握手](https://docs.python.org/3.12/library/ssl.html#ssl.SSLSocket.do_handshake)。
这不承诺跨Python版本兼容，运行依赖随本轮来源冻结。
