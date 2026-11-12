# A06 Completions / Models交付证据

原始记录见[index.json](index.json)，文件字节与SHA256逐项核对。
1模型/7生成：原始Unicode echo与真实提示概率、批量6候选JSON/SSE、
best_of实际均值选优与全部24输出token计费、FIM `a + b`、取消后同PID
Chat/MTP；Models目录/详情/稳定时间/404/403通过。驱动51.954秒、
严格租约52.503秒，50次NVML全OK，进程退出0、端口/锁释放。

[原业务结果](business-01/result.json)保留FAILED：最后驱动误把取消计入
错误数；此前所有业务断言通过。实际7=6成功+1取消、0错误符合原合同。
[独立原始线包复核](business-review-01.json)重新核对实际响应、完整候选
概率、原commit运行时源码、指标和清理。仅修复驱动断言并加CPU反例，
ELF及运行时源码不变，无额外GPU运行。最初缺少--inherit-lease的0模型
拒绝、新增夹具编译失败和2项夹具预期失败均保留，不覆盖历史。

[交付决定](release-01/decision.json)、[源码/控制面核对](release-01/control-review.json)
和[默认安装](delivery-01/result.json)分别记录实际范围。50不同C++、
51输入/schema、6官方SDK、4实际原生/TLS、11驱动/托管/发行CPU检查通过。
新包随附两份补全schema；原生与可选nginx均有路由，别名配置进入托管
启动参数；既有恢复策略不变。安装0模型/0Chat，保留已验A05回退包，
原ledger/策略/Key/配额保持STOPPED。非性能任务，无全模型质量、性能
或长稳接受声明；A07及其余总目标继续，尚未合入main。
