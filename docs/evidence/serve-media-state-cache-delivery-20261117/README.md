# 媒体状态缓存与视觉分块默认交付 rev214

实际包`f02ffbffa78aa24379a54e835592e00a9c3d6304ff62ac177a848ae1ae11e25b`，generation
`bbd132e81985145436e46f7121795596525682acb9507c7311780d0adeb45413`；引擎`2de5630966e0f3d2602b205aad2585e0ce211fdadaf655047c1ad2e03af52df3`。
19项STOPPED检查/发布、实际整根回退与再前进完成，283.040秒，
0新增GPU。最终STOPPED，outer已配置且累计启动0次。

实际图片/视频内容、完整视觉块、三轴位置及偏移共同绑定缓存；
分块prefill保留全局视觉特征与位置。原标准视频短尾数值回归已修复，
完整/冷/热输出和logprobs一致，真实SSD写入/恢复成立。
[业务证据](../serve-media-state-cache-business-20261117/README.md)中，新引擎
正式接受6启动26请求；含原失败与诊断累计13/60、636.757秒。

发布预检揭示false→true能力迁移被旧规则拒绝。新包仅增加已验的
发布迁移模块；6项CPU/7次执行通过，随后本记录完成真实STOPPED迁移。
引擎、其发布记录、客户端、live controller与helper保持业务验收身份；
不声称新整包另跑GPU。第一次prepare失败、归档权限拒绝均保留，
工作目录改为0700后重做；没有放松保护。

核心/client/outer账本字节保持，profile只做完整合同的显式能力升级，
history/pending/selected/deadline/clock/configuration保持。先新包迁移，
再安装已实跑的原现代保留根，回退后恢复各自原周期，仅递增revision。
人工回退根`f046a6d535d5`、自动payload`ffa5c43848ee`
都使用当前引擎，不冒充独立旧引擎回退；rev204原包和状态另保留。

退休generation/版本登记各两份可恢复归档，0删除。PR38最终检查和
实际合main继续；长稳由用户执行，全部master未完成项继续推进。
