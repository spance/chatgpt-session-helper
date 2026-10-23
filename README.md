# ChatGPT Session Helper

一组面向 ChatGPT 与本地 Codex 数据的实用工具。本仓库包含 3 个相互独立的工具：

| 工具 | 用途 | 运行环境 |
| --- | --- | --- |
| `chatgpt-conversation-exporter.user.js` | 将当前 ChatGPT 对话导出为 JSON 或 Markdown | 浏览器 Userscript 扩展 |
| `codex_project_rename.py` | 重命名或移动 Codex 项目目录，并迁移本地保存的路径引用 | Python 3.10+ |
| `switch-codex-session-provider.py` | 在 `custom` 与 `openai` 之间双向切换 Codex 会话 provider | Python 3.10+ |

两个 Python 工具只使用 Python 标准库，不需要安装第三方依赖。

## 1. ChatGPT 对话导出工具

文件：`chatgpt-conversation-exporter.user.js`

### 功能

- 支持普通对话页面：`https://chatgpt.com/c/<conversation-id>`
- 支持分享页面：`https://chatgpt.com/share/<conversation-id>`
- 自动识别用户消息和 Assistant 响应，并保持页面中的原始顺序
- 导出 JSON 或 Markdown，也可以直接复制 JSON
- 长对话可自动滚动扫描，以加载更早的消息
- 支持 ChatGPT SPA 页面切换和流式响应更新
- 兼容新版消息单元 DOM，并保留对旧版消息角色属性的回退支持
- 显示当前识别出的用户消息和 Assistant 消息数量
- 不调用 ChatGPT 未公开的后端 API

### 安装

先安装一个 Userscript 浏览器扩展，例如：

- Tampermonkey
- Violentmonkey
- Userscripts for Safari

然后按以下步骤安装：

1. 在扩展中创建一个新脚本。
2. 删除扩展生成的默认模板。
3. 将 `chatgpt-conversation-exporter.user.js` 的全部内容粘贴进去。
4. 保存脚本并刷新 ChatGPT 对话页面。

脚本加载后，页面右下角会显示“ChatGPT 对话导出”面板。

### 使用

打开普通对话或分享链接后，可在右下角面板执行：

- `重新扫描整个对话`：滚动并重新收集当前页面可访问的消息
- `下载 JSON`：导出结构化的消息列表和请求—响应分组
- `下载 Markdown`：导出便于阅读和归档的 Markdown
- `复制 JSON`：将与 JSON 文件相同的内容写入剪贴板

建议等待 ChatGPT 完成回答后再导出。长对话建议先执行一次“重新扫描整个对话”。

### JSON 结构

JSON 同时包含按页面顺序排列的 `turns` 和按请求整理的 `exchanges`：

```json
{
  "title": "对话标题",
  "source_url": "https://chatgpt.com/c/...",
  "page_type": "live",
  "exported_at": "2026-07-12T12:00:00.000Z",
  "streaming_at_export": false,
  "turn_count": 2,
  "exchange_count": 1,
  "turns": [
    {
      "index": 0,
      "role": "user",
      "content": "用户请求",
      "images": [],
      "links": []
    },
    {
      "index": 1,
      "role": "assistant",
      "content": "Assistant 响应",
      "images": [],
      "links": []
    }
  ],
  "exchanges": [
    {
      "request": {
        "role": "user",
        "content": "用户请求"
      },
      "responses": [
        {
          "role": "assistant",
          "content": "Assistant 响应"
        }
      ]
    }
  ]
}
```

主要字段：

| 字段 | 说明 |
| --- | --- |
| `page_type` | `live` 表示普通对话，`shared` 表示分享页面 |
| `streaming_at_export` | 导出时 Assistant 是否仍在生成响应 |
| `turns` | 按页面顺序保存的原始消息 |
| `exchanges` | 按用户请求和 Assistant 响应整理后的分组 |
| `images` | 消息中识别到的图片引用 |
| `links` | 消息中识别到的超链接 |

### 控制台 API

脚本加载后，可在浏览器开发者控制台中访问：

```javascript
window.__chatgptConversationExporterV3
```

重新扫描当前对话：

```javascript
await window.__chatgptConversationExporterV3.scan();
```

生成当前结构化结果和 Markdown：

