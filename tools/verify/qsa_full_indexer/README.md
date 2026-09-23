# 完整4K indexer边界采集

复制模板至prepared新目录，去掉.in并更新输出路径；不覆盖冻结证据。
observer.cpp用g++-14 C++23 -O2 -shared -fPIC -ffp-contract=off
-Wall -Wextra、CUDA include和-ldl构建observer.so，build.log须为空。
首项run.py运行tools/evalscope关闭/开启/关闭三侧原4K HTTP，保持
原始请求、全文、usage、stop、服务退出；质量驱动1仍为原错题失败。
通过观测不变性才运行analyze.py。两控制器stdout留prepared，确认
真实终态再seal.py封存，seal stdout亦留prepared。

12层×4096，108份元数据；IQ/IK投影、查询变换、raw写入、压缩、
评分矩阵、汇总和选择的实际指针/完整字节交接。96份旧冻结输入
绑定，36组checkpoint权重39327744字节、216份旧张量/末行绑定。
HC mixed仍是跨运行全字节比较，不宣称重观测HC生产者指针。
seqids默认零与显式数组形式用null标记区分。评分保存整个
[16384,2048]矩阵，1024有效压缩key及其余padding一并留存；
因果可见列、未来组和padding不能混为实际评分读取。

本次qsa-full-indexer-20260924。捕获预算1.1GB、参考预留4GB、
另留20GiB。此工具只核对边界，不核对算术，不证明整模型正确。
