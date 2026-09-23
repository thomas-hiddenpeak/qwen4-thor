# 四分支 layer1 路由专家合并

各支新down按自身flat slot构造逆映射，验证40960分配完整且
无重复、每slot专家与自身ID一致。使用自身router权重，CPU
顺序10次显式FMA；原累加起点经核实全正零。原支完整10485760
个F32逐位恢复，修正支使用新槽位与新权重，无旧路由替代。

模板去.in复制到 `.q4t-work/prepared/moe-scale-routed-combine-l1-20260924/`，
新建对应e2e/reference。combine用g++-14 C++20 -O3 -fopenmp
-Wall -Wextra -ffp-contract=off。空build.log后run.py，stdout留
prepared/run.log；真实终态后seal.py，stdout也留prepared。
新实验新目录，共享输入不写入。预算1GB+20GiB余量。

保存完整逆映射/路由F32输出、全部差异与token分布，来源down、
IDs和权重逐项绑定。终点仅routed F32；共享分支尚未重算，不能
声称完整MoE输出或最终答案已确认。没有生产/观测修改、新HTTP
或性能结论。
