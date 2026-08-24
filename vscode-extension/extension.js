// HopGPT Chat — VS Code 聊天框里的全自动改代码 participant
//
// 工作方式：把当前文件/引用文件的内容 + 用户请求发给本地 HopGPT 代理，
// 让 HopGPT 用 aider 的 SEARCH/REPLACE 文本协议输出改动，本扩展解析并
// 用 workspace.fs 直接把改动写进磁盘。全程不依赖上游 tool calling。

const vscode = require('vscode');

// ---------------------------------------------------------------------------
// 系统提示词：复用 aider editblock 的 SEARCH/REPLACE 协议（已实测 HopGPT 能稳定输出）
// ---------------------------------------------------------------------------

const SYSTEM_PROMPT = `Act as an expert software developer.
Always use best practices when coding.
Respect and use existing conventions, libraries, etc that are already present in the code base.
Take requests for changes to the supplied code.
If the request is ambiguous, ask questions.

Once you understand the request you MUST:
1. Think step-by-step and explain the needed changes in a few short sentences.
2. Describe each change with a *SEARCH/REPLACE block* per the format below.

All changes to files must use this *SEARCH/REPLACE block* format.
ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!

# *SEARCH/REPLACE block* Rules:
Every *SEARCH/REPLACE block* must use this format:
1. The *FULL* file path alone on a line, verbatim. No bold asterisks, no quotes around it, no escaping.
2. The opening fence and code language, eg: \`\`\`python
3. The start of search block: <<<<<<< SEARCH
4. A contiguous chunk of lines to search for in the existing source code
5. The dividing line: =======
6. The lines to replace into the source code
7. The end of the replace block: >>>>>>> REPLACE
8. The closing fence: \`\`\`

Every *SEARCH* section must *EXACTLY MATCH* the existing file content, character for character, including all comments and docstrings.
*SEARCH/REPLACE* blocks will *only* replace the first match occurrence.
Include enough lines in each SEARCH section to uniquely match each set of lines that need to change.
Keep *SEARCH/REPLACE* blocks concise.
Include just the changing lines, and a few surrounding lines if needed for uniqueness.
Break large *SEARCH/REPLACE* blocks into a series of smaller blocks.

To create a NEW file, use a *SEARCH/REPLACE block* with:
- The new file path, including dir name if needed
- An empty SEARCH section
- The new file's contents in the REPLACE section

ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!`;

// ---------------------------------------------------------------------------
// 解析 SEARCH/REPLACE 块（以及 ### path + 代码块 的整文件兜底）
// ---------------------------------------------------------------------------

