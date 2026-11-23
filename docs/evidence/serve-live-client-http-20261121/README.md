# Live原生客户端资料查询真实业务验收

实际运行：候选引擎68bcc25e、原生TLS入口、官方OpenAI JS 7.47.0 LiveWS，
1次模型启动、1个连接、1ASR+2前台生成+1普通Chat，64.040秒。原RPM3
保持；第三轮alpha实际准入3、结算1730 tokens且预留归零。真实转写为
“请查询当前项目版本号。”，客户端读取真实STATUS并回填，原生语音
答复rev426。最终session.closed、1000关闭、普通Chat=42、资源/端口/
租约释放全部核实。62次NVML正常；默认rev426与客户预算未改。

最终旧检查器错误要求ASR和TTS同时常驻ready，故[原始运行](../serve-live-close-health-original-20261121/README.md)
仍保留FAIL和COMMAND_FAILED。实际资源管理在TTS前正常释放ASR权重，
ASR为metadata_ready/device_bytes=0、TTS为ready，二者reason=none、
load/compute failures=0且没有残留清理。独立离线复核使用原始字节、
完整未改的协议观察器和更正后的健康判定，通过5项健康反例及身份、
实际业务、结算、资源和租约检查；没有把原命令状态改成COMPLETED，
也没有追加GPU。验收状态为PASS_LIVE_CLIENT_BUSINESS_OFFLINE_REVIEW。

累计三轮3启动/10实际工作/255.282秒。[第一轮未委派](../serve-live-client-business-failed-20261121/README.md)
和[第二轮静音尾部重复ASR](../serve-live-close-tail-failed-20261121/README.md)
原记录不改写。关闭修复有[18项针对性CPU](../serve-live-close-tail-cpu-20261121/README.md)
及本次实际RPM/关闭证据，不能用提高配额绕过。听感未验。

这里只接受主WSS客户端委派资料闭环，不代表完整Live、主轻资格、
托管恢复或新默认已交付。继续音频fixture、Responses/Decisions、上下文
保留、录制/旁路/fork、媒体、组合发行和全部master任务。

[结果](result.json)、[复核原件](evidence.tar.gz)、[逐文件索引](index.json)。
原始GPU日志、事件和错误断言来源在上方原始运行归档，私钥和客户
策略不进入公共归档。
