# 四分支 layer1 新路由分组与输入量化

以各支新IDs按专家/flat slot稳定排序分组，每支40960行。按
(token,expert)与冻结实际gather对齐，旧行号-1表示新增选择；新增行
和删除边独立保存，不伪造旧值参与差异计数。原支重新排序后SF与
packed全部恢复，三个修正支分别新增147/212/238个专家行。

device重算比例和接受版legacy E4M3，并给出native诊断；CPU独立
重算比例、枚举RNE E4M3及E2M1 payload。CPU/device比例逐位相同，
native与独立RNE相同。生产编码缺陷在此有意保留：仍只传播layer0
干预，没有新增layer1修正。四支各有2950组高值编码错误，不隐去。
1024个checkpoint input_scale逐个重读绑定。

复制模板去.in到 `.q4t-work/prepared/moe-scale-input-quant-l1-20260924/`，
新建对应e2e/reference。cpu用g++-14 C++20 -O3 -fopenmp -Wall
-Wextra -ffp-contract=off；device用nvcc C++20 -O2 -arch=sm_110a
-ccbin=g++-14 -Iinclude，host -Wall,-Wextra,-ffp-contract=off。
空build.log后run.py，stdout留prepared/run.log；真实终态后seal.py，
stdout也留prepared。不覆盖冻结/共享文件，不修改生产代码。

预算2GB+20GiB余量，封存100项614486851逻辑字节。完整映射、
比例/尺度/packed、全部共有边差异、新旧边标识均保留。终点输入
FP4操作数，不构成GU、最终答案或新HTTP正确性证明。