const SEARCH_REPLACE_RE =
  /^([^\n`]+?)[ \t]*\n```[^\n]*\n<<<<<<< SEARCH\n([\s\S]*?)\n?=======\n([\s\S]*?)\n?>>>>>>> REPLACE\n```/gm;

const WHOLE_FILE_RE = /^###[ \t]+([^\n]+?)[ \t]*\n```[^\n]*\n([\s\S]*?)\n```/gm;

function cleanPath(raw) {
  return (raw || '').trim().replace(/^###\s+/, '');
}

function parseSearchReplaceBlocks(text) {
  const blocks = [];
  SEARCH_REPLACE_RE.lastIndex = 0;
  let m;
  while ((m = SEARCH_REPLACE_RE.exec(text)) !== null) {
    blocks.push({
      path: cleanPath(m[1]),
      search: m[2],
      replace: m[3],
      kind: 'search_replace',
    });
  }
  return blocks;
}

function parseWholeFileBlocks(text) {
  const blocks = [];
  WHOLE_FILE_RE.lastIndex = 0;
  let m;
  while ((m = WHOLE_FILE_RE.exec(text)) !== null) {
    blocks.push({
      path: cleanPath(m[1]),
      search: '',
      replace: m[2],
      kind: 'whole_file',
    });
  }
  return blocks;
}

function parseEdits(text) {
  const blocks = parseSearchReplaceBlocks(text);
  if (blocks.length > 0) return blocks;
  return parseWholeFileBlocks(text);
}

// 去掉 SEARCH/REPLACE 块，只留模型的文字说明
function stripEditBlocks(text) {
  let t = text.replace(SEARCH_REPLACE_RE, '');
  t = t.replace(WHOLE_FILE_RE, '');
  return t.replace(/\n{3,}/g, '\n\n').trim();
}

// ---------------------------------------------------------------------------
// 文件读写
// ---------------------------------------------------------------------------

const decoder = new TextDecoder();
const encoder = new TextEncoder();

function workspaceRoot() {
  return vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0]
    ? vscode.workspace.workspaceFolders[0].uri
    : null;
}

function resolveUri(path) {
  const root = workspaceRoot();
  const p = path.trim();
  if (p.startsWith('/') || /^[A-Za-z]:[\\/]/.test(p)) {
    return vscode.Uri.file(p);
  }
  if (root) {
    return vscode.Uri.joinPath(root, p);
  }
  return vscode.Uri.file(p);
}

async function readFileText(uri) {
  try {
    const bytes = await vscode.workspace.fs.readFile(uri);
    return decoder.decode(bytes);
  } catch (e) {
    return null;
  }
}

async function writeFileText(uri, content) {
  await ensureParentDir(uri);
  await vscode.workspace.fs.writeFile(uri, encoder.encode(content));
}

async function ensureParentDir(uri) {
  const dir = vscode.Uri.joinPath(uri, '..');
  if (dir.toString() === uri.toString()) return; // 已到根
  try {
    await vscode.workspace.fs.stat(dir);
  } catch (e) {
    await ensureParentDir(dir);
    await vscode.workspace.fs.createDirectory(dir);
  }
}

function applySearchReplace(content, search, replace) {
  if (!search.trim()) {
    return { content: replace, changed: true, isNew: true };
  }
  if (content.includes(search)) {
    return { content: content.replace(search, replace), changed: true };
  }
  // 兜底：模型可能在 SEARCH 里带了行尾空白差异
  const trimmed = search.replace(/[ \t]+$/g, '');
  if (trimmed !== search && content.includes(trimmed)) {
    return { content: content.replace(trimmed, replace), changed: true };
  }
  return { content, changed: false, error: '未在文件里找到要替换的内容（SEARCH 不匹配）' };
}

// ---------------------------------------------------------------------------
// 上下文收集
// ---------------------------------------------------------------------------

async function collectContextFiles(request) {
  const files = []; // { path, content }
  const seen = new Set();
  const selections = [];
  const MAX_FILE = 300000; // 单个文件最多读 300KB

  async function addFile(uri, displayPath) {
    if (!uri || uri.scheme !== 'file') return;
    const key = uri.toString();
    if (seen.has(key)) return;
    seen.add(key);
    const content = await readFileText(uri);
    if (content === null) return; // 目录或读不了
    if (content.length > MAX_FILE) {
      files.push({
        path: displayPath,
        content: content.slice(0, MAX_FILE) + '\n... (文件过大，已截断) ...',
      });
    } else {
      files.push({ path: displayPath, content });
    }
  }

  // 1) 当前活动编辑器里的文件
  const editor = vscode.window.activeTextEditor;
  if (editor && editor.document.uri.scheme === 'file') {
    const ws = vscode.workspace.getWorkspaceFolder(editor.document.uri);
    const rel = ws ? vscode.workspace.asRelativePath(editor.document.uri, false) : editor.document.uri.fsPath;
    await addFile(editor.document.uri, rel);
    // 2) 编辑器里的选中文本
    if (!editor.selection.isEmpty) {
      const sel = editor.document.getText(editor.selection);
      if (sel.trim()) selections.push(sel);
    }
  }

  // 3) 聊天里显式引用的文件 / 选中片段
  for (const ref of request.references || []) {
    if (ref && typeof ref.value === 'string' && ref.value.trim()) {
      selections.push(ref.value);
      continue;
    }
    if (ref && ref.uri && ref.uri.scheme === 'file') {
      const ws = vscode.workspace.getWorkspaceFolder(ref.uri);
      const rel = ws ? vscode.workspace.asRelativePath(ref.uri, false) : ref.uri.fsPath;
      await addFile(ref.uri, rel);
    }
  }

  return { files, selections };
}

function buildHistoryText(context) {
  const parts = [];
  try {
    const history = context && context.history ? context.history : [];
    for (const turn of history) {
      try {
        if (typeof vscode.ChatRequestTurn !== 'undefined' && turn instanceof vscode.ChatRequestTurn) {
          parts.push('User: ' + turn.prompt);
        } else if (typeof vscode.ChatResponseTurn !== 'undefined' && turn instanceof vscode.ChatResponseTurn) {
          let text = '';
          for (const p of turn.response || []) {
            if (p && typeof p.value === 'string') text += p.value;
          }
          if (text.trim()) parts.push('Assistant: ' + text.trim());
        }
      } catch (e) { /* ignore */ }
    }
  } catch (e) { /* ignore */ }
  return parts.join('\n\n');
}

function composeUserMessage(files, selections, historyText, prompt) {
  const out = [];
  if (files.length > 0) {
    out.push('Here are the files currently in context (full path + exact contents):\n');
    for (const f of files) {
      const lang = detectLang(f.path);
      out.push('### ' + f.path + '\n```' + lang + '\n' + f.content + '\n```');
    }
  }
  if (selections.length > 0) {
    out.push('The user also selected this text (do not assume it is a full file):\n');
    for (const s of selections) out.push('```\n' + s + '\n```');
  }
  if (historyText) {
    out.push('Prior conversation:\n' + historyText);
  }
  out.push('User request:\n' + prompt);
  return out.join('\n\n');
}

function detectLang(path) {
  const ext = (path.split('.').pop() || '').toLowerCase();
  const map = {
    js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
    ts: 'typescript', tsx: 'typescript', py: 'python', rb: 'ruby', go: 'go',
    rs: 'rust', java: 'java', c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', cc: 'cpp',
    cs: 'csharp', php: 'php', swift: 'swift', kt: 'kotlin', scala: 'scala',
    sh: 'bash', bash: 'bash', zsh: 'bash', json: 'json', yml: 'yaml', yaml: 'yaml',
    toml: 'toml', md: 'markdown', html: 'html', css: 'css', scss: 'scss',
    sql: 'sql', r: 'r', lua: 'lua', vim: 'vim', dockerfile: 'dockerfile',
  };
  return map[ext] || '';
}

// ---------------------------------------------------------------------------
// 调用本地 HopGPT 代理
// ---------------------------------------------------------------------------

async function callHopGPT(proxyUrl, model, userText, token) {
  const controller = new AbortController();
  const sub = token && token.onCancellationRequested
    ? token.onCancellationRequested(() => controller.abort())
    : null;
  try {
    const res = await fetch(proxyUrl.replace(/\/+$/, '') + '/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model,
        stream: false,
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          { role: 'user', content: userText },
        ],
      }),
      signal: controller.signal,
    });
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      throw new Error('HTTP ' + res.status + ': ' + body.slice(0, 500));
    }
    const data = await res.json();
    const content = data && data.choices && data.choices[0]
      && data.choices[0].message && data.choices[0].message.content;
    if (typeof content !== 'string') {
      throw new Error('代理返回里没有文本内容：' + JSON.stringify(data).slice(0, 300));
    }
    return content;
  } finally {
    if (sub && sub.dispose) sub.dispose();
  }
}

// ---------------------------------------------------------------------------
// participant 主逻辑
// ---------------------------------------------------------------------------

async function handle(request, context, stream, token) {
  const config = vscode.workspace.getConfiguration('hopgpt');
  const proxyUrl = config.get('proxyUrl', 'http://127.0.0.1:8787');
  const model = config.get('model', 'claude-sonnet-4.5');
  const prompt = request.prompt.trim();

  if (!prompt) {
    stream.markdown('请描述你要我做什么。例如：给 calc.py 加一个 subtract 函数。');
    return;
  }

  stream.progress('正在读取上下文…');
  const { files, selections } = await collectContextFiles(request);
  const historyText = buildHistoryText(context);
  const userText = composeUserMessage(files, selections, historyText, prompt);

  stream.progress('正在调用 HopGPT（' + model + '）…');
  let raw;
  try {
    raw = await callHopGPT(proxyUrl, model, userText, token);
  } catch (e) {
    const msg = String(e && e.message || e);
    if (/ECONNREFUSED|fetch failed|ENOTFOUND|AbortError|aborted/i.test(msg)) {
      stream.markdown(
        '❌ 连不上 HopGPT 代理（' + proxyUrl + '）。\n\n' +
        '请先在终端启动代理：\n\n' +
        '```bash\ncd ~/research/token_steal && ./start.sh\n```\n\n' +
        '并保持浏览器里的 chat.ai.jh.edu 标签页开着。'
      );
    } else if (/credentials_expired|凭证已过期/i.test(msg)) {
      stream.markdown(
        '❌ HopGPT 凭证已过期。\n\n' +
        '请**刷新浏览器里的 chat.ai.jh.edu 页面**，等 10 秒让扩展抓到新凭证，再重试。'
      );
    } else if (/cloudflare_block|Cloudflare/i.test(msg)) {
      stream.markdown(
        '❌ 被 Cloudflare 拦截。\n\n' +
        '请刷新 chat.ai.jh.edu 页面完成人机验证，等扩展重新推送 cookie 后再试。'
      );
    } else {
      stream.markdown('❌ HopGPT 调用失败：\n\n```\n' + msg + '\n```');
    }
    return;
  }

  const edits = parseEdits(raw);
  if (edits.length === 0) {
    // 纯问答：直接回显模型文本
    stream.markdown(raw);
    return;
  }

  stream.progress('正在应用 ' + edits.length + ' 处改动…');
  const results = []; // { path, status, note }
  const changedUris = [];

  for (const edit of edits) {
    const uri = resolveUri(edit.path);
    const existing = await readFileText(uri);
    if (existing === null && edit.search.trim()) {
      results.push({ path: edit.path, status: 'warn', note: '文件不存在，无法应用 SEARCH/REPLACE' });
      continue;
    }
    const before = existing === null ? '' : existing;
    const r = applySearchReplace(before, edit.search, edit.replace);
    if (!r.changed) {
      results.push({ path: edit.path, status: 'error', note: r.error || '未应用' });
      continue;
    }
    try {
      await writeFileText(uri, r.content);
      const verb = r.isNew ? '新建' : '修改';
      results.push({ path: edit.path, status: 'ok', note: verb });
      changedUris.push(uri);
    } catch (e) {
      results.push({ path: edit.path, status: 'error', note: '写入失败：' + (e && e.message || e) });
    }
  }

  // 回显结果
  const explanation = stripEditBlocks(raw);
  if (explanation) {
    stream.markdown(explanation + '\n\n---\n\n');
  }

  const okCount = results.filter((r) => r.status === 'ok').length;
  stream.markdown('**已应用 ' + okCount + '/' + edits.length + ' 处改动**\n\n');
  for (const r of results) {
    const icon = r.status === 'ok' ? '✅' : r.status === 'error' ? '❌' : '⚠️';
    stream.markdown(icon + ' `' + r.path + '` ' + r.note + '\n\n');
  }
  for (const uri of changedUris) {
    stream.reference(uri);
  }

  const failed = results.filter((r) => r.status !== 'ok');
  if (failed.length > 0) {
    stream.markdown(
      '有改动没应用成功。若是因为 SEARCH 不匹配，可以把文件重新加入上下文后重试，' +
      '或让我只改更小的片段。'
    );
  }
}

function activate(context) {
  const participant = vscode.chat.createChatParticipant('hopgpt-chat.hopgpt', handle);
  participant.iconPath = vscode.Uri.joinPath(context.extensionUri, 'icon.svg');
  context.subscriptions.push(participant);
}

function deactivate() {}

module.exports = { activate, deactivate };
