# SRV-02j2原生flags对照修正

原c69e4adc固定轮执行1个mixed native，在cudaHostGetFlags回显值
等于cudaHostAllocPortable输入值的断言处退出。原guard
COMMAND_FAILED/exit1、1.277353秒/2观察，无模型启动/HTTP；原
package/execution保持，默认pair未安装。原日志未打印实际flags，
不能推断其具体数值或已证明wrapper篡改参数。

CPU fake已验证flags逐字传递。CUDA 13.3本机header的统一寻址
说明与[官方内存文档](https://docs.nvidia.com/cuda/cuda-runtime-api/cuda_runtime_api/group__CUDART__MEMORY.html)
说明host allocation的可访问属性并非只由显式Portable/Mapped位
决定；这本身不证明本机回显值。修正探针加入相同flags的直接CUDA
分配对照，打印requested/wrapped/direct并要求后两者完全相同。
直接对照4096字节先释放，再申请余下两块；整个mixed活跃请求峰值
仍不超过32MiB。该对照明确绕过quota，只用于验证后端查询属性，
不作为生产覆盖能力；生产ELF、核心及wrapper完全不改。

新增3个纯CPU flags验收器合同；原28 wrapper/268公共/234必需/
9后端/5驱动证据复用。只重新构建native探针，新package-v2保留
原plan、guard、失败、构建身份，原package与失败不覆盖。

唯一继续轮：同锁重复修正的mixed并完成尚未执行的其他5 native、
25 HTTP生命周期、3模型启动尝试及最多7生成；新GPU窗口最多600秒。
累计native最多7，原失败仍不计通过，模型/请求预算未扩大。没有
自动重试、长验或vLLM恢复；全部通过和guard有效后才交付默认。
