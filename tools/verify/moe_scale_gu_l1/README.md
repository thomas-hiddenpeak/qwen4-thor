# 四分支 layer1 新行数/新输入 GU 传播

使用各支新专家行数及按flat slot排序的FP4输入，checkpoint gate/up
参数重读绑定；记录的每专家cuBLASLt算法须对新shape通过AlgoCheck
及32MiB workspace检查，失败不换算法绕过。输入已按新序排列，
不再套用旧gather重排。原分支361专家，三个修正分支各360专家；
没有新激活的专家。输出按(token,expert)与实际GU对应，新增行
独立保存，不填零冒充原输出。

复制模板去.in到 `.q4t-work/prepared/moe-scale-gu-l1-20260924/`，
新建对应e2e/reference。gu用g++-14 C++20 -O2 -Wall -Wextra，
链接cublasLt/cudart。空build.log后run.py，stdout留prepared/run.log。
run.py真实终态后执行support_audit.py：确认共有输入未改变的行，
输出也未改变；其stdout留prepared/support-audit.log。通过后执行
seal.py，stdout留prepared/seal.log。新实验新目录，不能覆盖。

全部新shape兼容，原支52428800个BF16按映射全部恢复。逐项支持
审计显示没有不变共有输入对应变化输出。完整各专家GU、全部共有
差异/行分布、旧GU行映射、新flat slots和权重计划封存；新增行
输出完整保留。预算2GB+20GiB余量，封存10482项1246430733逻辑
字节。未保存FP32累加器；这是记录算法传播，不是独立GEMM参考。
仍仅layer0尺度干预，终点layer1 GU，尚未中间量化/down/共享分支。
生产未改，无新HTTP或最终答案正确性结论。
