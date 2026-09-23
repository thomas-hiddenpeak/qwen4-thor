# 完整indexer覆盖与旧压缩参考复用审计

纯冻结数据核对，不新增HTTP。复制至prepared新目录并改输入路径，
新建同名e2e目录；run stdout留prepared，终态后seal.py。
检查36组GEMM与576块汇总的完整笛卡尔覆盖、mask结果，分类
7139项评分FP64差异：实际可见3585、被mask3554、padding0。

84份压缩操作数逐字节接通旧完整1024组参考：raw历史、norm权重、
参数、压缩输出、三行RoPE坐标。旧sass-order-reference.json的
variant5计数已由原清单绑定；其完整输出当时不在该清单，故本次
仅声明当前输出字节等于旧已绑定expected摘要，不虚构历史绑定。

初版把旧清单误当成覆盖全部生成文件，KeyError退出1；原审计及
日志保留。v2显式记录historic_manifest_binding=false及当前验证
方式，未覆盖或修改任何旧证据。本次qsa-full-indexer-audit-v2-20260924。
这只是复用输入与现存输出的校验，不是重新运行完整压缩参考。
