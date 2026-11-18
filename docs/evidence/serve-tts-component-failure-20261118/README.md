# TTS计算首轮失败保留

2例各2帧，1GPU talker和1CPU参考加载，43.453秒；提示/talker
通过，预测器部分hidden/logits超过冻结误差合同，64 codes中4个
不同，整体FAIL。完整132操作数与两组codes判断见result.json；
不放宽门槛、不作为功能交付或声音质量验收。严格租约正常监控，
自身清理/释放成功，默认rev306保持。

新增绑定/缓冲2CPU与比较门3CPU已过，构建零警告，872来源/11
负控通过。下一步仅CPU用保存的native talker hidden重复预测器，
区分输入累计漂移与预测器本身偏差，随后修复具体问题；全部master
与PCM/接口/恢复交付继续。
