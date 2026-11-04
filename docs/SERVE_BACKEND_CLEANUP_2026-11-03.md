# SRV-02g：请求的MTP启动与默认程序内部清理

2026-11-03，可靠性修复，不是性能优化。父提交c7677c86，已接受默认
40cf6376/79d4a657保持，候选尚未验收。长期目标继续active。

## 确认缺陷与范围

- `--startup-plan off`时请求MTP加载失败被吞掉并退回plain decode。
  改为任何已尝试的MTP加载失败均拒绝启动；`--no-mtp`仍显式关闭。
- Model最终加载同步未检查，ModelOwner/MTP失败清理在drain失败后
  仍可能继续释放。改为不再继续析构未知生命周期的资源；MTP局部
  loader先于失败守卫声明，保证守卫先执行。逐tensor上传本来已有
  源所有权与同步，本批没有证明实际mmap提前释放故障。
- 默认q4t生产ELF直接链接的八类CUDA释放/完成API统一检查：Free、
  FreeAsync、FreeHost、EventDestroy、StreamDestroy、StreamSynchronize、
  DeviceSynchronize、EventSynchronize。失败固定位置诊断后`_Exit(1)`，
  不重试未知释放、不继续析构；活跃HTTP可中断，不能保证错误封套。
  SRV-02f已有明确责任方的serve cudaFree保留原始返回码，让其停止、
  drain并发错误协议。此例外不允许吞同步失败。

不额外增加GPU同步，不改模型算术、kernel分派、精度、模板或调度。
非默认research reservation profile、其他独立可执行文件、外部库内部
调用未由本链接边界覆盖；媒体全链分配处理、host OOM、物理RAM硬限额
和长稳仍待办。进程退出不证明驱动物理资源即时逐字节回收。

CUDA13.3定义中，stream destroy可在工作未结束时返回，event同步才
等待已记录工作；因此不能以成功destroy或async free当排空证明。
参考[NVIDIA stream API](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/group__CUDART__STREAM.html)
与[event API](https://docs.nvidia.com/cuda/archive/13.3.0/cuda-runtime-api/group__CUDART__EVENT.html)。

## 冻结验证

成组构建生产OFF和验证ON、host OFF/ON，零警告。CPU新增四项：嵌套/
线程隔离、原始release错误返回、失败不unwind/atexit、handled free不能
吞drain错误；运行完整host、required、backend、配置、inventory与驱动。

一把GPU租约覆盖原生HTTP25、真实MTP lifecycle（含微小缺权重/分配失败、
scratch/checkpoint边界以及真实MTP加载成功和两种分配失败），17个微小
CUDA探针与七次真实server启动。探针包含八API模拟错误、验证正常传递、
生产忽略故障环境、handled返回、MTP scratch/失败drain、预留未移交释放，
及三个正常组件路径。全量server不注入真实OOM或GPU损坏。

真实业务固定10次生成尝试、6个负场景、7启动、最多256控制请求；
整阶段1800秒，1500秒前进入安装，结束清理留90秒，不自动重试/扩样。
1生产实例跑普通JSON/SSE、strict JSON、OpenAI JS真实项目资料工具及
回答共5次；legacy MTP加载失败实例无ready；四实例各一次正常回答后
分别在MTP device free、model device/freeHost/eventDestroy退出1；
最后实例底层cudaFree错误必须保留完整SSE gpu_unhealthy/DONE后退出1。
微小探针17进程、native HTTP1进程、MTP lifecycle1进程另计，不伪装为
零模型加载；lifecycle中的实际MTP尝试与全量server启动分别记录。

失败保留现场；只有具体失败可定向修复复验。通过后保留当前40cf回退
pair，安装候选及sidecar，CLI/worker健康，HTTP复用同SHA候选；更新
证据、状态并阶段提交推送，不宣称性能等价或长期目标完成。
