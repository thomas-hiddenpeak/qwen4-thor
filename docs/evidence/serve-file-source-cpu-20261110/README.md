# A09.5 FI01 来源合同与受控字节读取

2026-11-10。13项CPU全过（10新增、3直接受影响），0.264秒；仅组件接受，文件输入尚未进入模型执行，FI02—FI05全部继续。默认Uploads cdb5ced9保持STOPPED，0模型/GPU。

实际检查了Chat/Responses来源互斥与nullable、data URI/base64、预算/取消/期限、真实File多块含二进制原字节、归属隔离、大小拒绝先于pin、完整性损坏拒绝及读引用清理。文件名只作元数据，存储MIME来自原File。

首构建因新夹具用了不存在的Null helper失败；改为项目Json空值，原件保留。随后host构建零警告；不是GPU/模型质量或完整API的验收。

原件：`source-plan.json`/`source-required.txt`、两次build、`source-cpu-01.*`、`source-sha256.json`和`decision.json`。来源版本：固定Node SDK7.47.0与官方file-inputs，见`source-review.json`。
