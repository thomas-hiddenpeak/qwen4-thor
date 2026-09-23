# 完整indexer评分汇总参考

复用冻结完整点积和评分，无新HTTP/设备执行。复制至prepared新
目录并改路径，新建e2e目录及reference子目录。run.py stdout留
prepared，真实终态后seal.py；生成清单后不得追加归档日志。

12×4096×2048，逐四头ReLU和顺序FP32累加、缩放、因果mask。
三变体：旧设备rsqrtf(128)、CPU double sqrt倒数舍入FP32、
完整FP64求和与缩放。旧device-scale文件含两个float，scale.cu
明定0=rsqrtf、1=1/sqrtf；本参考明确选第0项，完整文件保存。
不将FP64评分矩阵传播至汇总，输入仍为实际BF16点积。

本次qsa-full-indexer-reduce-v2-20260924。初版误假设该文件只有
一个float，运行于输入检查即退出1，旧脚本/日志留prepared原目录，
没有算术结果；v2核对scale.cu摘要及两个值后使用明确索引。
576块形成36份完整F32参考，基底+delta无损恢复；所有差异未舍入
值与可见四头FP32和完整保留，匹配最终原值/不可见组和不保存。
预算1GB，另留20GiB。不把指定算术相同当成质量通过。
