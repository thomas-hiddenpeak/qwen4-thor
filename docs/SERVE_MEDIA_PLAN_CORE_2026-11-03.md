# SRV-02i第一批：共享媒体预检核心

2026-11-03事前冻结，可靠性开发。父提交129b5135；02h候选仍等待GPU，
不修改其冻结来源/工具/预算，也不将02h或本批CPU结果算作GPU交付。

本批实现可被processor与serve共同使用的纯host模块：有界PNG/JPEG
头部识别、源图/帧累计和resize几何、预计patch/merged tokens、视觉
workspace和attention shared memory检查。使用固定容量输出和span，
规划本身不做解码、设备调用或堆分配；后续集成必须在全部items预检
完成后才解码，解码器仍需独立分配配额和实际尺寸复核。

候选逻辑上限：8个items、每item32帧、总encoded16MiB、单源图边长
16384/像素16777216、累计强制RGB128MiB、处理后65536 patches、
vision workspace2GiB。这些为可显式传入的有界策略，0不表示无限；
仍需结合max_prefill、槽位max_len、已分词文本和device函数能力进一步
收紧。源像素上限复用原image processor配置；128MiB可容纳8张2K图或
32张1K帧的RGB。workspace按实际10H+I及32MiB GEMM scratch计算，
不改变resize来强行塞入限额。配置/发现接口接线时必须公开有效值。

各上限是逻辑工作量边界，不是总RAM峰值估计或物理预留；解码器内部
缓冲、原始/resize/float/BF16等并存仍单独记账。必须提供已知的实际
attention函数动态shared memory上限；未知不假定48KiB或Thor最大值。

保留正常输入现有C++ round/resize/补末帧规则；修复整数乘法溢出的
几何计算。不悄悄改成另一参考库的舍入规则，也不对过大输入自动裁切。
支持PNG IHDR和8-bit baseline/extended/progressive JPEG SOF元数据；
头部通过不保证图像流可完整解码。其它格式明确拒绝，完整媒体字段、
base64、role合同及C分配硬配额留给接线批次，不能宣称已被修复。

固定CPU验收：无驱动C++23零警告构建，正常PNG/JPEG/图像/视频几何、
奇数帧padding、独立上下文预算、末尾帧不一致、格式/截断/坏头、极端
整数、每个容量边界、未知设备上限，且持续禁止C++堆分配下运行planner。
同时禁止C malloc/calloc/realloc；ASan/UBSan开，固定80个命名合同案例，
另12张小图（4组尺寸，各PNG/baseline JPEG/progressive JPEG）分别走
image/2帧/5帧，与未改processor对照36个实际几何。输入最大321x272
范围，任一失败仅针对该缺陷处理；不测任意运动视频或新像素质量。
生成夹具与产物只进入.q4t-work。合成案例不做大分配。测试数量和全部
原始输出保留；具体失败只改/重验受影响项，不启GPU/HTTP/模型。

完成此核心后仍须接入现有processor/RunVisionPipeline/配置与错误参数，
冻结包含02h尚未执行场景的统一GPU/业务验收；不能凭独立planner通过
称服务已有限额。当前默认3fd31a21保持，长期工业化目标active。
