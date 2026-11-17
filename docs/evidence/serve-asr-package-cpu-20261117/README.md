# ASR发行参数和接口声明的针对性CPU

2026-11-17，11个独立用例完成，含原失败12次/0.785391秒、0模型
动作/0GPU。原FAILED保留，仅补原失败的1项；其余通过证据复用。

固定模型profile/revision/权重SHA和显式ASR四项预算进入服务包
prepare/verify/command及CLI。可选资产缺失仍可验证包，不读取/下载
它们，不把音频失能转成核心Chat启动依赖；未声明配置、错误身份/
预算/路径/别名拒绝发布。原embedding包直接回归通过。

实际中文JSON、英文全部SSE事件、config/最终health用真实保留HTTP
响应验证。新增multipart/转录事件/输出/schema与OpenAPI路由，固定
SDK源摘要。原config校验失败暴露媒体声明遗漏video_detail和
video_high_min_frame_pixels，补齐当前完整分支并保留旧形状；旧失败
不改写，未重复GPU。共享内存null原已被支持。

C++实际能力声明绑定固定ASR身份，host/引擎构建零警告、默认五项
保持。实际程序与完整源码快照保留于工作目录；此处只接受CPU合同，
尚未接受ASR发行包或主轻/TLS/版本恢复组合。继续这些真实组合、
可回退交付和全部master，不关闭A10。
