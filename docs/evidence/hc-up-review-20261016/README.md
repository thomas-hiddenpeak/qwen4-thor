# HC up固定交错复审证据（2026-10-16）

结论：NO_GO保留，三对B-A均为正；并未定位延迟差异的根因。
协议与解释见[复审报告](../../HC_UP_REVIEW_2026-10-16.md)。
A为main be74946d…，B为原HC up候选f01a0ee6…，无新实现。
六条为selected-tier诊断，不是完整五档验收；原五档证据复用。
各次exit均server=0、HTTP输出检查通过，prompt/output hash全部一致。

run_control.py为实际执行驱动；输出目录固定，不能覆盖重跑。
control-results.json保留每次度量；control-review.json保留冻结规则结果。
elf-identity.json保留全部CUDA ELF hash，added-kernel-symbols.txt标识新内核。
启动准备失败发生在HTTP前：保存binary旁缺少CMakeCache，补回各自原始
归档后执行固定六条，额外HTTP为零；失败日志与说明保留。

完整服务日志、请求响应、原始数据库、构建配置及ELF留在本机
.q4t-work/hc-up-review-20261016/，模型和reference未改。
