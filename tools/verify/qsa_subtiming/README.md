# QSA计时记录合同（开发完成，尚未运行）

仅验证观察器的事件生命周期与缺失/错误拒绝；fake CUDA不接触设备，
不能证明GPU数值、真实计时准确性、归因或性能。此独立程序不链接引擎，
`fake_cuda/`不得加入任何引擎编译路径。

当前用户要求不追加测试，以下命令留待恢复测试后执行。从仓库根目录：

```bash
mkdir -p .q4t-work/qsa-subtiming-contract
g++-14 -std=c++23 -Wall -Wextra -Werror \
  -I tools/verify/qsa_subtiming/fake_cuda -I include \
  tools/verify/qsa_subtiming/contract_test.cpp \
  -o .q4t-work/qsa-subtiming-contract/contract_test
.q4t-work/qsa-subtiming-contract/contract_test
```

真实采集必须经GPU独占驱动，先进行正常HTTP对照。需要同时设置
`Q4T_LAYER_SUBTIMING=1 Q4T_QSA_SUBTIMING=1`，仅T>8且不保存checkpoint
的主模型forward启用额外事件。单独设置QSA开关、QSA值不等于1、
T<=8或checkpoint verify（含总T>8的多槽verify）均不增加事件集合。
旧LAYER开关仍按presence解释；只对新的QSA开关要求精确值1。
T不是纯prefill的证明，非checkpoint mixed batch也可能满足条件；
归因只取已知prefill调用，不能把不同forward角色合并报告。
本工具没有GPU执行入口，也不启动模型。

新增行`[qsa-subtiming]`的五项合计嵌套于`full_core`，score_select
包含位置元数据准备/可能的回读等待；不含QKV/out投影。单位毫秒，
按本forward所有QSA层求和。所有预期层五对事件必须完整且有效；
`valid=0`或缺行不能作耗时0或成功。开启开关后，新增事件及宿主开销
会产生观察扰动；与既有细计时日志分别标识，不拼接成同身份样本。

真实GPU验收还需检查输出/缓存一致、expected层数、五段与full_core的
关系及关闭/开启墙钟差异。七项CPU用例和本机编译都不能替代这些检查。
