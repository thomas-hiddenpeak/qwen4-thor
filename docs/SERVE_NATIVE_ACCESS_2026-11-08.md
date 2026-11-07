# N01/N02/N03：原生受控接入

状态：已实现、针对性验收并可回退交付。继续A01/A02和其余
[总计划](SERVE_MASTER_PLAN_2026-11-08.md)，不把阶段当终点。

## 要解决的问题

去掉nginx强制依赖后，TLS、内网接入、证书生命周期和浏览器接入
仍是q4t必须提供的原生能力。默认保持本机配置，部署时可选择启用；
“可选启用”不等于不实现。普通外部代理继续兼容。

1. N01：原生IPv4/IPv6监听与实际对端CIDR、独立Key；TLS1.2/1.3
   握手和非阻塞JSON/SSE。复用现有恢复准入、额度和后端身份检查，
   不把客户端送来的代理头当真实来源，不重放请求。
2. N02：证书链/私钥匹配、有效期、名称、CA与可选mTLS；显式轮换
   先校验后原子切换，新连接使用新上下文，已有SSE完成。坏证书或
   读取超时保留仍有效的旧上下文，状态可诊断，不重启模型。重复
   更新不能无限创建未回收辅助进程；持久配置与运行身份明确绑定。
3. N03：可配置Origin白名单、浏览器预检、允许方法/请求头、凭据
   规则、Vary以及错误响应上的CORS；预检不消耗模型/RPM/TPM预算，
   实际业务仍必须通过来源、Key和恢复准入。

## 实施约束

TLS使用系统OpenSSL 3，不自行编写密码学，不复制nginx代码。
原生入口通过固定库路径按需加载TLS符号：未启用TLS时不加载
libssl/libcrypto；缺库或无TLS构建只阻止启用TLS，不拖停已有核心。
TLS构建需要开发头文件。本机只有3.0.13运行库，开发包将解包到
`.q4t-work/serve-native-access-20261108/deps/`，不改变系统库版本。

复用已有HTTP解析器，通过受期限约束的输入/输出适配器接入TLS，
保留原有分帧、请求体和背压边界。证书失败不得自动回退明文。
证书加载/轮换的阻塞I/O必须隔离，不允许在主事件循环直接阻塞读取。
外部代理无需专用nginx模块；真实客户端来源转发仍列N05的可信配置。

## 针对性验证和上限

先成组开发，再执行实际C++入口加CPU后端的TLS/IPv4/IPv6/CIDR/
鉴权/CORS/证书错误与轮换/握手慢客户端/流中断用例。无TLS构建
验证明文功能和明确启用失败；不会把测试替身说成真实模型通过。
仅重验直接受影响的HTTP解析/监听和配置合同，不跑全量host。

实际模型组上限2启动/8Chat/900秒，同一严格GPU租约，覆盖TLS
JSON/SSE、资料工具、证书更新前后同PID及错误隔离；执行前冻结
具体请求和配置。无完整性能、完整模型质量或长稳。未指定生产域名/
证书时使用明确标记的短期CPU/GPU夹具，不替用户部署公共服务。

