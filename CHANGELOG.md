# Changes / 版本更新

## 2026-11-03.1 — Configurable industry classification / 通用行业分类

- Added an Analysis & Classification page for industry context, category definitions and keywords, suggested actions, priority/exclusion terms, and region aliases.
- Replaced automotive/voice-specific prompts, hidden filters, vendor rankings, and Webhook label conversions with configurable rules. Default categories are industry-neutral; unclear keyword matches remain unclassified and pending review.
- Preserved arbitrary organization and language names. Webhooks keep region as a single string and languages as an array; field mapping remains editable.
- Kept full article text and section evidence, preserved validated titles, and corrected mixed trial/implemented summaries. Long-text caches include the analysis profile identity.
- Profile changes retain historical articles and sent payloads. Revision/state checks and profile identity prevent stale analysis from overwriting current records.
- Retained the five stage values for compatibility, with cross-industry meanings documented in both READMEs. Existing model settings, source/rule configuration, schedules, and delivery history are retained on upgrade; back up private data before upgrading.
- Validation: 46 isolated tests; dependency/startup check; browser checks for profile editing, persistence, draft preservation, dynamic review options, and single-article mock analysis. No live model or real Webhook was called. Linux/macOS remain untested.

### 中文

- 新增「分析与分类」，可编辑行业背景、分类说明及关键词、建议动作、优先词、排除词和地区别名。
- 移除车载语音专用提示、隐含筛选门槛、厂商来源偏好及 Webhook 行业标签转换；默认通用分类，无法判定时保留未分类和待审核。
- 主体和语言不限固定名单；地区仍输出单个字符串，语言输出数组，字段映射继续可编辑。
- 保留全文和分段证据，保留已校验标题，修复混合试验/已实施事实的摘要误判；长文缓存按分析配置区分。
- 配置变更不改历史新闻与已推送内容；修订号、状态及配置校验防止过期分析回写。
- 保留五个兼容阶段值，中英文说明已补充跨行业含义。升级保留原模型、来源规则、计划时间及历史数据；升级前备份个人数据。
- 验证：46 项隔离测试、启动依赖检查及浏览器配置/持久化/草稿/动态选项/模拟单条分析。未调用真实模型或 Webhook；Linux/macOS 未实测。
