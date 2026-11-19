# 原生参考ICL与Realtime可回退交付 rev336

默认引擎 `6c6857c859716efba3039784bf3964f39ec759ba956ca497799b9da4ccdc653d`，服务包
`37d329881968020f00bfedbbe48c0e856c5a288f767b84a2fbe1d16e21a7bd32`。19项STOPPED发布、实际整根回退与再次
前进通过，320.450秒，0新增GPU；服务保持STOPPED，outer启用
且累计启动0。前驱rev326及恢复入口完整保留。

本次交付参考音频ICL及Realtime转录：真实参考条件进入Speech/Chat，
实时转录支持持续输入、VAD/降噪、历史/删除与处理后波形取回。
[主轻实际资格](../serve-icl-realtime-primary-light-20261119/README.md)和
[Guardian恢复三组](../serve-icl-realtime-managed-recovery-20261119/README.md)
验证同一持久参考跨网关/整包恢复保持，未完成任务中断结算，显式新
会话恢复业务，不自动重放输入。修复后4模型、63HTTP+5Realtime、
595.196秒、554次正常NVML；原ops失败保留，累计5模型/65HTTP+5Realtime。

RPM30、TPM6000、alpha输出3457与已用额度保持；私有验收包中的
TPM60000没有覆盖持久策略。profile历史/冷却、版本周期和outer预算
保持。5旧intent、2旧generation、2旧registration通过维护API可恢复
归档，0删除。回退目标 `c995e9ca583d0401bee4af7445dceadbbfde8bfef86f82136f7ac285e208dc5e` 使用同一已验
引擎和不同保留控制器；实际验证候选→保留自动恢复和STOPPED根
回退，反向完成资格登记，未另注入反向自动故障，也不宣称旧引擎回退。

62项ICL/Realtime恢复合同接入CI，本机只核对名称加载，复用已过CPU
证据；CI结果和主线合入另记。原exact-code失败、声纹一致性失败及
用户延期听感保持，严格浮点边界接受不能替代音色质量。语义VAD、
Realtime对话/Live、提示词音色及其他master缺口继续实现，不把本阶段
交付称为完整OpenAI兼容、全模型质量、性能或长稳通过。
