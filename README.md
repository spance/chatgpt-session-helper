# ChatGPT Conversation Exporter

一个用于导出 ChatGPT 单个对话的浏览器 Userscript，支持普通聊天页面和分享页面，可将当前对话保存为 JSON 或 Markdown。

## 功能

* 支持 ChatGPT 普通对话页面：

```text
https://chatgpt.com/c/<conversation-id>
```

* 支持 ChatGPT 分享页面：

```text
https://chatgpt.com/share/<conversation-id>
```

* 自动识别用户消息和 Assistant 响应
* 按原始顺序导出全部消息
* 自动整理为“请求—响应”分组
* 导出 JSON
* 导出 Markdown
* 一键复制 JSON
* 支持长对话自动滚动扫描
* 支持 ChatGPT 流式生成响应
* 支持 ChatGPT SPA 页面切换
* 显示当前识别到的用户消息和 Assistant 消息数量
* 不依赖 ChatGPT 未公开后端 API

## 安装

需要先安装支持 Userscript 的浏览器扩展，例如：

* Tampermonkey
* Violentmonkey
* Userscripts for Safari

然后：

1. 在扩展中创建一个新脚本。
2. 删除默认模板。
3. 将 `chatgpt-conversation-exporter.user.js` 的内容粘贴进去。
4. 保存脚本。
5. 刷新 ChatGPT 对话页面。

脚本会自动在页面右下角显示导出面板。

## 使用方法

打开一个 ChatGPT 对话页面或分享链接后，右下角会出现导出工具：

```text
ChatGPT 对话导出

用户 12 条，助手 12 条

重新扫描整个对话
下载 JSON
下载 Markdown
复制 JSON
```

建议在 ChatGPT 回答完成后再执行导出。

对于较长的对话，可以先点击：

```text
重新扫描整个对话
```

脚本会尝试滚动对话区域，加载并收集更早的消息。

## JSON 格式

导出的 JSON 同时包含原始消息列表和请求—响应分组。

```json
{
  "title": "对话标题",
  "source_url": "https://chatgpt.com/c/...",
  "page_type": "live",
  "exported_at": "2026-07-12T12:00:00.000Z",
  "streaming_at_export": false,
  "turn_count": 4,
  "exchange_count": 2,
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
      "content": "ChatGPT 响应",
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
          "content": "ChatGPT 响应"
        }
      ]
    }
  ]
}
```

主要字段：

| 字段                    | 说明                              |
| --------------------- | ------------------------------- |
| `page_type`           | `live` 表示普通聊天页面，`shared` 表示分享页面 |
| `streaming_at_export` | 导出时 ChatGPT 是否仍在生成响应            |
| `turns`               | 按页面顺序保存的原始消息                    |
| `exchanges`           | 按用户请求和 Assistant 响应整理后的分组       |
| `images`              | 消息中识别到的图片地址                     |
| `links`               | 消息中识别到的超链接                      |

## Markdown 格式

Markdown 文件按问答组组织：

```markdown
# 对话标题

## 第 1 组

### 请求

用户请求内容

### 响应

ChatGPT 响应内容
```

适合用于：

* Obsidian
* Git 仓库
* 知识库
* RAG 数据预处理
* 对话归档
* 后续文本分析

## 动态页面支持

ChatGPT 使用 SPA 页面结构，切换对话时通常不会刷新整个网页。

脚本会监听：

* URL 路径变化
* 新消息插入
* Assistant 流式响应更新
* 对话内容动态加载

当同一条 Assistant 响应持续生成时，脚本会更新已有记录，而不是将每次 DOM 变化识别成一条新消息。

点击导出按钮时，脚本还会重新扫描一次当前页面，以减少遗漏。

## 控制台 API

脚本加载后，可通过浏览器控制台访问：

```javascript
window.__chatgptConversationExporterV3
```

重新扫描当前对话：

```javascript
await window.__chatgptConversationExporterV3.scan()
```

构建当前结构化结果：

```javascript
const result =
  window.__chatgptConversationExporterV3.buildResult();
```

生成 Markdown：

```javascript
const exporter =
  window.__chatgptConversationExporterV3;

const result = exporter.buildResult();
const markdown = exporter.toMarkdown(result);

console.log(markdown);
```

最近一次导出的结果还会保存到：

```javascript
window.__chatgptConversationExport
```

## 实现原理

脚本主要通过 ChatGPT 页面 DOM 提取对话内容。

核心识别属性包括：

```css
[data-message-author-role="user"]
[data-message-author-role="assistant"]
```

同时结合以下信息确定消息顺序：

* `conversation-turn-*`
* DOM 顺序
* 消息节点标识
* 当前对话 URL

脚本不会调用 ChatGPT 私有后端接口，也不会读取其他未显示的账号对话。

所有提取和导出操作都在本地浏览器中完成。

## 已知限制

### 仅导出当前可见分支

如果某条响应曾多次重新生成，ChatGPT 页面通常只挂载当前选中的响应分支。

脚本只能导出当前页面实际显示的版本，不能保证获得所有历史分支。

### 图片不会打包下载

脚本只保存图片引用，例如：

```json
{
  "alt": "image",
  "src": "https://..."
}
```

部分图片地址可能依赖登录状态，或者是临时 URL，之后可能失效。

### 流式响应可能不完整

如果在 ChatGPT 仍然生成回答时导出，文件只包含当前已经生成的内容。

JSON 中会标记：

```json
{
  "streaming_at_export": true
}
```

### 页面结构变化

该工具依赖 ChatGPT 当前的页面 DOM。

如果 ChatGPT 修改消息节点属性或页面结构，脚本中的选择器可能需要更新。

## 隐私说明

* 不上传对话内容
* 不连接第三方服务器
* 不使用远程分析服务
* 不读取其他浏览器标签页
* 导出文件由浏览器直接在本地生成

请注意妥善保管导出的 JSON 和 Markdown 文件，其中可能包含个人信息、代码、账号信息或其他敏感内容。

## 适用场景

* 备份单个 ChatGPT 对话
* 将对话保存到 Obsidian
* 将对话提交到 Git
* 构建个人知识库
* 提取问答数据集
* 对对话进行结构化分析
* 将 ChatGPT 内容迁移到其他系统

## License

可根据项目需要选择开源许可证，例如：

```text
MIT License
```

## Disclaimer

本项目不是 OpenAI 官方产品，与 OpenAI 无隶属或合作关系。

ChatGPT 页面结构可能随时变化，因此无法保证脚本始终兼容所有未来版本。
