# 人工整根回退的显式周期交接

原发布第9步check失败，完整诊断为selected root needs explicit version
registration。正确身份保护拒绝了驱动遗漏的登记；此时并非模型或SDK失败。
随后用原forward请求恢复已验主包到rev88，核心/客户端/profile/outer/version
状态字节保持。原失败不改写，不当作完成的发行接受。

修正驱动按已有原生登记合同执行：换根、显式登记该根的新周期、旧入口
check/status/profile/version/outer检查、切回并显式登记主根。最多20项
STOPPED检查，0GPU。只有当前周期未消费时才做这组发行验证；两次登记
均保留完整前驱，核对旧注册文件不变，原核心/配额/profile/outer预算不变。
自动payload回退仍保留固定根及原周期，不能使用这条人工路径重置预算。

检查登记并复用现有实现，不放宽根身份校验、不修改已封包payload。
通过后更新实际发行结论，全部master任务继续。
