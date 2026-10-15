# 最终候选旧证据复用独立审查 02

2026-10-09。结论：允许按下述依赖范围复用旧证据，新增共享位置复制与
T4边界由本轮定向证据承担；没有发现需要重跑全部115请求的依据。
本审查只读取源码、冻结清单、既有构建/结果和摘要，不执行构建、测试、
HTTP或模型。审查者参与了terminal实现；这里是身份/依赖/证据复用审查，
不替代另一审查者对该实现的独立代码审查。

## 身份与变化范围

| 比较 | 运行清单数量 | 相同来源 | 修改 | 新增 |
|---|---|---|---|---|
| 原strict → M1 | 130 → 131 | 124 | 6 | 1 |
| M1 → 最终 | 131 → 132 | 124 | 7 | 1 |
| 原strict → 最终 | 130 → 132 | 118 | 12 | 2 |

计数是src/include清单项，含占位文件，不是编译TU数量。130旧快照、
131项M1快照和132项当前运行来源均与冻结SHA匹配；旧/M1/最终的
7/11/8项binary/cache身份也逐项匹配。两个阶段各18份运行目标
flags/link文件仅替换对应build目录后完全相同；CXX/CUDA/System
三份编译器元数据三版逐字一致，未删除其他参数进行归一化。

原strict、M1、最终q4t分别为 `a566faf5`、`a2ba1bd2`、`eae6949b`。
IO/runtime/text/trace库和sequential控制binary三版逐字一致；CUDA
model/PLE/quant archive及server库不同，不声称机器码或可执行文件等价。
完整SHA、逐文件差异和校验来源见同名JSON。

M1→最终7个修改来源为model.h/mtp.h/mtp_policy.h/model.cu/mtp.cu/
chat_generation.cpp/chat_scheduler.cpp，新增position_copy.h。model.h
是完成合同说明；model.cu运行改动仅ModelVerifyMulti上传源生命周期、
RoPE源持有、checked completion与checkpoint发布；strict使用的
ModelDecodeBatchMulti及reset/complete/end/restore函数未变。
mtp.cu除T4 Multi边界外，共享MtpForward由固定H2D改为UVA方向推断。
这一共享依赖确实变化，不能称整个strict路径字节未变。

## 115条历史请求为什么保留、怎样复用

- 旧115请求仍归原候选105条与独立变体10条，48对全文/计数/finish
  配对也保持旧身份，绝不标成最终binary重新通过115条。
- strict生产控制TU、其B1 target/argmax及模型算术kernel未变。
  接受/拒绝、stop与pending correction合同保留；新generation结果
  校验检查旧strict本就保证的计数、非stop前缀、词表和seed元数据。
  strict两处step-fit条件仅改共用名字，真值不变。
- 已验证有效KV/indexer/recurrent/PLE/RoPE/page-map及future不可见性
  的reader/writer与sequence生命周期未变。当前state测试文件逐字匹配
  旧long补件的冻结源码；与最初short诊断相比只含旧阶段已增加的
  long-only入口和可配置调用上限，不能混用两个历史测试身份。旧短局部
  通过与独立长补件保留为原受测实现的有界证据；没有对最终binary
  重新读取这些全部状态，也不宣称任何输入的形式化证明。
- 取消、RST/deadline、RequestControl、strict等待排空和请求释放机制
  未改；新前缀guard不改变合法strict返回的交付分支。T4专属seq.Fail
  不进入strict路径。旧同模式取消/恢复证据可据未变机制复用，不能
  扩大为任意错误、FIN或致命CUDA恢复。
- target前reserve/fallback顺序、普通scheduler B1回退、strict
  FinishSequential及错误结果清空未改。旧资源/中途故障证据仍支持
  这些不变合同；位置复制变化由本轮共享依赖补验承担。旧逻辑注入
  不是物理OOM/硬件故障。

最重要的例外是共享MtpForward：旧init含device来源与固定H2D的
方向不匹配，不能只凭旧HTTP通过声称新复制正确。新真实CUDA18例
直接证明本机三类来源、两种stream与1/4/8192行的值/guard/顺序合同，
本轮生产sequential补件和最终五档再覆盖实际初始化/草稿/extend接线。
这与复用未改的target/state/cancel机制共同构成证据，不是掩盖改动。

## 本轮补充范围与当前状态

| 新证据 | 实际补充 | 本次审阅状态 |
|---|---|---|
| 18例真实CUDA | 生产位置复制helper来源/形状/顺序合同 | 既有日志18/18通过 |
| 10 HTTP | T4变体fresh+四故障/恢复9条；最终生产strict 1K/256一条 | 既有summary通过，无强杀/残留 |
| 9 HTTP | 实际T4后的受控stop0..3、输出cap1..5、restore/skip-extend及prompt+提交前缀 | 既有summary通过，无强杀/残留 |
| 30 HTTP | 最终生产plain/strict各五档三次、配对及当前成本 | 审阅时尚无总summary，不能预先写通过 |

10与9中的T4请求均属独立链接变体，只有10组中的1条是最终生产
binary。30全部属于最终生产候选，执行完成后另据原DB/日志审计；
本报告不代替其结果。M1的8条入口/prefill补验仍单列M1身份。

原长直接组EOS失败、两模式末项coverage失败、定向补件、四项持续
任务语义不足、自然完整GPU extend只见1/4而2/3为有界控制等限制
全部保留。旧五档成本仍归旧binary；新30不宣称相对旧main无回退或
提速。现有证据不批准完整快速T4数值/状态资格，也不自动关闭Goal。
