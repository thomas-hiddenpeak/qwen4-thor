# HC 模块注册的受控诊断（执行前冻结）

A 为原 main，B 为原 HC，C 仅将 HC 模块构造函数入口替换为 RET。
prepare.py 检查原 SHA、init-array 重定位、注册字符串和 host stub 指令，
确认改动仅四字节，非 .text 段、文件长度与相对地址全部不变。
原文件不修改；C 不能部署或执行 MTP，因为 HC 内核故意未注册。

固定 A B C / C B A，各一条原 200K off 请求，完整阶段持同一 GPU lease。
冻结判据见 protocol.json；不追加样本，不以局部诊断替代性能验收。
prepare.py 依赖相邻 hc-up-thunk-cause-20261016/prepare.py 的只读 ELF 解析。
原始运行文件保留在 .q4t-work/hc-up-registration-cause-20261016/。


## 结果与监控竞态

六条HTTP均完成、输出合同通过、服务退出0。guard最终为ENV_ABORT，
原记录保留：在最后服务退出时，将自身PID 1149505误报为竞争进程。
NVIDIA查询和/proc读取跨越进程退出，最后client_after早于报告，
server_after_shutdown紧随其后。并非已证明出现外部竞争。

父runner在收尾被中断，因此不覆盖其仅五条observations，也不补造正常
COMPLETED记录。recover_completed.py只读六个已完成请求，另存
recovered-observations/review及completion-audit。冻结数值判据同样不满足：
B−C为−78.869/+52.049ms，R=204.820ms，不支持注册状态根因。
不追加HTTP、不重跑、不宣称完整阶段正常完成或性能验收通过。
raw-manifest记录原始日志、环境和退出证据哈希；off-shape-early-return
证明本档形状在新增HC分派判断之前返回（不排除函数布局/前后序变化）。

监控修复使用已观察的自有进程身份，加一次NVIDIA与/proc复核确认退出；
当前仍可见、未知、不可读或复用PID不能按历史记录豁免，复查错误仍中止。
12项CPU合同与专用host CTest通过；无新的GPU请求。
