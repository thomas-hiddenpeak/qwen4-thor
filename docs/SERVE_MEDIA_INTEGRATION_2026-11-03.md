# SRV-02i：媒体预检与解码容量接线合同

2026-11-03事前冻结，父8837e680，可靠性任务。上一轮是实际开发/CPU证据
推进。GPU仍有同一vLLM PID2336565，不干预；本批先成组实现与CPU/构建。
默认3fd31a21不变。修改已有来源会使02h旧包在当前工作区的source gate
拒绝，这是有意的候选更新；旧包、source.bundle与原始记录完整保留。
不绕过旧gate，也不把旧CPU成功当新候选或GPU接受。

## 输入与资源合同

媒体继续为`--allow-media`显式实验范围；公开text schema不扩成通用VL。
仅user媒体；image_url允许字符串或对象{url,detail?}，detail仅省略/auto。
video_frames为2..32帧，同一URL结构；part只含对应键和可选匹配type。
拒绝未知/混合字段、video_url、其它role和未实现detail档，不静默忽略。
文本part既有合同保持。raw base64及精确data:image/png;base64, / image/jpeg
允许；标准字母表、正确末尾padding、零unused bits，拒绝空白、URL-safe、
截断、任意位置padding和MIME/实际格式不符；remote URL继续拒绝。

沿用已冻结核心策略：8items、32frames/item、总encoded16MiB、单源边长
16384/16777216pixels、源RGB累计128MiB、65536patches、workspace2GiB。
新增每次decoder所拥有C分配256MiB上限，计分配前缀；覆盖PNG zlib及
stb内部临时缓冲，非仅最终RGB。此值容纳16M像素RGBA16bit缓冲与RGB转换
的常见并存，但不承诺上限组合全部成功。realloc按可见所有权净增长记账，
不宣称覆盖allocator内部瞬态或物理RSS；全机RAM硬保护仍未证明。
有效策略和实际attention函数shared上限在config/capabilities公开。

全部item/frame头部/容量/几何在任何解码前完成。媒体占位符种类和顺序
匹配后，在槽位准入前以展开后长度再次计算输出/槽位预算；槽位交接后
仍复核具体容量。对必然超限请求不做vision GPU工作。processor复用同一
几何/源计划，解码后尺寸必须与计划相符；不保留第二份resize公式。

decoder配额回调不抛异常，待stb清理后区分quota(400/media_decode_limit)
与真实malloc失败(既有bad_alloc503)。头部/形状/预算为400且稳定code/param；
未知设备能力在显式媒体启动时失败关闭。取消沿用既有409及首因计数，
至少在头部/item/frame与GPU提交前检查，单次C解码或GPU kernel不承诺抢占。
正常resize/像素、模板、模型算子和精度保持；32帧宽整数修复独立记账。

## 验证与交付边界

本批成组后执行：生产/validation零警告构建、既有host OFF/ON/必需合同、
生成合同/审计防漂移、80预检合同、36实际processor几何、原processor
逐分配失败与四组参考像素，再加输入形状/base64/角色、展开槽位预算、
decoder配额/回收/恢复和取消定向CPU案例。不因文档/封存变化重跑模型。
保留第一轮全部输出，具体失败只重验受影响项，禁止物理OOM试验。

在GPU交接或空闲后另冻结组合候选身份、次数/墙钟及完整驱动，并在一把
跨会话lease内覆盖02h未执行所有权/错误/上传和真实工具闭环，加02i
过限拒绝/同PID恢复、合法图/视频、取消/设备能力。未完成前不替换默认，
不宣布媒体或工业化目标完成。无需再询问已获授权的开发/阶段提交。
