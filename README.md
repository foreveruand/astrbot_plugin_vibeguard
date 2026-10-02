# astrbot_plugin_vibeguard

保护敏感隐私数据。自动对发往 LLM 的敏感词、密码与密钥进行脱敏替换，在接收到 LLM 回复后无缝还原，本地对话历史保持原文存储。

## 特性

- **智能敏感信息脱敏**：支持自定义精确敏感词以及正则表达式匹配（如 OpenAI API Key、GitHub Token、手机号等）。
- **LLM 前缀缓存友好**：同一个敏感词在有效时间内复用同一个安全占位符，不破坏 LLM 厂商的 Prompt Cache / Prefix Caching 机制。
- **滑动窗口 TTL 过期机制**：每次请求命中会刷新有效时间；超过有效期未使用的敏感词将自动失效，重新生成全新随机占位符。
- **无缝还原体验**：LLM 返回内容中的占位符会自动替换回原始信息，对终端用户无感知。
- **本地历史保留原文**：利用 AstrBot 的生命周期钩子在历史存盘前恢复原文，本地数据库中对话历史保持真实完整。

## 配置项

进入 AstrBot WebUI -> 插件管理 -> `astrbot_plugin_vibeguard` 配置页面即可进行可视化配置：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `enabled` | bool | `true` | 是否启用脱敏功能 |
| `sensitive_words` | list | `[]` | 自定义敏感词列表，精确匹配 |
| `sensitive_patterns` | list | `["sk-[a-zA-Z0-9_-]{20,}", ...]` | 正则表达式匹配规则列表 |
| `token_ttl_seconds` | int | `3600` | 占位符映射缓存过期时间（秒），每次命中刷新 |
| `placeholder_prefix` | string | `__VG_` | 占位符前缀 |
| `placeholder_suffix` | string | `__` | 占位符后缀 |
| `replace_in_contexts` | bool | `true` | 是否在发往 LLM 的历史上下文中同样执行脱敏 |

## 授权许可

[AGPL-3.0](LICENSE)