```javascript
const exporter = window.__chatgptConversationExporterV3;
const result = exporter.buildResult();
const markdown = exporter.toMarkdown(result);

console.log(result);
console.log(markdown);
```

最近一次导出的结果还会保存到：

```javascript
window.__chatgptConversationExport
```

### 已知限制

- 只能导出页面当前实际挂载和显示的响应分支，不能获取未显示的历史分支。
- 图片只保存引用，不会打包下载；依赖登录状态或临时 URL 的图片以后可能失效。
- 流式生成期间导出只能得到当时已显示的内容，JSON 会通过 `streaming_at_export` 标记该状态。
- 工具依赖 ChatGPT 页面 DOM；如果页面结构发生变化，选择器可能需要更新。

## 2. Codex 项目目录重命名与迁移工具

文件：`codex_project_rename.py`

仅仅在 Finder 或终端中重命名一个 Codex 项目目录，可能会使本地会话仍然指向旧路径。此工具会在重命名或移动目录的同时，迁移已知 Codex 元数据中的项目路径。

### 处理范围

工具会检查并迁移以下位置中的项目路径及其子路径：

- `~/.codex/state_*.sqlite` 中的 `threads.cwd`
- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/archived_sessions/**/*.jsonl`
- `~/.codex/session_index.jsonl`
- `~/.codex/.codex-global-state.json`
- `~/.codex/.codex-global-state.json.bak`
- `~/.codex/config.toml`

正式执行时，它会：

1. 检查 Codex 是否仍在运行。
2. 预检查 SQLite、JSON 和 JSONL 数据。
3. 在 `~/.codex/backups/` 下创建一致性备份和 `manifest.json`。
4. 重命名或移动项目目录。
5. 迁移 Codex 元数据中的路径引用。
6. 验证旧路径是否仍有残留。
7. 发生错误时自动尝试回滚目录和元数据。

### 运行前准备

先完全退出 Codex Desktop，并确认没有 Codex CLI/TUI 进程仍在使用相关数据。工具默认会拒绝在检测到 Codex 运行时执行。

建议先查看帮助并运行 dry-run：

```bash
python3 codex_project_rename.py --help
python3 codex_project_rename.py migrate --help
```

### 基本语法

```text
python3 codex_project_rename.py migrate [选项] OLD NEW
python3 codex_project_rename.py restore [选项] BACKUP
```

`OLD` 是现有项目目录；`NEW` 可以是：

- 单独的目录名：在原父目录中重命名
- 相对或绝对路径：在同一文件系统内将项目移动到指定路径

路径中包含空格时必须加引号。

### 示例一：在原位置重命名

先预览：

```bash
python3 codex_project_rename.py migrate \
  /Users/me/projects/old-name \
  new-name \
  --dry-run
```

确认输出中的旧路径、新路径、数据库记录和元数据文件都正确后，去掉 `--dry-run` 正式执行：

```bash
python3 codex_project_rename.py migrate \
  /Users/me/projects/old-name \
  new-name
```

结果目录为 `/Users/me/projects/new-name`。

### 示例二：移动到另一个目录

```bash
python3 codex_project_rename.py migrate \
  /Users/me/projects/old-name \
  /Users/me/archive/new-name \
  --dry-run

python3 codex_project_rename.py migrate \
  /Users/me/projects/old-name \
  /Users/me/archive/new-name
```

目标路径必须尚不存在。为避免非原子复制带来的风险，工具会拒绝跨文件系统移动；这种情况应先自行复制或移动目录，再使用下一节的 `--metadata-only` 迁移 Codex 元数据。

### 示例三：目录已经被手动重命名

如果目录已经从 `old-name` 手动改成 `new-name`，但 Codex 元数据仍然保留旧路径，可使用 `--metadata-only`。该模式只迁移元数据，不再操作目录：

```bash
python3 codex_project_rename.py migrate \
  /Users/me/projects/old-name \
  /Users/me/projects/new-name \
  --metadata-only \
  --dry-run

python3 codex_project_rename.py migrate \
  /Users/me/projects/old-name \
  /Users/me/projects/new-name \
  --metadata-only
```

### 使用非默认 Codex 数据目录

`migrate` 子命令可通过 `--codex-home` 指定状态目录：

```bash
python3 codex_project_rename.py migrate \
  /old/project/path \
  /new/project/path \
  --codex-home /path/to/codex-home \
  --dry-run
