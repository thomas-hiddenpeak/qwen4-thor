# 噪声抑制参考与使用边界（2026-11-19）

只读取Xiph官方SpeexDSP镜像的固定提交
[`7a158783`](https://github.com/xiph/speexdsp/tree/7a158783df74efe7c2d1c6ee8363c1e695c71226)：
COPYING、README、preprocess.c与公开preprocess头，共4文件51905字节，
SHA256见sources.json。原件保留在私有noise-reference-01，不修改reference/。

借鉴内容是持续频域处理、噪声估计及逐频率增益的工程方向。q4t新模块
独立实现固定512点FFT、sqrt-Hann重叠相加、滚动谱最小值和Wiener
增益；这不是SpeexDSP源码移植或Ephraim–Malah实现等价声明。未复制
或链接其源码、不增加库/服务/权重运行依赖；SpeexDSP原许可为BSD类
三条款，当前只作参考，若以后复制代码须另行保留许可与来源。

该记录不构成噪声抑制、真实语音或接口验收。CPU基础模块与后续同源
波形接线、实际业务在各自证据中记录；关闭预处理继续走原路径。
