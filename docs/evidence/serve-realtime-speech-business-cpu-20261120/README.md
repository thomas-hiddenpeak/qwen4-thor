# Realtime语音业务驱动：CPU与实际程序构建

2026-11-20。实际候选cfae4d60零警告构建176.414秒；默认rev366保持。
新驱动复用原生TLS、固定OpenAI JS7.47.0/ws8.21.0和真实Chat/TTS/ASR。
三种音频格式、0.75语速、项目工具回答、播放截断/删除、取消后文字与
两客户RPM/实际TPM通过同一业务租约验证；不引入运行时外部服务。

11项Python反例与5项既有SDK事件检查、JS语法及20来源反例均通过。
汇总脚本误把Node默认Unicode报告当作TAP，原FAILED完整保留；逐份
检查全部实际零退出和原始成功计数后复用，不重跑。源码审查另确认
ASR会正常释放TTS权重，健康断言分别检查语音阶段ready、ASR后
metadata_ready，并新增1项含10个故障反例的CPU检查通过。共17项。
开发用G.711独立解码复用本机CPython3.12 audioop；其弃用提示保留，
不进入发行依赖，也未增加编译警告。

新增GPU为0。短验范围固定1模型、2WSS、9次response.create、2Chat、
3次不带原文/提示的ASR、1200秒，尚未运行。原质量失败与用户延期
听感保持；恢复、主轻资格、可回退交付和全部master继续。

[验收](acceptance.json)、[索引](index.json)、[原始记录](records.tar.gz)。
