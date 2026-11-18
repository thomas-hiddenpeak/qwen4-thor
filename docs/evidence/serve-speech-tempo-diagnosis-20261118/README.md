# Speech高速内容诊断：4倍仍未解决

两级≤2倍处理的4项CPU合同通过（0.114秒），885来源/12负控
通过、零警告；原两条4倍实际内容仍失败（4.174秒）。
成熟SoundTouch 2.3.2的4倍同样丢词，将候选和参考再减速均有
重复或错词，未证明只是ASR限制，不能据此接受现有算法。

诊断原计划6例/2 ASR；首组三例实际均执行，但比较器遇到错误
语言提前退出。保留原失败，修复记录逻辑，将错语言/无EOS明确
记为失败，复用这三例，只执行剩余中文组。诊断累计仍为2 ASR；
补验加诊断共3 ASR、0 TTS/codec/主LLM，约12.73秒。
所有租约正常释放，默认rev306保持。不是性能或完整正确性验收。

SoundTouch是已有系统库的**测试对照**，不进入生产程序或发行依赖；
接口按[官方2.3.2头文件](https://codeberg.org/soundtouch/soundtouch/src/tag/2.3.2/source/SoundTouchDLL/SoundTouchDLL.h)
核对，原件在本地`tempo-diagnosis-source-01`。其[官方说明](https://www.surina.net/soundtouch/README.html)
描述WSOLA和窗口质量取舍，不能替代本次实际失败结论。
安装的FFmpeg未含atempo，本次未执行或部署FFmpeg。

下一步：4倍仍是待实现/验证项，需保留短时语音变化的算法与独立
内容证据；不把拒绝4倍或缩小兼容范围当解决。可独立继续编码、
服务生命周期和HTTP接入，全部Speech任务满足后才交付。
