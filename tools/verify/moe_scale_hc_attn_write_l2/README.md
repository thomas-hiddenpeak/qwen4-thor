# 四分支 layer2 注意力 HC 写回

prepare.py复核并复用封存通用FMA helper及原零警告日志，不新编译。
run.py在准备和执行时校验摘要。stdout留prepared/run.log；退出
终态后seal.py，封存stdout留prepared，不覆盖任何冻结证据。

各支自身layer2 attention out、layer1最终残差和layer2 attention
注入门值做CPU显式FMA/BF16写回。原分支须完整恢复实际fused
输出和MLP残差消费者；检查有限性、BF16舍入及输入变化支持集。
完整BF16、全部差异索引/处F32及token计数保留，预算2GB并保留
20GiB空闲。旧模型目录、运行时和reference不修改，无新HTTP。

终点在layer2 MLP HC读取前；仅layer0尺度干预，不新增本层修复。
没有最终任务正确性或性能结论。详见尺度传播报告。
