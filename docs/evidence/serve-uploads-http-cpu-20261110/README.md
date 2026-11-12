# A09.4 原生Uploads HTTP/TLS针对性CPU

16 C++、3合同、7实网Python和1既有原生取消检查全部通过。
必要host/实际引擎/TLS入口构建零警告，0模型/GPU；两条固定
官方SDK直接核心与TLS/prefix链均实际运行17MiB+13字节Part、
MD5/顺序/原字节、隔离/完成核对和取消。具体请求数见decision。

满48MiB数据工作预留仍可取消；两项实际上传占满客户端data槽时，
原生control取消成功，迟到Part均返回409。完整MIME后的坏HTTP
trailer不发布Part，提前413正常返回；旧容量不扩大，合法>4KiB
完成JSON可达核心。父准入仅使用身份绑定测试适配；SDK、原生TLS、
生产handler与磁盘worker是真实实现。原请求取消的保留容量回归通过。

该证据绑定候选ebbd4c6b/入口4083131f，默认6a7404c7保持STOPPED。
发行审查发现重复Part产生重复chunk引用，旧v11无法读取这种
manifest；继续修复为旧格式可读的独立块并定向验证，不能据当前
HTTP接受直接发布。随后适用业务/发行与全部后续任务继续。
