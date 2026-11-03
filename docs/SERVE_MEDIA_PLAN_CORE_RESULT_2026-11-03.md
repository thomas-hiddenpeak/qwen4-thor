# SRV-02i：共享预检核心CPU通过，服务接入待办

2026-11-03，可靠性开发，父提交129b5135。
[事前合同](SERVE_MEDIA_PLAN_CORE_2026-11-03.md)中的独立核心已完成。
没有替换默认程序，没有运行模型、HTTP或GPU测试；工业化目标保持active。

## 实现与边界

[media_plan.h](../include/q4t/vision/media_plan.h)与
[media_plan.cpp](../src/vision/media_plan.cpp)提供纯host、固定容量计划：

- 验证PNG签名/IHDR字段/CRC及8-bit JPEG C0/C1/C2元数据，拒绝其它格式；
  头部有效不代表压缩流能成功解码。
- 检查全部item/frame，累计encoded和强制RGB源数据量；末帧尺寸不同也
  在调用方开始解码前拒绝。返回固定错误码和item/frame位置，失败清空
  整份计划，不把先前成功item当作可执行的部分结果。
- 保持现有C++ round、resize和视频补末帧规则，以宽整数检查乘法及
  resampler/CUDA使用的有符号int索引、ceil-div余量，再窄化。
- 统一预计patch、合并后token、替换placeholder后的prompt长度，要求
  留至少一个输出槽位并满足max_prefill；单时间组attention共享内存
  必须满足调用方提供的实际函数上限，未知时拒绝。
- workspace按现有代码`L*(10*H+I)*2+32MiB`计量，并分别报告pixel BF16
  和输出BF16；不把它们冒充同时峰值。策略值为有界参数，0不是无限。

当前生产CMake、processor、serve以及02h冻结工具均未改写；只有独立
host入口编译新模块。这些检查**尚未在实际服务生效**。解码器内部分配
配额、解码后尺寸复核、HTTP字段/base64合同、取消检查和配置公开需接入。
源RGB预算不覆盖stb内部临时缓冲、resize/float副本或统一物理RAM；不是
内存预留。模块只支持当前已实现的temporal/spatial merge=2合同。

## 本机验证

原件目录`.q4t-work/serve-media-plan-core-20261103/run-01`，首轮通过，
没有失败重试或新推理。C++23/g++-14，`-Wall -Wextra -Werror`，独立
ASan/UBSan构建。动态依赖没有CUDA；编译后复核默认pair和02h封存通过。

| 检查 | 结果 |
|---|---|
| 构建 | 两个纯host executable零警告 |
| 合同 | 80命名案例全部通过，含PNG/JPEG全部前缀截断、畸形头、格式、极值、帧尾不一致、每类容量与精确边界、独立上下文、设备未知和失败事务性 |
| 禁止分配 | planner调用区持续禁止普通C++ new/new[]和直接C malloc/calloc/realloc；观察到0次分配尝试，结合实现无容器分配/设备调用 |
| 实际processor对照 | 12张小图×image/2帧/5帧，共36例；PNG、baseline及progressive JPEG；网格/元素数均相同 |
| 几何语义 | 140×100参考网格、272舍入半点、5帧补末帧、32帧宽整数乘法等固定预期通过；未改变现有processor像素计算 |
| Sanitizer | 实际上述调用无ASan/UBSan报告；不外推未执行输入或完整模型质量 |

probe SHA：`4d36c6f7e436a7ab0d99657f28a0324d33f6116557d5b5de2b427360ee5d12ee`；
实际processor对照SHA：
`36426e1b19aa6a881bf46dd3ed8f15468f3017dec536e796435b91029530ad96`。
真实解码器保持02h来源，不重新宣称整套像素/运动视频/torchvision等价。

17:28:09 CST只读NVML仍见vLLM PID2336565，占93,864,288,256字节，
没有停止或修改服务。CPU阶段不需要GPU lease，也没有取得GPU lease。
默认继续`3fd31a21`/sidecar`3ba0b8d3`；02h候选20fadc22仍等待原验收，
本阶段不能替代其GPU错误注入、视觉上传或真实工具调用闭环。

## 证据与下一步

[证据目录](evidence/serve-media-plan-core-20261103/README.md)保留42原件，
包括命令、完整stdout/stderr、构建参数、源SHA、12生成图片及环境观察；
4份Git文件共24,348字节，两个本机ELF按完整SHA登记。
[原始结果](evidence/serve-media-plan-core-20261103/result.json)及
[manifest](evidence/serve-media-plan-core-20261103/manifest.json)可逐项核对。
封存工具在测试后增加，不在被测试核心来源中；没有因此重跑已通过项。

下一批先接入全部媒体的前置准入与共享resize计划，再补解码器配额、
实际尺寸复核、字段/base64合同及错误映射、取消/截止边界与有效配置。
改动冻结运行时来源时必须形成新复合候选及统一验收，覆盖02h尚未执行
场景和02i过限拒绝/同PID恢复；不能绕开旧包指纹或将独立CPU结果当发布。
