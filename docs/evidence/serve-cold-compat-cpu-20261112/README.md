# R07 严格验证交接与旧控制器冷启动适配

2026-11-12：10项CPU首次全部通过，0.981879秒；真实当前v16接受包的
独立适配及接受来源核验通过，2.584457秒。0GPU、无默认/旧包修改。

严格GPU驱动只在显式`--inherit-lease --inherit-fd N`时传递最多8个
已有FD；普通路径不增加继承，错误/重复/关闭的FD在查询和业务前拒绝。
CPU用真实子进程和管道核对传递及原租约保持，监控结果使用CPU夹具。

`q4t service version-adapt`将旧包复制为独立适配包，精确增加冷启动
提示读取与探测资格分支两处；可组合既有profile状态表示适配。
连续模型事实保持false，仍须真实Guardian授权与原有有界探测。
额外源码变动、非原语义的continuous字段或不一致封包被拒绝。
新设备域包的版本登记要求primary/fallback均能处理冷路径。

真实来源为rev64包a3bb4ec8，适配目标b5e14f73；控制器SHA为727ce2d7，
与当前控制器逐字节一致，模型引擎及其清单保持。原接受元数据/业务
证据已核对，此适配本身仍明确`business_acceptance=false`。不以封包
通过替代本轮完整冷启动业务；既有R06其他行为证据按身份复用。

`plan.json`、`cpu.*`、`retained-check.*`、`retained.json`、`source.json`
分别保留范围、实际结果、原包/默认保护与源码身份。真实设备域启用、
NVML失能短验和可回退发行继续；Serve总目标保持active。
