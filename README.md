# HopGPT → VS Code 一键代理

把 JHU HopGPT 接到 VS Code 聊天框，全自动读写本地文件。**同机使用，不需要 cloudflared。**

## 工作原理

```
VS Code 聊天框（@hopgpt）/ aider 终端
   │  OpenAI 格式请求
   ▼
本地代理 proxy.py  （127.0.0.1:8787）
   │  把请求放入任务队列，等浏览器扩展来领取
   ▼
浏览器扩展（extension/）
   │  用浏览器真实网络栈执行 LibreChat 请求（天然通过 Cloudflare）
   ▼
chat.ai.jh.edu  （HopGPT 上游）
```

- **请求走浏览器**：Cloudflare 会识别并拦截代理的伪造 TLS 指纹，但浏览器是真实 Chrome，永远不会被拦。所以上游请求由浏览器扩展执行，代理只负责翻译格式。
- **凭证**：扩展通过 `chrome.cookies` 读取 httpOnly cookie，通过主世界注入拦截 `Authorization` 头拿到 token。
- **认证方式**：HopGPT 当前使用 `connect.sid`（Express 会话）+ `refreshToken`，**不需要 `cf_clearance`**。

---

## 一次性配置

```bash
cd ~/research/token_steal
chmod +x start.sh
```

### 安装浏览器扩展（凭证来源，必须）

1. 浏览器打开 `chrome://extensions`
2. 右上角打开 **开发者模式**
3. 点 **「加载已解压的扩展程序」**
4. 选择文件夹 `~/research/token_steal/extension`
5. 确认列表里出现 **HopGPT Credential Keeper**

> 扩展装好后**不要再动它**。以后每次只用 `./start.sh`。

---

## 每次使用（3 步）

### 1. 启动代理

```bash
cd ~/research/token_steal
./start.sh
```

看到 `凭证状态: ✓ 就绪` 即可。

### 2. 保持浏览器标签页开着

