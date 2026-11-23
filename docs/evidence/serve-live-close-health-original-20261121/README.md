# Live第三轮原始结果：最终健康断言失败

原始驱动结果与租约命令结果仍为FAIL/COMMAND_FAILED。实际ASR、客户端
委派/真实项目资料读取与回填、语音rev426、session.closed/1000关闭以及
关闭后的普通Chat=42均通过。alpha实际准入3、结算1730 tokens，无额外
尾部ASR，预留归零。共1启动/4实际工作/64.040秒，62次NVML正常，
资源和租约清理完成，默认不变。

失败位置是最终健康检查错误地要求两种语音模型都常驻ready。源码
`ActivateAuxiliaryFamily(true)`在TTS前释放ASR权重，`AsrHealth::Released`
将它置为metadata_ready/device_bytes=0，保留有效元数据。本轮ASR/TTS的
reason均none，load/compute failures均0，cleanup pending为false。

这份原始结果不改标。修正检查器后，必须独立复核已保存的完整业务、
源码身份、真实资源结算与健康记录，并补反例；复核不需要另跑GPU，
也不把COMMAND_FAILED伪造成COMPLETED。总计划与Live后续任务继续。

原始来源/日志见[evidence.tar.gz](evidence.tar.gz)、[索引](index.json)及
[结果](result.json)。不包含私钥或客户/网关配置。
