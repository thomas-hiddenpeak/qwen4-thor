# 完整QSA主投影与变换边界

模板复制至prepared新目录、去掉.in并更新输出路径，不覆盖冻结
证据。本次qsa-full-main-20260924。observer.cpp以g++-14 C++23
-O2 -shared -fPIC -ffp-contract=off -Wall -Wextra、CUDA include和
-ldl构建observer.so；build.log须为空。首项测试运行run.py三侧
HTTP：关闭/开启/关闭观测，MTP关闭，120份元数据及原始请求/
全文/usage/stop/退出核对完成后才运行analyze.py。

覆盖12层全部4096位置的qg/k/v/out四组GEMM；记录实际BF16权重、
cuBLASLt算法，核对布局、FP32计算、alpha1/beta0、转置及32MiB
workspace。输入现场比对冻结HC mixed，输出投影输入比对冻结
attention输出。qg/k原始输出与q/k归一化输出完整保存；已有v、
最终out及RoPE后Q/K/gate完整比较冻结来源，不保存重复副本。

norm、RoPE、KV写入、attention及最终残差消费者均核对相关
指针/完整字节；HC混合输入来自跨运行冻结字节比较，不宣称
本观测重新追踪了HC混合生产者指针。保存norm权重/epsilon、
RoPE theta/位置/三行位置坐标及seqids。seqids为空时物化零数组，
本工具未另外保留null标记，不能区分空指针与显式全零数组。

analyze.py核对72组checkpoint权重、276份旧末行/参数、完整gate
拆分和RoPE不旋转后缀；投影/norm/RoPE算术在此阶段未重算。
原600440错答保持只证明观测不变性，不是质量通过。
采集预算3.4GB，后续参考预留4GB，另留20GiB。

run.py和analyze.py的stdout均放prepared。确认二者真实终态后
执行seal.py，stdout仍在prepared；复制已结束日志，再生成一次
终态artifact-binding并逐项复核，禁止封存后继续追加日志。