引用的官方合同：[非阻塞TLS错误处理](https://docs.openssl.org/3.0/man3/SSL_get_error/)、
[证书与私钥](https://docs.openssl.org/3.0/man3/SSL_CTX_use_certificate/)、
[对端验证](https://docs.openssl.org/3.0/man3/SSL_CTX_set_verify/)。
具体版本以本机库和匹配开发包为准；库可选不等于TLS功能已验收。

## 凭据更新和撤销合同

v4原生包封存证书、私钥、可选客户端CA/CRL的绝对路径；凭据文件
由部署者提供，原子替换文件后使用`service tls-reload`。包和模型
身份不随换证变化；重新启动入口仍读取相同路径，旧v1—v3包不改写。
私钥只允许当前UID私有普通文件；CA/证书/CRL必须由当前UID持有、
不可被组或其他用户写入。读取在独立exec内，2秒期限，清理最多
100毫秒；未确认退出保留服务级抽象Unix租约，下一入口代次不能
叠加阻塞读取器。私有管理接口不占模型、业务额度或恢复预算。

更新先校验证书链、有效期、名称、私钥和可选CA/CRL，再原子换
上下文；状态提供证书指纹、代次、有效截止时间和7天内到期提示。
新握手必须仍在有效期内；旧证书到期后拒绝新TLS连接，无明文降级。
已有连接保持其握手时的上下文完成，不自动重放或重启模型。

mTLS可配置本地签名CRL，更新时校验签名与nextUpdate，新客户端
握手检查叶证书撤销；CRL过期同样拒绝新握手。默认不隐式联网拉取
OCSP/CRL，避免新增在线外部依赖；CA/CRL轮换由部署配置显式执行。
未配置CRL时状态明确不执行叶证书撤销检查。服务端证书的信任与
撤销由客户端策略校验；客户端撤销不追溯已经建立的SSE，紧急取消
使用现有请求取消/停止入口操作，不把换证伪装成即时清退所有会话。

## 当前验证进展

71项不同CPU通过。最终Release入口ba6baf0e：19项TLS、17项旧入口、
4项代次、1真实Chromium浏览器通过；4项驱动/封包导入通过。26项
配置/HTTP依赖摘要不变，复用已有通过记录。最初夹具权限、浏览器
启动及真实TLS早拒绝RST失败保留，后者已修复并有直接回归。

Guardian重新构建与已接受7a57af71字节一致，独立CUDA探测仅复用
接受产物、不运行。实际业务计划1启动/6Chat、累计上限2/8/900秒，
绑定本机LAN地址和自身/32白名单；同机网卡测试不等于异机部署。
上述计划已通过；没有追加完整性能/模型矩阵。

## 实际业务与交付

实现提交`fcb5a7d4`。1模型/6Chat，驱动52.810秒、严格租约53.720秒，
51次外层NVML均OK；TLS1.2 JSON、TLS1.3 SSE、合法换证、坏证书保留
旧上下文、真实`docs/MODEL.md`资料工具/引用、结构化SSE均通过。
模型、控制器、Guardian与入口进程身份保持；grammar 4/4回收，
持久停止、端口/GPU/租约释放。[177份原件](evidence/serve-native-access-20261108/README.md)
包含初次CPU/浏览器夹具失败、实际早拒绝缺陷修复和审计读取器错误。

默认服务v4 `cc32f970`，发布元数据`352c368d`；原生入口`ba6baf0e`，
Guardian `7a57af71`。引擎`c6285915`和元数据`a48dcb67`保持不变。
默认保留原loopback监听、Key、配额与CIDR策略，TLS/CORS按配置启用；
短期测试证书不安装，系统信任不改变。默认与TLS验收包44个运行文件
摘要相同，明文回归已覆盖最终Release。未因配置不同重跑整套推理。

`build/service-native-state`原ledger字节、服务ID和恢复预算保持，仍为
STOPPED；安装仅离线核对/CLI help/status，0模型/0Chat。旧v3 e7652b4c
包和b08eea25发布元数据保留，可按
[回退回执](evidence/serve-native-access-20261108/delivery-01/rollback/receipt.json)
恢复两项描述文件与服务软链接；必须保持实际ledger预算，不能重新初始化。

## 启用与操作

- `service prepare`默认原生入口；策略v3支持明确IPv4/IPv6监听地址、
  `server_name`及`allowed_client_cidrs`。非loopback必须TLS，来源按
  实际socket对端检查；信任代理头/路径前缀在N05继续实现。
- 配置TLS时传`--certificate`和`--key`绝对路径；可选`--client-ca`
  开启mTLS，`--client-crl`开启本地叶证书撤销检查。凭据不放入不可变
  包；私钥0600、证书/CA/CRL不可被组/其他用户写入，均当前UID持有。
- 策略v3的`cors`设置`allowed_origins`、`allowed_methods`、
  `allowed_headers`、`exposed_headers`、`allow_credentials`、`max_age`。
  凭据模式不能使用通配Origin；未配置CORS保持关闭。配置是启用选择，
  对应能力已实现且有真实浏览器验证。

运行中的服务原子替换原路径文件后，可直接执行：

```bash
build/service/bin/q4t service tls-status --state build/service-native-state
build/service/bin/q4t service tls-reload --state build/service-native-state
```

状态包含当前有效指纹/代次/截止时间、7天到期提示及更新错误；更新
不改变模型PID。当前默认处于STOPPED，无活控制器时status明确UNKNOWN，
不会为了查询启动服务。无TLS构建与明文运行均不加载libssl/libcrypto。

这一交付完成N01—N03；OpenAI输入/消息/生成与其他原生能力仍按总计划
继续，不以这次交付结束总任务。没有宣称异机部署、完整质量或长稳。
