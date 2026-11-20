# rev386原生Realtime双向音频可回退交付

2026-11-20。默认引擎ecd6cc23、协议f0c82ef9、v24服务包1fbaf0cb，
主轻资格3c1e823a，保留控制包c5ff88e6。19条原生CLI/349.269秒
全部成功，实际完成升级、两方向登记、STOPPED整根回退和再前进。
服务最终STOPPED，发行新增模型启动0；旧rev376程序、元数据与状态
快照保存在本地delivery-01/previous及previous-state。

同一conversation现在处理真实音频输入、ASR、手动commit、VAD自动
回答/打断、空闲、混合及临时音频；保留真实波形和历史，不能用客户端
transcript绕过识别。语音输出、函数工具回填、取消分类与准确结算
已组合验证。共用核心、原生入口与客户配额，不引入nginx或外部语音
服务。正常播放听感仍由用户另行安排。

[主轻资格](../serve-realtime-duplex-composed-profiles-20261120/README.md)
注明原主档身份通过五文件取消分类修复的来源复用；轻量取消为当前
引擎实际运行，不声称重跑完整矩阵。
[受影响恢复](../serve-realtime-duplex-managed-recovery-20261120/README.md)
含原失败累计6模型/21工作/7连接/1探测、353.339秒，332NVML正常。
入口重建保留健康模型；控制器恢复等满原配额窗口；整包回退保留
双向输入和profile历史，不重放中断工作。真实故障只注入候选到
保留包，反向验证登记与STOPPED回退，未额外注入运行故障。

恢复接受与模型质量分别记录：原无关拒答、TLS恢复后拒答和回退后
错答仍FAIL；原字段检查失败、缺失最终metrics、实际命令与计划
argv不同也保留。没有改变seed/temperature，不以成功采样覆盖失败。
本次不声称全模型质量、性能收益、长稳或完整OpenAI兼容通过。

alpha RPM30/TPM6000/output3457，beta RPM1/TPM6000/output256不变；
客户/用量账本、profile历史/冷却、版本周期及外层恢复预算均保持，
outer enabled且charged_total=0。回退包保留同一已验引擎和双向能力，
只切换已验旧控制器，不自动降回不支持音频输入的旧引擎。

发布前原生维护CLI可逆归档各4条退役发布意图、副本、版本登记，
124.128秒，0删除、0模型操作。原件与SHA/恢复计划
留在publication-maintenance-01；当前依赖、回退锚点和预算未变。
私有状态、密钥和程序副本未进入公共归档。

继续本阶段PR最终检查及main合入，然后MCP、追踪、Live与全部master。
[交付记录](acceptance.json)、[索引](index.json)、[原件](records.tar.gz)。