```

### 从备份恢复

成功迁移后，终端会打印本次备份目录，例如：

```text
~/.codex/backups/project-rename-20260716-183000-12345
```

如需恢复，将该目录传给 `restore`：

```bash
python3 codex_project_rename.py restore \
  ~/.codex/backups/project-rename-20260716-183000-12345
```

恢复命令从备份的 `manifest.json` 读取 Codex 数据目录、旧路径和新路径。若迁移时包含目录重命名，恢复时也会在条件安全时将目录改回旧名称。

### 参数说明

| 参数 | 适用命令 | 说明 |
| --- | --- | --- |
| `--dry-run` | `migrate` | 显示完整迁移计划，不修改目录、文件或数据库 |
| `--metadata-only` | `migrate` | 只迁移 Codex 元数据，不重命名或移动项目目录 |
| `--codex-home PATH` | `migrate` | 指定 Codex 数据目录，默认是 `~/.codex` |
| `--allow-running` | `migrate`、`restore` | 忽略 Codex 进程检测；仅在确认属于误报时使用 |
| `--version` | 主命令 | 显示工具版本 |

不要把 `--allow-running` 当作常规选项。Codex 仍在写入会话或 SQLite 数据时强制迁移，可能造成状态不一致。

## 3. Codex 会话 Provider 双向切换工具

文件：`switch-codex-session-provider.py`

该工具在 `custom` 与 `openai` 之间批量切换本地 Codex 会话的 `model_provider`。位置参数表示切换后的目标 provider：

```text
custom  表示 openai -> custom
openai  表示 custom -> openai
```

它会递归扫描：

- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/archived_sessions/**/*.jsonl`
- `~/.codex/state_*.sqlite` 中的 `threads.model_provider`

JSONL 中只修改 `session_meta.payload.model_provider`，不会替换用户消息、Assistant 响应或工具输出中偶然出现的 `model_provider` 文本。SQLite 也只更新与源 provider 完全相等的记录，不会覆盖其他 provider。

### 运行前准备

执行写入前请完全退出 Codex Desktop 和 Codex CLI，避免它们同时写入会话文件或数据库。先通过帮助和 dry-run 检查切换方向及记录数量：

```bash
python3 switch-codex-session-provider.py --help
```

### 从 custom 切换到 openai

先预览：

```bash
python3 switch-codex-session-provider.py openai --dry-run
```

确认输出显示 `custom -> openai` 后正式执行：

```bash
python3 switch-codex-session-provider.py openai
```

### 从 openai 切换到 custom

先预览：

```bash
python3 switch-codex-session-provider.py custom --dry-run
```

确认输出显示 `openai -> custom` 后正式执行：

```bash
python3 switch-codex-session-provider.py custom
```

### 使用非默认 Codex 数据目录

```bash
python3 switch-codex-session-provider.py custom \
  --codex-home /path/to/codex-home \
  --dry-run
```

### 参数说明

| 参数 | 说明 |
| --- | --- |
| `custom` 或 `openai` | 必填；指定切换后的目标 provider |
| `--dry-run` | 统计将修改的会话文件、`session_meta` 和数据库记录，但不写入 |
| `--codex-home PATH` | 指定 Codex 数据目录，默认是 `~/.codex` |

### 重要注意事项

- 这是批量切换工具，会修改扫描范围内所有与源 provider 相等的会话。
- 脚本不会修改 `custom`、`openai` 之外的其他 provider。
- `custom` 和 `openai` 原本混合存在时，一次切换会将二者合并为同一个值。之后反向执行无法判断每条记录最初属于哪一方，因此反向切换不等同于逐条恢复历史状态。
- 当前工具不会自动创建备份；重要数据建议先备份 `~/.codex`，并始终先执行 `--dry-run`。

## 隐私与安全

- 所有工具均在本地运行，不会主动上传对话或 Codex 元数据。
- ChatGPT 导出文件以及 `~/.codex` 会话中可能包含个人信息、代码、路径、账号信息或其他敏感数据，请妥善保管。
- 修改 Codex 本地状态前应先完全退出 Codex，并核对 dry-run 输出。

## Disclaimer

本项目不是 OpenAI 官方产品，与 OpenAI 无隶属或合作关系。ChatGPT 页面结构和 Codex 本地数据格式可能随版本变化，使用前请先检查工具输出并保留必要备份。
