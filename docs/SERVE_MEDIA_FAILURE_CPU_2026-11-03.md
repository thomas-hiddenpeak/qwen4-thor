# SRV-02h：CPU完成，GPU验收待交接

2026-11-03。实现快照`4b5bf5da`已推送工作分支，长期目标保持active。
这不是GPU验收或默认发布。范围见[冻结合同](SERVE_MEDIA_FAILURE_2026-11-03.md)。

## 当前产物与已验证范围

候选`20fadc22004f4e8692362c69a1a044eda58a64498ba95829974b69dd18c7da5b`，
validation `9b12d758e9c800f2888ddfb9f871972d6f162c837fbe06a7a8027d930dc4c2e3`，
protocol `44c4ea8bf93fd5368988973d783c934653e8e366bbc120b455ffed074d225269`，
207运行时来源，96审计锚点。两版最终构建零警告。

代码已改为：显式请求媒体时视觉加载错误拒绝启动；部分权重、RoPE、
workspace、请求pixels/output及解码源有所有者；workspace分配失败清空
scratch并保留权重。已知device分配OOM可返回503，其他device错误排空
停服，错误不能发布输出。std::bad_alloc用固定缓冲503或SSE error/DONE，
诊断构造前已有请求/错误计数保护。C解码分配失败待解码器自行清理后
才转bad_alloc；不从C分配回调直接抛出。后端区域host异常仍保守停服。

| 已执行检查 | 结果与范围 |
|---|---|
| host OFF/ON | 248/257，零失败/跳过 |
| 必需发布合同/grammar后端 | 214/9通过 |
| 驱动/配置 | 6与15+10通过；库存和生成合同检查通过 |
| 持续C++分配拒绝 | 22个HTTP/SSE状态和部分写失败组合，固定响应不分配、不重复终态 |
| processor所有权 | 41个C++、68个C malloc/realloc失败边界；8正常/恢复调用，跟踪资源归零 |
| 像素参考 | a/b图与各两帧共4组，逐字节一致；transformers5.16.1、Pillow12.3.0；两帧相同，不证明运动或torchvision等价 |
| 代码计算对照 | 视觉kernels/forward、resize算法及抽出的分配policy词法未变；不是整模型数值或性能结论 |

第一轮CPU因生成语义文档指纹未同步而整体失败，记录保留；补C解码
分配分类后最终身份重新构建/验证。更早格式整理前/解码修复前的构建
摘要也保留，其中历史runtime ELF不全保留、未复用为最终候选接受。

## GPU边界与恢复入口

16:50:59 CST只读NVML预检：vLLM PID2336565占93,864,288,256字节，
服务于16:05新启动。预检记ENV_ABORT，未开始GPU lease、原生HTTP、
tiny CUDA、真实视觉上传或模型业务；未停止/修改这项服务。
已询问该新服务是否保留，答复前按保留处理，不凭CPU成功推进发布。

阶段包`.q4t-work/serve-media-failure-20261103/package`已封存。确认GPU
空闲后，在来源/模型/SDK/默认pair校验仍通过时运行：

```bash
python3 -B tools/serve_media_failure/stage.py launch
python3 -B tools/serve_media_failure/stage.py finalize
```

两条命令有依赖：只有launch/guard成功才finalize。launch完整驱动持有
共享GPU锁，不拆段释放；17请求/8负场景/9启动及1800秒总上限，安装前
1500秒。发生失败保留现场并另审受影响项，不直接重复launch。后续来源
变化必须重新冻结适用候选，不能绕过指纹校验运行旧接受流程。

当前默认仍为`3fd31a21`/sidecar`3ba0b8d3`，未替换，也没有新的部署
健康或工具闭环证明。[SRV-02i审计清单](SERVE_MEDIA_RESOURCE_AUDIT_2026-11-03.md)
列出解压容量、几何及过晚准入问题，尚未实现；全机RAM保护、新ELF长稳、
实际访问/多人/通知与任务恢复等全局缺口仍存在。

## 证据

[CPU证据](evidence/serve-media-failure-cpu-20261103/README.md)封存362原件，
10份Git文件共2,095,617字节；13个本机ELF/源码bundle身份单列。
包括原失败、最终构建/测试、像素、候选/旧默认pair绑定及占用预检，
不宣称全部原始日志脱敏。暂存Git blob与原件逐字节核对结果见
[custody](evidence/serve-media-failure-cpu-custody-20261103.json)。
