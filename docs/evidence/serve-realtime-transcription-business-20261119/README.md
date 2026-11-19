# 真实Realtime手动转录短验（2026-11-19）

**PASS**：实际官方OpenAI JS7.47.0/WS8.21.0→原生TLS入口→原生ASR。
1模型/1入口启动，3WSS会话、5有效音频提交、1普通Chat，44.378秒；
44次NVML正常、唯一持续租约，进程/端口/锁清理、默认rev326保持。
严格guard COMPLETED与driver PASS分别保存，不由轮询推断无短暂干扰。

- 真人中/英录音按1、7、16381、16000字节交替块送入真实PCM队列；
  三份完整转录的增量拼接与最终文本完全一致。中文PCM与G.711均
  精确符合原文，英文WER=0.027778，低于原0.05门。
- 模型实际logprobs/音频与文本token数完整；alpha两轮81+259=340
  token真实结算。第三轮原RPM拒绝，仅failed，无伪造成功/usage。
- beta真实ASR首个增量后关闭：1请求aborted，无成功终态，队列借用、
  数据许可、token预留、会话与256MiB逻辑预留全部回收；新连接真实
  G.711转录成功。随后同模型PID普通Chat返回42，未自动重启或重放。
- 全组5个item身份、84个事件身份关联与唯一性核对；
  4请求成功（3ASR+1Chat）、1配额失败、1取消，原始观测全部保留。

此前普通Chat前置结果复用自business-03；该次误接明文的WSS失败与
business-02的公平队列启动拒绝保留，不追认通过。累计3次serve尝试
（其中1次加载前拒绝）、2次完整模型加载、2条Chat；真正ASR仅本阶段
执行。0重复已通过CPU，0性能矩阵，0全模型质量矩阵。

入口使用真实生产q4t-ingress/API Key/TLS和PID绑定Unix准入传输；
准入决定由私有固定后端控制夹具提供，**不等于Guardian恢复验收**。
未加入nginx、Python网络服务或SDK到原生核心运行依赖；JS/ws与
FFmpeg/CPython audioop仅用于开发验证。长稳/听感仍待用户。

当前候选SHA256 `de97cea01c22899aacebdc786ae8988b625ef71f58bb53cbb4c730ffaeaf1b53`；默认rev326不替换，ICL编码器
原数值待决及其他未验能力保持。继续自动VAD/降噪/延迟、对话/Live、
实际Guardian组合与可回退交付，不将本子任务当作Serve总计划完成。
