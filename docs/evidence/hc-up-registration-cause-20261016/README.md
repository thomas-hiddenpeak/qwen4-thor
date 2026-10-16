# HC 模块注册的受控诊断（执行前冻结）

A 为原 main，B 为原 HC，C 仅将 HC 模块构造函数入口替换为 RET。
prepare.py 检查原 SHA、init-array 重定位、注册字符串和 host stub 指令，
确认改动仅四字节，非 .text 段、文件长度与相对地址全部不变。
原文件不修改；C 不能部署或执行 MTP，因为 HC 内核故意未注册。

固定 A B C / C B A，各一条原 200K off 请求，完整阶段持同一 GPU lease。
冻结判据见 protocol.json；不追加样本，不以局部诊断替代性能验收。
prepare.py 依赖相邻 hc-up-thunk-cause-20261016/prepare.py 的只读 ELF 解析。
原始运行文件保留在 .q4t-work/hc-up-registration-cause-20261016/。
