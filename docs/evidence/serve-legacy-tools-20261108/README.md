# Legacy functions/function_call交付原件

实现提交`30e1b62c`。31 C++、53 schema/native、12 Python（含3项
驱动）、6 JS通过。首轮JS夹具误取id而非call_id保留在cpu-01，
修正后只重验该项；桥接legacy计划一致性正反例另通过。四目标及
未改原生目标构建零警告，不跑完整性能/模型质量。

真实1启动/7Chat、61.614386秒驱动、62.658040秒严格GPU租约，
59次NVML全OK。实际MODEL.md读取、旧function结果回填/标题引用、
auto JSON、旧历史迁移现代工具、截断/取消不执行和同PID MTP继续
通过。模型退出0；GPU、18161—18165端口及锁释放。

`index.json`列124份原件及SHA256；586源码Git和快照核对。
默认33c52e4a/e33fa4f0，协议13f42691；服务13f077b6/4996f016。
旧945a05ea/6c167898和d2cc6b9b/dda2ef99回退保留。原生入口与
Guardian、44控制面运行文件相同；沿用已验CUDA探针；原ledger/
Key/配额保持STOPPED，安装只help/status，0模型/0Chat。

不包含ELF、模型与私有策略；接受范围是legacy函数接口，custom
及全部剩余总计划继续开放，短验不能代替长稳。
