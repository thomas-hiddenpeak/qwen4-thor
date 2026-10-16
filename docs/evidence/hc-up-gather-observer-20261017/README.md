# Gather边界观测：CPU夹具阶段

原二进制动态链接cudaEventSynchronize、cudaMemcpyAsync和
io_uring_submit_and_wait，工具仅转发原参数、返回值与errno，不链接CUDA
实现或改变模型。用每线程同步成功返回作为起点，在T×2560字节PLE H2D
入口取终点，汇总该段内uring提交等待的墙钟和线程CPU；首次没有PLE
同步的样本不能使用，任何同步/I/O错误明确保留。真实映射仍须检查。

本机原B trace的目标H2D恰为24×20971520+1×20943360字节，即每chunk
一次；不可未经验证假定每层一次。模型加载阶段同大小拷贝须排除。
PageReader以O_RDONLY打开，未用O_DIRECT；uring返回等待不等于纯SSD
服务时延，读取字节不等于实际物理磁盘字节。区间外余量仍含CPU排序/
整理、调度和观测成本，不可直接称scatter。先前同步等待和ComputeRowIds
发生在所测边界之前，不能把这里的区间称为整个PleEmbedding::Gather。

CPU夹具使用libfake，实现20ms同步、20msCPU工作、30ms假uring等待；
不调用真实CUDA/io_uring。两线程、无同步、同步失败、I/O错误、普通形状
过滤、参数/errno/返回值均有断言；check_fixture.py验证记录及时间分解。
零警告编译（-std=c++23 -O2 -Wall -Wextra -Werror）。格式化后重编译的
probe SHA完全相同，复用原CPU证据，无重复运行。暂未启动新模型对照。

构建产物仅在.q4t-work/hc-up-gather-observer-20261017，编译命令：

```sh
g++-14 -std=c++23 -O2 -Wall -Wextra -Werror -fPIC -shared -I/usr/local/cuda-13.3/include tools/evalscope/gather_interval_probe.cpp -ldl -o .q4t-work/hc-up-gather-observer-20261017/gather_interval_probe.so
```

fixture与fake源和原始输出一并保留。实际模型观测必须另行冻结协议、
检查25条映射/请求合同与全阶段独占，不将此工具通过当作TTFT根因结论。


## 真实观测执行前冻结

protocol.json固定A B B A四条原200K off，每条新鲜服务、同输入输出合同，
全阶段单一GPU lease。仅启用layer timing和Gather probe，无PMU/Nsight。
用客户端事件的monotonic窗排除加载，必须准确对应25次目标上传与25个
chunk；第一chunk无条件排除。后24个区间均要求有效同步、成功I/O、
跨身份相同提交等待次数及请求完成数，避免比较不同读取工作量。
每指标均计算两组B−A与同身份R；稳定正增加仍只是观测，不自动归因。
不把全部Gather墙钟认作设备关键路径。所有原始结果保留，不扩样。


## 结果：Gather不能解释本轮层事件差

四条HTTP合同通过，guard正常COMPLETED，804次运行轮询未检测竞争。
25上传/报告一一匹配，后24条同步/I/O/拷贝成功、工作量逐chunk相同，
冻结有效性全过。各指标与全部区间见gather-review，原始证据哈希见manifest。
两组B−A层事件差+122.850/+100.690ms，超过同身份R=41.990ms；
Gather wall差−6.271/+2.059ms，R=6.546ms，方向和量级均不足以解释。
Gather总wall574.193–580.739ms，CPU572.888–579.377ms；uring API
wall308.483–316.442ms，CPU307.179–315.021ms，不能归作主要阻塞SSD。
此为本轮观测，不能追溯证明历史原因或把所有CPU余量称scatter。

离线层分组显示full组差+108.440/+124.210ms，lin组+15.060/−23.780ms；
full组仍含MoE/HC等，不是attention kernel净时间。停止Gather采样，
下一步复用已有Q4T_LAYER_SUBTIMING的full_qkv/full_core/full_oproj及
MoE/HC分项（先冻结协议），不新增无关优化，不用插桩TTFT接受HC。
