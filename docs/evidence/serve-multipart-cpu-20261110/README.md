# A09.1 增量 multipart 组件针对性 CPU

11项C++通过，必要host构建零警告。固定官方SDK7.47.0实际编码2种multipart请求（Files及UploadPart），C++逐3字节解码并核对原256字节/Unicode文件名/所有字段。SDK端响应仅是注入夹具，0网络/模型；本组不能替代HTTP API或持久存储业务验收。

其他用例覆盖所有分片大小、二进制假边界、MIME padding/折行、截断、重复头/参数、配额和回调失败。初次SDK目录误传node_modules/openai而非安装根目录，尚未运行C++；修正执行环境后完成，失败原件保留，生产不变。

继续A09合同/二进制持久IO/实际传输/Uploads/消费/Batch及全部任务；默认6a90506e保持STOPPED，无GPU。
