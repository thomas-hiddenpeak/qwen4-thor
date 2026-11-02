# SRV-07预检与配置原始证据

直接复制1088份记录；另有本说明、本地大产物身份、来源映射与根seal。
正式结果见[交付报告](../../SERVE_PREFLIGHT_RESULT_2026-11-03.md)。
默认5642eb68/6aca6a3e，运行时b3ae710e，冻结驱动24310aba；旧
SRV-06 a3fd64a3/49c7c631及完整回退链保留。

execution保留首轮8生成/23预检/15拒绝：7业务通过，长工具历史auto返回
合法工具调用而非最终答案，质量失败；execution-corrected保留第二次
双槽MTP启动后的驱动单槽假设失败，0生成/1预检。两次均正常退出。
execution-final按原字节/SHA复用首轮7通过项，新增plain双槽4生成/
7预检/1拒绝，包含显式none长历史及实际短/长槽分配。精确2049边界
只做预检。累计12生成/31预检/16拒绝/3模型启动，1054.62秒finalize，
原预算未扩大。双槽MTP生成未验；不能说首轮全部通过。

安装实际CLI和grammar worker通过；HTTP复用相同ELF的候选证据，
安装后没有另起模型。最终guard COMPLETED，ENV_ABORT0；全部自有
模型进程退出。CPU保留首轮整数夹具/测试链接错误及受影响项修正，
运行时SHA不变；新模板/Tokenizer差分125、客户端31、最终驱动13。

所有wire、SDK、日志、metrics、配置、schema、guard和安装记录按原
字节保存。23个ELF/探针/source.bundle只在local-identities.json
登记本地原路径、大小与SHA。内部包seal包含本地大文件，完整复核
需要保留原绝对路径；不要删除本地失败、证据或回退目录。
