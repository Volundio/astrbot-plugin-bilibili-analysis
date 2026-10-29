# 更新日志

本插件版本遵循[语义化版本](https://semver.org/lang/zh-CN/)，记录格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.0.0] - 2026-10-30

初始发布。B 站视频链接解析插件，移植自 [koishi-plugin-bilibili-videolink-analysis](https://www.npmjs.com/package/koishi-plugin-bilibili-videolink-analysis)。

### 新增

- **链接解析**：自动解析消息中的 B 站视频内容，四种触发来源可分别开关——完整链接（含 `?p=N` 分 P）、`b23.tv` 等短链、独立 BV/AV 号、QQ 分享的 JSON 卡片
- **消息模板**：通用模板与卡片模板独立配置（emoji 风格默认），支持 `${标题}` 等占位符、`${~~~}` 拆分多条消息、大数字万/亿缩写
- **视频发送**：`${视频}` 占位符 + 独立发送开关；下载受大小上限（双保险：Content-Length 预检 + 流式超限中止）与读超时约束；视频独占转发节点发送
- **合并转发**：图文与视频节点合并为一条转发消息，可关闭，发送失败自动降级逐条
- **双数据源容灾**：视频信息优先经原版解析 API（信息与直链一次返回），其失败时自动切换 B 站官方 API（含真实分 P），直链经官方 `playurl`（wbi 签名）获取
- **等待提示**：解析前发送提示语，完成后自动撤回
- **频率限制**：单用户 30 秒内最多 5 个视频；同一会话内重复链接最小间隔去重
- **请求容灾**：每个 API 请求支持直连 / Apifox 代理 / 并行竞速三种模式

### 平台支持

- 仅支持 QQ（AstrBot 的 `aiocqhttp` 适配器，OneBot v11 协议）
