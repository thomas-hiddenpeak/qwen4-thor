# Responses声明、线协议修复与发布合同证据

2026-11-09。10不同C++、10不同Python、4不同官方SDK后台场景通过；
最后只重验概率投影受影响的SDK流场景。3次构建通过且零警告；无GPU、
无默认替换。这不是完整A07/A08交付，总计划继续。

修复实际缺陷：所有响应遗漏有效tool_choice，成功usage遗漏
cache_write_tokens，未include概率的续读事件遗漏必需logprobs数组。
现在如实回显选择、计数和空数组，不修改模型计算或虚构缓存命中。
新增能力/config/OpenAPI，声明v3复制完整应用schema和所有引用内置的
OpenAPI，包离开仓库仍可验证。生成工具仅开发期使用，不新增运行时依赖。

1150节点SDK图有来源/许可/编译器摘要；旧656节点运行时图重生成字节相同。
51份新JSON/SSE严格通过、23份旧会话对象通过。旧同步50份中的14份、旧
后台51份中的44份仍是协议缺陷负控；未覆盖或填补原线包来宣布通过。

首次8项C++7通过，新增夹具在Begin替换snapshot后仍引用旧字段，修正只
重验该项；修复投影后另外2项回放/概率检查通过。schema第一轮6/9通过，
核对官方HTTP活动/取消usage:null与固定TypeScript不同，保留合法空值；
第二轮发现真实投影logprobs遗漏并修复，三项受影响检查第三轮通过。
最终使用declarations-sdk-01的cancel/json/crash和declarations-sdk-02的
stream原件，不覆盖首次失败的stream记录。包验证与未改config检查复用。
首次来源检查提示6个已改锚点，逐项审查刷新后286锚点/26后端/42入口/
41Chat字段/21Completions字段/5突变负控通过。生成器准备曾误写一个
header-only路由的.cpp路径，改为实际.h后生成成功；该错误未运行模型。

官方线协议核对：[创建](https://developers.openai.com/api/reference/resources/responses/methods/create)、
[取消](https://developers.openai.com/api/reference/resources/responses/methods/cancel)。
usage为空时不能伪造0，completed/incomplete必须有真实usage。
继续后台实际模型、可回退发布及前台未知提交/对象恢复/其余执行选项。
