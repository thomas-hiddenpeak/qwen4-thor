# 原生说话人服务验收器9CPU通过（2026-11-18）

9项/0.314秒、0GPU/HTTP。复用实际A/B/A JSON/SSE，核对源SHA、
时间/标签/usage、delta和segment关联、speaker及alignment/VAD
实际健康和各项预算；错误声源、漏segment/假标签/坏usage和能力
丢失都拒绝。原6项ASR/辅助/整包恢复证据保护通过，7防遗漏负控通过。

实际服务GPU与默认交付仍待完成；本组不是服务包已验。接着四组
现代/旧控制保留、原生TLS、自动整包回退，默认rev286保持；真实
重叠和全部master继续，总目标active。
