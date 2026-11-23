# Live 真实查询通过，正常关闭多余 ASR 失败

第二轮使用实际原生 TTS 合成明确查询录音。ASR 未接收原文，正确识别“请查询当前项目版本号。”，前台实际产生客户端委派；客户端读取真实 docs/STATUS.md，回填对应委派，原生语音正确答复 `rev426`。这些局部结果保留。

正常关闭却把 VAD 后剩余静音作为显式 commit，再申请一次 ASR。客户 RPM 已按本轮 1 ASR + 2 前台调用设为 3，因此额外请求被拒绝，SDK 报 client_rpm_exhausted，缺少 session.closed；整轮仍为 **FAIL**。不会靠提高配额绕过，也不把有正确答案当正常关闭成功。

实际 1 模型启动，1 TTS夹具 + 1 ASR + 2 前台生成共 4 项工作；77.921 秒。ASR/项目回答质量正确，原生 TTS录音来自明确标注的合成夹具，不冒充真人录音或已试听。模型正常退出、进程/端口/预留清理，NVML全部正常，默认 rev426不变。

[evidence.tar.gz](evidence.tar.gz) 保留原始事件、生成录音、原失败源码、日志与清理；[result.json](result.json) 和 [index.json](index.json) 记录身份。下一步只修复正常关闭的已判静音尾部处理，保留真实未分析/有声尾部、样本时钟与已接受借用者，并验证显式Realtime commit原行为。默认和总目标未改变。
