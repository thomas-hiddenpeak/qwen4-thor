# 官方Python SDK与原生TLS音色闭环

固定官方openai 3.55.1及依赖，直接使用Voice/Consent/Speech/Chat方法，
SDK仅为测试依赖。原生入口验证证书、API Key及路径前缀；保留原始
multipart/JSON/SSE和SDK解码结果，禁止重试。10项不同CPU用例通过
（11次执行），覆盖真实SDK序列化、错误、流式字节、参数扩展和清理。

首轮1主模型/2入口、11次业务尝试、88.038秒：真实授权与Voice注册、
改名/分页、跨客户、否定授权/异声/配额、入口重启后状态、Speech
及无提示ASR通过。Chat客户端错误使用enable_thinking关键字，未发出
HTTP，原失败保留。修正为SDK extra_body，2项受影响CPU通过。

续验精确复制原注册状态，不重新登记或生成录音；1主模型/1入口、
6 POST、66.215秒。SDK流式Chat实际生成并逐事件核对原wire，独立
ASR完整识别“Please close the window.”，存储Chat音频ID一致。
撤销后Speech和Chat均404、Voice删除成功，核心前后均42。
复用首轮Speech独立ASR“The blue train arrives at noon.”和入口重启证据。
两轮合计17次尝试、16个实际业务POST；控制请求不含在该POST计数内。

各轮持续持有严格GPU租约；83/63次NVML均正常，末次无GPU进程，
模型和入口退出0、自有子树/端口/锁释放确认。轮询不排除小于采样
间隔的外界干扰。未宣称性能收益，默认rev316及714生产源码保持。

入口使用验收进程提供的生产AdmissionSocket协议；此证据覆盖原生
TLS及入口重启，不冒充真实Guardian恢复。继续主/轻profile、受监护
恢复、Base-only已有音色消费资格及保留状态回退。参考音频ICL、
提示词音色、Live及听感/真人身份质量仍需闭合，完整语音资格未宣称。
