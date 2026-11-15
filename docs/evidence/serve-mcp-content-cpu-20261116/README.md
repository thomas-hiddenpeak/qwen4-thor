# MCP 原生内容、资源读取及错误结果 CPU 证据

2026-11-16，非性能业务推进。26项不同C++最终通过（27次执行：首组
18/19，实际输出合同修复后8/8；0 skip），官方SDK7.47.0新增1项
JSON/SSE/续接检查通过，7项来源负控通过。主机构建最终零警告；
完整引擎零警告构建通过，SHA为
`fb3d5427e0a913cca7d69c0e5088a5f59b1da01104d6bb10af130dc2909d5482`。0 GPU，rev126默认与发布记录SHA保持。

## 实际改动

- 原生text/image/audio/resource_link/resource以及resources/read结果
  均有有界类型校验；通用base64合同与图片像素解码分离，音频/PDF
  不会因不是图片而在此层被误拒绝。原始元数据保持。
- resources/read只通过原MCP endpoint发起，需资源能力及父拥有者
  明确许可，继续原DNS/CIDR/TLS/认证策略。file:///etc/hostname
  测试只返回远端fixture文本，本机路径不参与读取。未知结果不重试；
  完整响应的内容错误被记录为已知失败。
- isError工具原生结果保留content/structuredContent/_meta，持久
  认证和程序恢复不丢失原数据；标准error仍保留失败含义。输出器
  只允许已calling且isError/content一致的失败结果携带output。

## 失败与复验

首次CPU失败发现输出器仍拒绝failed+output，已修真实发布路径；
仅重验失败程序项和7项受影响输出合同，复用其余18项。构建01/02/04
依次为测试的循环复制警告、Parse参数类型、缺失声明头，原日志保留；
03/05最终主机构建通过，不删除早期失败。

5bdccdff远端CI43741411586的两项程序媒体边界失败日志压缩保留。
043e5786已修发布边界，先前8项复验覆盖第一项；本组又实际通过
跨程序媒体配额项。043e5786远端CI43742617506已全部通过，回执保留。

## 尚须完成

本组是原生结果/传输与错误恢复合同，不是MCP完整交付。模型媒体
准备、资源的认证冻结/预算、跨操作会话、connector/tunnel映射、
完整SDK/真实模型业务/恢复资格/可回退发行与main合并继续；
所有Serve总计划任务保持active，不以本证据替代这些实现。

两份required列表、原始stdout/stderr/json、官方SDK消费的真实C++
输出和source-sha256.json可复核。没有模型质量或性能结论。