打开 [https://chat.ai.jh.edu](https://chat.ai.jh.edu) 并**保持标签页开着**（扩展会每分钟自动推送新 token + cookie）。

如果隔很久没用导致 token 过期，**刷新一下页面**即可，扩展会自动抓到新 token。

### 3. 在 VS Code 里聊天

打开 **Chat 面板**（`Ctrl+Alt+I`），输入 `@hopgpt` 后说需求。全自动改文件见下一节。

> **Codex 侧边栏**和 HopGPT 完全独立，照旧用，不用改。

---

## 全自动改代码（VS Code 聊天框 @hopgpt，推荐）

在 VS Code 聊天框里 `@hopgpt`，让 HopGPT **直接读写本地文件**，不用点任何"应用"按钮。

原理：一个本地扩展注册了聊天 participant，复用 aider 的 SEARCH/REPLACE 文本协议
（HopGPT 纯文本即可，不依赖 tool calling），扩展解析后用 `workspace.fs` 把改动写进磁盘。

### 安装（已装好可跳过）

扩展已通过软链接装进 VS Code：

```bash
ln -sfn ~/research/token_steal/vscode-extension ~/.vscode/extensions/hopgpt-chat-0.1.0
```

然后**完全重启 VS Code**（关掉所有窗口再开）。

### 用法

1. `./start.sh` 启动代理，保持 chat.ai.jh.edu 标签页开着。
2. 打开你要改的项目/文件。
3. 聊天框（`Ctrl+Alt+I`）输入 `@hopgpt` 然后说需求，例如：

```
@hopgpt 给 calc.py 加一个 subtract 函数，返回 a - b
```

扩展会读取当前文件内容 → 调 HopGPT → 解析 SEARCH/REPLACE → **直接写盘**，并在回复里列出改了哪些文件（可点击跳转）。

> **Ask 模式自动检测**：在 **Ask** 模式下不写 `@hopgpt` 也可能被自动路由到 HopGPT
> （由 `disambiguation` 驱动）。但 Agent / Edit 模式下 VS Code 不做自动检测，需要显式 `@hopgpt`。

### 配置

在 VS Code 设置里搜 `hopgpt`：

| 设置 | 默认 | 说明 |
|---|---|---|
| `hopgpt.proxyUrl` | `http://127.0.0.1:8787` | 代理地址 |
| `hopgpt.model` | `claude-sonnet-4.5` | 模型，可换 `claude-opus` / `gpt-5.5` / `o3` 等 |

> 说明：`isDefault`（设为默认 agent、无需 @）是 VS Code 较新版本才支持的字段，
> 当前 1.134 尚不支持，所以这里用 `@hopgpt` 显式调用。升级 VS Code 后我会帮你补上。

---

## 全自动改代码（aider 终端版）

如果你更习惯终端，也可以用 [aider](https://aider.chat)（和上面同一个 SEARCH/REPLACE 协议）。

### 安装（已装好可跳过）

aider 装在独立的 conda 环境（用 Python 3.12，避开系统 3.13 的依赖冲突）：

```bash
~/miniforge3/bin/conda create -y -n aider python=3.12
~/miniforge3/envs/aider/bin/python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple uv
UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  ~/miniforge3/envs/aider/bin/uv pip install --python ~/miniforge3/envs/aider/bin/python aider-chat
```

> 校园网到 PyPI 很慢，务必用清华镜像 `pypi.tuna.tsinghua.edu.cn`。

### 用法

先确保代理在跑（`./start.sh`）、chat.ai.jh.edu 标签页开着，然后在**任意 git 仓库**里：

```bash
# 建一个软链接，之后随处可用
ln -s ~/research/token_steal/aider-hopgpt.sh ~/.local/bin/aider-hopgpt

cd ~/你的项目
aider-hopgpt                       # 进入交互模式，直接说要改什么
aider-hopgpt calc.py               # 把某些文件加入上下文
aider-hopgpt --message "加个 subtract 函数" calc.py   # 一次性非交互
```

aider 会读代码、直接改文件、并自动 git 提交（想手动提交加 `--no-auto-commits`）。

### 常用环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOPGPT_MODEL` | `claude-sonnet-4.5` | 主模型，也可 `gpt-5.5` / `claude-opus` / `o3` |
| `HOPGPT_WEAK_MODEL` | `hopgpt` | 生成提交信息等的弱模型（o3-mini） |
| `AIDER_EDIT_FORMAT` | `diff` | 改动应用失败时可改成 `whole`（整文件重写，更稳但更费 token） |

```bash
HOPGPT_MODEL=claude-opus aider-hopgpt   # 换更强的模型
```

---

## 可用模型（11 个）

**Azure OpenAI**

| 名字 | 说明 |
|---|---|
| `hopgpt` | o3-mini（默认） |
| `o3` / `o3-mini` | OpenAI o 系列 |
| `gpt-5.4` / `gpt-5.4-mini` / `gpt-5.4-nano` / `gpt-5.5` | GPT-5 系列 |

**Claude**

| 名字 | 说明 |
|---|---|
| `claude-sonnet` | Sonnet 5 |
| `claude-sonnet-4.5` | Sonnet 4.5 |
| `claude-opus` | Opus 4.5 |
| `claude-haiku` | Haiku 4.5 |

---

## 故障排查

```bash
curl -s http://127.0.0.1:8787/health | python3 -m json.tool
```

| 现象 | 处理 |
|---|---|
| `"ready": true` | 正常 |
| `"ready": false` + 无 token | 刷新 chat.ai.jh.edu，确认扩展已启用 |
| 请求报「凭证已过期」 | 刷新 chat.ai.jh.edu，等 10 秒再试 |
| 请求报「被 Cloudflare 拦截」 | 刷新页面完成人机验证（罕见，新认证方式一般不需要） |

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `proxy.py` | 核心代理（翻译 OpenAI/Anthropic → LibreChat 内部 API） |
| `extension/` | 浏览器扩展（捕获 token + cookie 并推送） |
| `start.sh` | 每次启动代理 |
| `aider-hopgpt.sh` | 用 HopGPT 驱动 aider（终端版全自动改代码） |
| `vscode-extension/` | VS Code 聊天框 `@hopgpt` 扩展（聊天框里全自动改代码） |
| `.token` / `.cookies` / `.ua` | 凭证文件（自动生成，已 gitignore） |
