# SRV-08b：客户端网络截止与可选连接配置

2026-11-04，起点92129c71，默认8e9b4ce7/e61bed7c、协议a56aed21。
可靠性及客户端接入任务，无性能优化声明。高级配置按执行时选择。
本机实现尚未验收，不纳入本次已验Serve基础版本的合入范围。

冻结范围：标准库ChatClient为同一请求建立单调时钟预算，覆盖有界
DNS解析、TCP/TLS、请求写入、响应头/体/错误正文和EOF；持续来字节
不重置预算。保留urllib调用接口与HTTP解析、禁止环境代理/重定向/
请求自动重试；可选总网络上限、私有CA和连接IP，Host/SNI及证书名称
仍按URL目标检查。域名解析在可回收子进程内，数值IP直接连接；
清理有界，不能把同步用户sink/CPU解析或OS调度称为可抢占的实时操作。
读取和交出结果前后检查截止，超时不提交成功，也不自动重放工具。

JS辅助类与Python→Node桥补显式apiKey、CA材料及连接IP，保留固定
Node24.18.0/openai7.47.0、零重试、完整wire交叉核对和原生总截止。
使用Node内置HTTP(S) transport，无新增包下载；显式CA不关闭证书/
主机名验证，不能和自定义fetch静默混用。凭据不进入argv/诊断输出；
默认本机直连不要求Key/CA，不自动安装TLS网关或修改旧配置。

生产runner、模型、模板、默认ELF/sidecar、已封存包、Key和三day计划
保持。持久任务独立TLS transport不重构、不自动登记新应用来源或
协议迁移；共享parser虽语义不变，源码身份变化仍需显式审核。

先冻结旧源及确定性慢IO观察，再成组实现与CPU合同；重点覆盖逐字节
头/体、临时响应、chunked尾部、慢错误、发送/握手共预算、超时清理、
DNS失败/到期、错误Key/CA/主机名、JS桥与配置传递、零重试/重定向。
保留已有parser/调用历史/工具批次合同，未变证据只按身份复用。

真实HTTP模拟与模型阶段全程同原gpu_exclusive锁。冻结上限：1模型、
6次生成尝试（计划Python和JS各两轮真实资料工具）、120控制、80个
无模型HTTP/TLS场景、360秒墙钟含45秒清理。只做本机短验；具体失败
保留并只重验受影响项，长验由用户负责，不自动扩大或反复寻找通过。

SDK接线核对本机固定版本client.ts的apiKey/fetch接口；官方资料确认
SDK默认有超时重试，因此此辅助类继续显式关闭重试：
[官方timeout说明](https://developers.openai.com/api/docs/guides/flex-processing#api-request-timeouts)。
本项目实际请求仅发本机模型，不连接OpenAI模型API。
