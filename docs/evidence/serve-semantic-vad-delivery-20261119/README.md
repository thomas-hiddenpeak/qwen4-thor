# 原生语义 VAD 可回退交付 rev346

默认引擎 `945775bbbdeadf6d6f008409f7b3daf5d5b3db628481a23f6a53b51622602dd2`，服务包
`38a8fcf6f3b1bebb05a96b25b0c072871d64b5e033f70e0a26f6dc32725c30eb`。19 项 STOPPED 发布、实际整根回退与再次
前进通过，364.243 秒，0 新增 GPU；服务保持 STOPPED，outer 启用
且累计启动0。首次发布驱动在60秒超时后保留FAIL与prepared双日志；
以原请求ID调用publish-resume完成，未重复迁移/重置预算。19项成功检查
共20次命令尝试，原超时和续办原件均保留。前驱rev336及恢复入口完整保留。

本次交付真实语义断句：ASR 完整前缀交给本机主模型评分，使用音频时钟
控制最长静默等待；续说取消并隔离旧评分，分类用量计入所属客户端。
[主轻实际资格](../serve-semantic-vad-primary-light-20261119/README.md)和
[Guardian 三组](../serve-semantic-vad-managed-recovery-20261119/README.md)
验证精确转录、私有评分结算、后端健康、入口和整包恢复后的能力保持。
恢复三组 4 模型、63 HTTP+10 Realtime、628.911 秒，
586 次正常 NVML；原导入失败0模型/0业务保持；发布迁移修复后只补候选版本组，
累计6模型/87HTTP+14Realtime，保留/TLS与主轻未重跑。

alpha RPM30/beta RPM1、两端 TPM6000、alpha 输出3457及已用额度保持；验收夹具 TPM60000
未覆盖持久策略。profile 历史/冷却、版本周期与 outer 预算保持。
5 旧 intent、2 旧 generation、2 旧 registration 经维护 API 可恢复归档，
0 删除。回退使用同一已验语义引擎与保留控制器；候选→保留实际自动
恢复通过，反向完成登记及 STOPPED 根回退，未额外注入反向自动故障。

PR #51 的最终提交 CI 与主线合入另记。原 exact-code、声纹一致性失败及
用户延期听感保持，浮点边界接受不能替代音色质量。Realtime 对话/Live、
提示词音色以及其余 master 缺口继续实施；本阶段不表示完整 OpenAI
兼容、全模型质量、性能或长稳通过。
