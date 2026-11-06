# QSA完整纳入证据

`decision.json`是本候选的独立工程接受，保留15格全部原值、负点和
原3% NO_GO。`http-review.json`从SQLite重建156条成对响应，并记录
另外4条真实工具请求；`contracts-review.json`与`cache-review.json`
分别覆盖48层状态/数值与24请求缓存业务。

`raw-records.tar.gz`保存764原路径/685内容对象；`raw-index.json`把原
绝对路径映射到`objects/<SHA256>`，每对象已独立读取校验。原件
包括plan、SQLite、日志、连续资源观测、GPU租约、直接合同与复用
FMA负控、CPU结果。`http-resources.json`为30个请求窗口的独立资源
汇总，服务RSS按真实PID绑定，不与物理压力相加。

`delivery-records.tar.gz`和`delivery-index.json`另保存61份安装原件，
`delivery-archive.json`记录摘要。`delivery-finalized.json`、
`installed-release.json`与`release-review.json`记录发布配对、协议、
74个自有进程身份退出、端口/GPU/共享锁释放。配对最终状态是DELIVERED；
较早deployment回执的PENDING_GUARD是时间线的一部分，没有覆盖旧回执。

输入包在本机研究worktree的
`.q4t-work/qsa-softmax-adoption-20261106/acceptance/inputs.tar.gz`，
4693路径/3126内容对象，SHA见`inputs-receipt.json`。它包含两ELF、
构建库/工具/输入及来源；不是模型权重包。完整大状态在原contracts
目录，84份路径/大小/SHA见`retained-state-cache-files.json`。两臂
独立执行后先逐字节比较，之后才硬链接去重；原路径保持。

主线提取与部署后的源码/默认变化是有记录的交付动作；不要再用旧
plan要求这些可变绝对路径维持旧默认。复核旧执行请使用封存对象及
原plan绑定，不能拿当前默认冒充当时父版。所有原始失败仍在保留
研究提交和此前证据中，本包不把阶段提交或归档当成额外测试。
