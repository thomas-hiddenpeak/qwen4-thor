# 完整4K QSA选择/KV/attention边界

模板复制到prepared新目录并去掉.in，更新输出目录，不覆盖冻结
证据。本次qsa-full-selection-20260924。observer.cpp以g++-14
C++23 -O2 -shared -fPIC -ffp-contract=off -Wall -Wextra、CUDA include
和dl构建observer.so，build.log须为空。首项测试运行run.py的
三侧tools/evalscope原4K HTTP，MTP关闭，36份元数据通过后才
运行analyze.py。原600440错答保持不代表质量通过。

覆盖12层×4096 query的评分、选择、长度、Q/gate/attention输出，
完整写入K/V、位置、页表及cache前缀。选择与cache在attention
实际入口核对指针和全部字节，不保存重复副本。所有seq id核对
为0；实际指针为空时保存默认0数组并由元数据seq_ids_null标明。
页表必须映射至预算的0..4095物理槽，超出则该采集失败，不能
截断或扩大拷贝。观察器不改变生产kernel调用。

analyze.py核对完整cache映射与K/V写入、选择因果性/唯一性/
长度/尾部-1，逐份比对旧末query或完整小张量。尚未独立重算
排序、评分、投影、Q/gate来源、attention算术或out_proj消费者。
不能将这些边界检查视为完整QSA正确。采集3.2GB、参考1GB，
预留20GiB。后续数值参考复用冻结输入，保留全部差异。

首次analyze误按单层manifest读取旧原始文件，KeyError发生后保留
analyze-v1.py/log；实际旧manifest绑定capture-binding.json，后者
再绑定原始文件。修正后验证两层摘要链，不重跑HTTP或改变采集。
