# 模型一键切换（astrbot_plugin_model_switch）

在 QQ 里一条命令切换 AstrBot 的全局主模型，并顺手把后台任务里写死的模型清成「跟随全局」。

- 不用重启，切完立即生效
- 改配置前自动备份，写完读回校验，失败全量回滚
- 能看出来哪些后台任务还在偷偷用旧模型

## 它解决什么问题

AstrBot 换主模型只改了对话链路，下面这些地方经常还写着旧模型：

- humanoid_core 情绪分析
- livingmemory 记忆
- autonomous_social 主动消息
- clonetts 语音识别
- 图片描述 / 长期记忆图片描述
- meme_manager、stealer、group_chat_plus、community_radar、memory_diary

本插件负责把它们统一成「跟随全局」，并在 `/切换主模型 状态` 里逐项体检。

## 安装

### 插件市场

AstrBot WebUI → 插件市场 → 搜索「模型一键切换」→ 安装。

### 手动

把本仓库整个目录放到 AstrBot 的 `data/plugins/` 下，目录名保持 `astrbot_plugin_model_switch`，然后重载插件。

## 命令

只有 `/切换主模型` 一个命令，其余都是它的子命令。

| 用法 | 作用 |
| --- | --- |
| `/切换主模型` 或 `/切换主模型 列表` | 列出可用对话模型：源名称 · 模型名、tool/图 支持、上下文长度、当前项 |
| `/切换主模型 6` | 按编号切换 |
| `/切换主模型 deepseek官转` | 按名称切换（源名称 / 模型名 / id 模糊匹配） |
| `/切换主模型 状态` | 体检：当前模型、备用模型、跟随/写死、图片描述策略、未启用插件 |
| `/切换主模型 跟随` | 清空所有后台任务里写死的模型，改为跟随全局 |
| `/切换主模型 测试` | 给当前模型真发一个 `ping`，验证通道可用 |

英文别名：`list` / `status` / `follow` / `test`。

> 只有主人可用（默认取 `admins_id`，可在插件配置里追加 `owner_ids`）。
> 注意别和 omnidraw 的 `/切换模型` 搞混。

## 使用示例

### 1. 看看现在有哪些模型

```text
/切换主模型
```

```text
可用对话模型（6 个）：
1. Balian2 · deepseek-v4.1-flash-1017  tool✓ 图✓  ctx 1M
2. grok · grok-4.7                     tool✓ 图✓  ctx 1M
3. grok2 · grok-4.7                    tool✓ 图✓  ctx 500K
4. grok3 · grok-4.7                    tool✓ 图✗  ctx 未知
5. deep中转 · deepseek-v4.1-flash      tool✓ 图✓  ctx 1M
6. deepseek官转 · deepseek-v4.1-flash  tool✓ 图✓  ctx 1M  ← 当前

⚠️ 以下模型的 id 前缀与 UI 里的源名称不一致（AstrBot 重命名源后不会改模型 id，切换仍按 id 生效）：
  · deepseek官转 → id deepseek/deepseek-v4.1-flash
回复 /切换主模型 编号 切换；/切换主模型 状态 体检；/切换主模型 跟随 改为跟随主模型；/切换主模型 测试 真发最小请求
```

### 2. 按编号或名字切换

```text
/切换主模型 2
```

```text
✅ 已切换到 grok-4.7
生效：全局（私聊+群聊）｜立即生效，无需重启

改动 4 项
· 主对话链（agent_runner + 运行时）
· 备用链（fallback_provider_ids）
· humanoid_core 情绪分析（跟随全局）
· autonomous_social 主动消息（跟随全局）

跳过 1 项
· embedding（专用模型，不动）

当前：grok-4.7  tool✓ 图✓  ctx 1M
备用：deep中转 · deepseek-v4.1-flash
```

也可以直接叫名字，源名称、模型名都认：

```text
/切换主模型 deepseek官转
/切换主模型 grok-4.7
```

### 3. 切完检查有没有漏网的

```text
/切换主模型 状态
```

```text
📊 模型切换状态
当前全局：grok · grok-4.7（grok/grok-4.7）
备用模型：deep中转 · deepseek-v4.1-flash
跟随全局：3 项（仅启用插件的后台任务）
写死未跟随：0 项 ✅
图片描述：跟随主模型（主模型支持图片）
未启用：livingmemory（已清空，重新启用后跟随全局）
视觉备用：未配置
不可切换：embedding（openai_embedding）｜STT/TTS｜rerank｜omnidraw 出图模型（固定，提示词副脑跟随主模型）｜qq_tools 视频模型
最后切换：2026-11-20 00:15:15 · 心若向阳(2261875776)
```

有漏网的就直接列出来，并给出修复命令：

```text
写死未跟随：2 项 ⚠️
  · humanoid_core 情绪分析=deepseek-v4.1-flash（建议 /切换主模型 跟随）
  · meme_manager 语义=old/provider-id（建议 /切换主模型 跟随）
```

### 4. 一次性清掉所有写死模型

适合刚装插件、或刚换过一批 provider 的时候：

```text
/切换主模型 跟随
```

```text
✅ 已改为跟随主模型

改动 5 项
· humanoid_core 情绪分析（跟随全局）
· livingmemory 记忆（跟随全局）
· autonomous_social 主动消息（跟随全局）
· clonetts 语音识别（跟随全局）
· 图片描述（跟随主模型）

当前：grok-4.7  tool✓ 图✓  ctx 1M
备用：deep中转 · deepseek-v4.1-flash
```

已经全空的话只会回一句「已经是跟随模式，无需改动」，不会白写文件。

### 5. 怀疑某个 provider 不通

```text
/切换主模型 测试
```

```text
✅ 测试成功：grok/grok-4.7 返回 OK
```

失败时会带上异常类型，方便判断是 key 错了还是网络不通：

```text
❌ 测试失败：grok/grok-4.7 ConnectError: Connection error.
```

### 6. 在配置页切换（不用发消息）

WebUI → 插件 → 模型一键切换：

1. 在「一键切换主模型」下拉里选一个 provider
2. 点保存
3. 插件自动热切换，并把结果写回「当前主模型 / 配置体检」

配置页那几个信息字段每 2 分钟自动重算一次，所以你在 UI 里增删 provider 之后，不用再手动重载插件。

## 配置页

- **一键切换主模型**：下拉选 provider，保存即切换
- **当前主模型 / 备用模型 / 配置体检 / 最后切换**：只读信息，每 2 分钟自动重算，UI 增删 provider 后不需要重载插件
- **视觉备用模型**：主模型不支持图片时，图片描述用哪个 provider
- **启用 / 额外主人**

## 切换时会改什么

只改模型相关字段，不动 embedding、STT/TTS、rerank、omnidraw 出图模型、qq_tools 视频模型。

具体会写这些配置：

- `cmd_config.json` → `agent_runner.config.model.provider_id`（主对话链）
- `cmd_config.json` → `agent_runner.config.model.fallback_provider_ids`（备用链，自动剔除已删除的 provider）
- `cmd_config.json` → `provider_settings.default_image_caption_provider_id`
- 各后台插件配置里的 provider 字段

`/切换主模型 跟随` 会把上述插件字段清空，回到 AstrBot 原生的「空值 = 跟随全局」。

## 已知限制

- AstrBot 在 UI 里重命名 provider 源时不会同步改模型 id，插件会在列表和状态里给出提示，但不会替你改 id
- 修改 `fallback_provider_ids` 后，AstrBot 的对话流水线阶段有缓存，可能需要重启一次才会停止旧的 fallback 警告（这是 AstrBot 本体行为）

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)

## 作者

君莫笑（HongJian0001）