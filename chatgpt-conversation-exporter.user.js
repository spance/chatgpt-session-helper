// ==UserScript==
// @name         ChatGPT Conversation Exporter
// @namespace    https://chatgpt.com/
// @version      3.1.0
// @description  Export ChatGPT live conversations and shared conversations as JSON or Markdown.
// @author       OpenAI ChatGPT
// @match        https://chatgpt.com/*
// @match        https://chat.openai.com/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(() => {
  'use strict';

  const APP_ID = 'chatgpt-conversation-exporter-v3';
  const PANEL_ID = `${APP_ID}-panel`;
  const STYLE_ID = `${APP_ID}-style`;
  const MESSAGE_SELECTOR_GROUPS = [
    {
      user: '[data-content-search-unit-key$=":user"]',
      assistant: '[data-content-search-unit-key$=":assistant"]'
    },
    {
      user: '[data-chatgpt-search-unit-key$=":user"]',
      assistant: '[data-chatgpt-search-unit-key$=":assistant"]'
    },
    {
      user: '[data-message-author-role="user"]',
      assistant: '[data-message-author-role="assistant"]'
    }
  ];

  // Avoid duplicate initialization if the userscript is manually re-run.
  if (window.__chatgptConversationExporterV3?.destroy) {
    window.__chatgptConversationExporterV3.destroy();
  }

  const state = {
    routeKey: '',
    messages: new Map(),
    fallbackOrder: 0,
    scanRunning: false,
    collectTimer: 0,
    panel: null,
    status: null,
    observer: null,
    routeTimer: 0,
    currentScroller: null
  };

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  const clean = text => String(text || '')
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  function isSupportedPath(pathname = location.pathname) {
    // Normal chats: /c/<id>
    // Shared chats: /share/<id>
    // Custom GPT chats can contain /c/<id> deeper in the path.
    return pathname.startsWith('/share/') || /(?:^|\/)c\/[^/]+/.test(pathname);
  }

  function getRouteKey() {
    return `${location.origin}${location.pathname}${location.search}`;
  }

  function isVisible(node) {
    if (!(node instanceof Element) || !node.isConnected) return false;
    const style = getComputedStyle(node);
    return style.display !== 'none' && style.visibility !== 'hidden' && node.getClientRects().length > 0;
  }

  function roleForNode(node) {
    const explicitRole = node.getAttribute('data-message-author-role');
    if (explicitRole === 'user' || explicitRole === 'assistant') return explicitRole;

    const unitKey =
      node.getAttribute('data-content-search-unit-key') ||
      node.getAttribute('data-chatgpt-search-unit-key') ||
      '';
    const match = unitKey.match(/:(user|assistant)$/);
    return match?.[1] || null;
  }

  function getRoleNodes() {
    const nodes = [];

    for (const role of ['user', 'assistant']) {
      for (const group of MESSAGE_SELECTOR_GROUPS) {
        const selector = group[role];
        const candidates = [...document.querySelectorAll(selector)]
          .filter(isVisible)
          // Keep only the outermost match from the selected DOM generation.
          .filter(node => !node.parentElement?.closest(selector));

        if (candidates.length > 0) {
          nodes.push(...candidates);
          break;
        }
      }
    }

    return nodes.sort((a, b) => {
      if (a === b) return 0;
      const position = a.compareDocumentPosition(b);
      if (position & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (position & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return 0;
    });
  }

  function contentRoot(roleNode, role) {
    const selectors = role === 'assistant'
      ? [
          '[data-markdown-text-style="assistant-message"]',
          '[data-message-content]',
          '.markdown',
          '.prose',
          '[class*="MarkdownRoot"]',
          '[class*="markdown"]'
        ]
      : [
          '[data-user-message-bubble="true"]',
          '[data-markdown-text-tone="user-message"]',
          '[data-message-content]',
          '.whitespace-pre-wrap',
          '[class*="whitespace-pre-wrap"]'
        ];

    for (const selector of selectors) {
      const node = roleNode.matches(selector)
        ? roleNode
        : roleNode.querySelector(selector);
      if (node) return node;
    }
    return roleNode;
  }

  function extractText(root) {
    const clone = root.cloneNode(true);

    clone.querySelectorAll([
      'button',
      'textarea',
      '[role="button"]',
      '[contenteditable="true"]',
      '[data-testid*="copy"]',
      '[data-testid*="feedback"]',
      '[data-testid*="branch"]',
      '[data-markdown-copy="exclude"]',
      '[aria-label*="Copy"]',
      '[aria-label*="复制"]',
      '[aria-label*="Good response"]',
      '[aria-label*="Bad response"]'
    ].join(',')).forEach(node => node.remove());

    // Remove standalone SVG UI icons, while preserving textual SVG descendants
    // in the unlikely event that an SVG contains meaningful text.
    clone.querySelectorAll('svg').forEach(svg => {
      if (!clean(svg.textContent)) svg.remove();
    });

    return clean(clone.innerText || clone.textContent);
  }

  function stableIdentity(roleNode, role, domOrdinal) {
    const legacyTurn = roleNode.closest('[data-testid^="conversation-turn-"]');
    const legacyTurnId = legacyTurn?.getAttribute('data-testid') || '';
    const legacyTurnMatch = legacyTurnId.match(/conversation-turn-(\d+)/);

    const unitKey =
      roleNode.getAttribute('data-content-search-unit-key') ||
      roleNode.getAttribute('data-chatgpt-search-unit-key') ||
      '';
    const unitMatch = unitKey.match(/(?:^|-)turn-(\d+):(\d+):(user|assistant)$/);
    const searchTurnKey = roleNode
      .closest('[data-content-search-turn-key]')
      ?.getAttribute('data-content-search-turn-key') || '';
    const searchTurnMatch = searchTurnKey.match(/(?:^|-)turn-(\d+)$/);

    const messageCarrier =
      roleNode.closest('[data-chatgpt-search-message-ids]') ||
      roleNode.querySelector('[data-chatgpt-search-message-ids]');
    const selectionMessage = roleNode.querySelector('[data-chatgpt-selection-message-id]');
    const legacyMessage =
      roleNode.closest('[data-message-id], [data-message-uuid]') ||
      roleNode.querySelector('[data-message-id], [data-message-uuid]');
    const messageIds = [
      roleNode.getAttribute('data-message-id'),
      roleNode.getAttribute('data-message-uuid'),
      selectionMessage?.getAttribute('data-chatgpt-selection-message-id'),
      legacyMessage?.getAttribute('data-message-id'),
      legacyMessage?.getAttribute('data-message-uuid'),
      ...(messageCarrier?.getAttribute('data-chatgpt-search-message-ids') || '')
        .split(/\s+/)
    ].filter(Boolean);
    const uniqueMessageIds = [...new Set(messageIds)];
    const messageId = uniqueMessageIds[uniqueMessageIds.length - 1] || '';

    const turnIndex = unitMatch
      ? Number(unitMatch[1])
      : searchTurnMatch
        ? Number(searchTurnMatch[1])
        : legacyTurnMatch
          ? Number(legacyTurnMatch[1])
          : null;
    const unitIndex = unitMatch ? Number(unitMatch[2]) : null;
    const order = turnIndex === null
      ? domOrdinal
      : unitIndex === null
        ? turnIndex
        : turnIndex * 1000 + unitIndex;

    if (unitKey) {
      return {
        key: `unit:${unitKey}`,
        order,
        turnIndex,
        messageId: messageId || null
      };
    }

    if (legacyTurnId) {
      return {
        key: `turn:${legacyTurnId}:${role}`,
        order,
        turnIndex,
        messageId: messageId || null
      };
    }

    if (messageId) {
      return {
        key: `message:${messageId}:${role}`,
        order,
        turnIndex,
        messageId
      };
    }

    return {
      key: `ordinal:${role}:${domOrdinal}`,
      order,
      turnIndex,
      messageId: null
    };
  }

  function collect() {
    if (!isSupportedPath()) return 0;

    const roleNodes = getRoleNodes();

    roleNodes.forEach((roleNode, domOrdinal) => {
      const role = roleForNode(roleNode);
      if (role !== 'user' && role !== 'assistant') return;

      const root = contentRoot(roleNode, role);
      const content = extractText(root);
      const images = [...root.querySelectorAll('img')]
        .map(img => ({
          alt: img.alt || '',
          src: img.currentSrc || img.src || ''
        }))
        .filter(image => image.src);

      const links = [...root.querySelectorAll('a[href]')]
        .map(anchor => ({
          text: clean(anchor.innerText || anchor.textContent),
          href: anchor.href
        }))
        .filter(link => link.href);

      if (!content && images.length === 0) return;

      const identity = stableIdentity(roleNode, role, domOrdinal);
      const signature = `${role}\u0000${content}\u0000${images.map(x => x.src).join('|')}`;

      const previous = state.messages.get(identity.key);
      state.messages.set(identity.key, {
        _key: identity.key,
        _order: previous?._order ?? identity.order ?? state.fallbackOrder++,
        _signature: signature,
        index: identity.turnIndex,
        message_id: identity.messageId,
        role,
        content,
        images,
        links
      });
    });

    refreshStatus();
    return roleNodes.length;
  }

  function scheduleCollect(delay = 120) {
    clearTimeout(state.collectTimer);
    state.collectTimer = window.setTimeout(() => collect(), delay);
  }

  function overflowY(element) {
    return getComputedStyle(element).overflowY;
  }

  function findScrollableAncestor(node) {
    let current = node?.parentElement;
    while (current && current !== document.body && current !== document.documentElement) {
      const overflow = overflowY(current);
      if (
        (overflow === 'auto' || overflow === 'scroll') &&
        current.scrollHeight > current.clientHeight + 80
      ) {
        return current;
      }
      current = current.parentElement;
    }
    return null;
  }

  function findScroller() {
    const firstMessage = getRoleNodes()[0];
    const ancestor = findScrollableAncestor(firstMessage);
    if (ancestor) return ancestor;

    const candidates = [...document.querySelectorAll('main *')]
      .filter(element => {
        if (!(element instanceof HTMLElement)) return false;
        const overflow = overflowY(element);
        return (
          (overflow === 'auto' || overflow === 'scroll') &&
          element.scrollHeight > element.clientHeight + 200
        );
      })
      .sort((a, b) => b.scrollHeight - a.scrollHeight);

    return candidates[0] || document.scrollingElement || document.documentElement;
  }

  function getScrollState(scroller) {
    const isDocument =
      scroller === document.scrollingElement ||
      scroller === document.documentElement ||
      scroller === document.body;

    if (isDocument) {
      const root = document.scrollingElement || document.documentElement;
      return {
        isDocument: true,
        top: root.scrollTop,
        clientHeight: window.innerHeight,
        scrollHeight: root.scrollHeight,
        setTop(value) {
          window.scrollTo(0, value);
        }
      };
    }

    return {
      isDocument: false,
      top: scroller.scrollTop,
      clientHeight: scroller.clientHeight,
      scrollHeight: scroller.scrollHeight,
      setTop(value) {
        scroller.scrollTop = value;
      }
    };
  }

  function isStreaming() {
    const selectors = [
      '[data-testid="stop-button"]',
      'button[aria-label*="Stop"]',
      'button[aria-label*="停止"]',
      'button[aria-label*="stop"]'
    ];
    return selectors.some(selector =>
      [...document.querySelectorAll(selector)].some(isVisible)
    );
  }

  async function scanWholeConversation() {
    if (state.scanRunning || !isSupportedPath()) return;

    state.scanRunning = true;
    setStatus('正在扫描整个对话……');

    try {
      const scroller = findScroller();
      state.currentScroller = scroller;

      let scroll = getScrollState(scroller);
      const originalTop = scroll.top;

      // Start at the top. This also gives lazy-rendered/virtualized pages a chance
      // to mount older messages before moving downward.
      scroll.setTop(0);
      await sleep(350);
      collect();

      let stableAtBottom = 0;
      let previousState = '';

      for (let step = 0; step < 400 && stableAtBottom < 5; step++) {
        scroll = getScrollState(scroller);
        collect();

        const maxTop = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
        const nextTop = Math.min(
          maxTop,
          scroll.top + Math.max(500, scroll.clientHeight * 0.75)
        );

        scroll.setTop(nextTop);
        await sleep(140);

        const after = getScrollState(scroller);
        const atBottom = after.top >= Math.max(0, after.scrollHeight - after.clientHeight - 6);
        const snapshot = `${Math.round(after.top)}:${after.scrollHeight}:${state.messages.size}`;

        if (atBottom && snapshot === previousState) {
          stableAtBottom += 1;
        } else if (atBottom) {
          stableAtBottom = 1;
        } else {
          stableAtBottom = 0;
        }
        previousState = snapshot;
      }

      collect();
      getScrollState(scroller).setTop(originalTop);
      await sleep(80);
      collect();
    } finally {
      state.scanRunning = false;
      refreshStatus(true);
    }
  }

  function buildResult() {
    const raw = [...state.messages.values()]
      .sort((a, b) => a._order - b._order);

    // De-duplicate exact adjacent copies that may appear during React remounts.
    const turns = raw
      .filter((message, index) => {
        if (index === 0) return true;
        const previous = raw[index - 1];
        return !(
          message.role === previous.role &&
          message._signature === previous._signature
        );
      })
      .map(({ _key, _order, _signature, ...message }, sequence) => ({
        sequence,
        ...message
      }));

    const exchanges = [];
    let current = null;

    for (const turn of turns) {
      if (turn.role === 'user') {
        current = { request: turn, responses: [] };
        exchanges.push(current);
      } else {
        if (!current) {
          current = { request: null, responses: [] };
          exchanges.push(current);
        }
        current.responses.push(turn);
      }
    }

    const pageTitle = clean(document.title)
      .replace(/\s*[|–—-]\s*ChatGPT\s*$/i, '')
      .replace(/^ChatGPT\s*[|–—-]\s*/i, '');

    const headingTitle = clean(document.querySelector('main h1')?.innerText || '');
    const title = pageTitle || headingTitle || 'ChatGPT Conversation';

    return {
      schema_version: 3,
      page_type: location.pathname.startsWith('/share/') ? 'shared' : 'live',
      title,
      source_url: location.href,
      exported_at: new Date().toISOString(),
      streaming_at_export: isStreaming(),
      turn_count: turns.length,
      exchange_count: exchanges.length,
      turns,
      exchanges
    };
  }

  function toMarkdown(result) {
    const output = [
      `# ${result.title}`,
      '',
      `- 来源：${result.source_url}`,
      `- 页面类型：${result.page_type === 'shared' ? '分享对话' : '正在聊天的对话'}`,
      `- 导出时间：${result.exported_at}`,
      `- 消息数：${result.turn_count}`,
      `- 问答组数：${result.exchange_count}`,
      `- 导出时仍在生成：${result.streaming_at_export ? '是' : '否'}`,
      ''
    ];

    result.exchanges.forEach((exchange, exchangeIndex) => {
      output.push(
        `## 第 ${exchangeIndex + 1} 组`,
        '',
        '### 请求',
        '',
        exchange.request?.content || '*缺少用户请求*',
        ''
      );

      if (exchange.request?.images?.length) {
        output.push('#### 请求中的图片', '');
        exchange.request.images.forEach(image => {
          output.push(`![${image.alt || 'image'}](${image.src})`);
        });
        output.push('');
      }

      if (exchange.responses.length === 0) {
        output.push('### 响应', '', '*缺少助手响应*', '');
      } else {
        exchange.responses.forEach((response, responseIndex) => {
          const suffix = exchange.responses.length > 1
            ? ` ${responseIndex + 1}`
            : '';
          output.push(
            `### 响应${suffix}`,
            '',
            response.content || '*空响应*',
            ''
          );

          if (response.images.length) {
            output.push('#### 响应中的图片', '');
            response.images.forEach(image => {
              output.push(`![${image.alt || 'image'}](${image.src})`);
            });
            output.push('');
          }
        });
      }

      output.push('---', '');
    });

    return output.join('\n');
  }

  function safeFilename(name) {
    return clean(name)
      .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
      .replace(/\s+/g, ' ')
      .slice(0, 100) || 'chatgpt-conversation';
  }

  function download(name, content, mimeType) {
    const blob = new Blob([content], { type: `${mimeType};charset=utf-8` });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = name;
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.cssText = 'position:fixed;left:-9999px;top:0';
      document.body.appendChild(textarea);
      textarea.select();
      const ok = document.execCommand('copy');
      textarea.remove();
      return ok;
    }
  }

  function injectStyle() {
    document.getElementById(STYLE_ID)?.remove();
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #${PANEL_ID} {
        position: fixed;
        right: 16px;
        bottom: 16px;
        z-index: 2147483647;
        width: 272px;
        padding: 12px;
        border: 1px solid rgba(128,128,128,.38);
        border-radius: 12px;
        background: rgba(245,245,245,.97);
        color: #111;
        box-shadow: 0 8px 30px rgba(0,0,0,.22);
        font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        backdrop-filter: blur(12px);
      }
      #${PANEL_ID}[hidden] { display: none !important; }
      #${PANEL_ID} .cge-title {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        font-weight: 700;
        margin-bottom: 7px;
      }
      #${PANEL_ID} .cge-close {
        width: 25px;
        height: 25px;
        padding: 0;
        border: 0;
        border-radius: 7px;
        background: transparent;
        color: inherit;
        cursor: pointer;
        font-size: 18px;
      }
      #${PANEL_ID} .cge-status {
        min-height: 38px;
        word-break: break-word;
      }
      #${PANEL_ID} .cge-button {
        display: block;
        width: 100%;
        margin-top: 8px;
        padding: 8px;
        border: 1px solid rgba(128,128,128,.38);
        border-radius: 8px;
        background: #fff;
        color: #111;
        cursor: pointer;
        font: inherit;
      }
      #${PANEL_ID} .cge-button:hover { background: #eee; }
      #${PANEL_ID} .cge-button:disabled {
        opacity: .55;
        cursor: wait;
      }
      @media (prefers-color-scheme: dark) {
        #${PANEL_ID} {
          background: rgba(34,34,34,.97);
          color: #f5f5f5;
        }
        #${PANEL_ID} .cge-button {
          background: #333;
          color: #f5f5f5;
        }
        #${PANEL_ID} .cge-button:hover { background: #444; }
      }
    `;
    document.head.appendChild(style);
  }

  function setStatus(html) {
    if (state.status) state.status.innerHTML = html;
  }

  function refreshStatus(scanCompleted = false) {
    if (!state.status || !isSupportedPath()) return;
    if (state.scanRunning) return;

    const result = buildResult();
    const users = result.turns.filter(turn => turn.role === 'user').length;
    const assistants = result.turns.filter(turn => turn.role === 'assistant').length;
    const streamingText = isStreaming()
      ? '<br><span style="color:#b06000">当前仍在生成，导出会包含目前已生成的部分。</span>'
      : '';
    const scanText = scanCompleted ? '<b>完整扫描完成</b><br>' : '';

    if (result.turn_count > 0) {
      setStatus(`${scanText}用户 ${users} 条，助手 ${assistants} 条。${streamingText}`);
    } else {
      setStatus(
        '<b>尚未识别到消息</b><br>' +
        `消息节点：${getRoleNodes().length}`
      );
    }

    window.__chatgptConversationExport = result;
  }

  function makeButton(label, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'cge-button';
    button.textContent = label;
    button.addEventListener('click', async () => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        await handler();
      } catch (error) {
        console.error('[ChatGPT Conversation Exporter]', error);
        setStatus(`<b>操作失败</b><br>${clean(error?.message || error)}`);
      } finally {
        button.disabled = false;
      }
    });
    return button;
  }

  function createPanel() {
    document.getElementById(PANEL_ID)?.remove();
    injectStyle();

    const panel = document.createElement('div');
    panel.id = PANEL_ID;

    const titleRow = document.createElement('div');
    titleRow.className = 'cge-title';
    titleRow.innerHTML = '<span>ChatGPT 对话导出</span>';

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'cge-close';
    close.title = '隐藏；刷新页面后会重新出现';
    close.textContent = '×';
    close.addEventListener('click', () => panel.hidden = true);
    titleRow.appendChild(close);

    const status = document.createElement('div');
    status.className = 'cge-status';
    status.textContent = '正在读取当前对话……';

    panel.appendChild(titleRow);
    panel.appendChild(status);

    panel.appendChild(makeButton('重新扫描整个对话', async () => {
      await scanWholeConversation();
    }));

    panel.appendChild(makeButton('下载 JSON', async () => {
      await scanWholeConversation();
      const result = buildResult();
      window.__chatgptConversationExport = result;
      download(
        `${safeFilename(result.title)}.json`,
        JSON.stringify(result, null, 2),
        'application/json'
      );
    }));

    panel.appendChild(makeButton('下载 Markdown', async () => {
      await scanWholeConversation();
      const result = buildResult();
      window.__chatgptConversationExport = result;
      download(
        `${safeFilename(result.title)}.md`,
        toMarkdown(result),
        'text/markdown'
      );
    }));

    panel.appendChild(makeButton('复制 JSON', async () => {
      await scanWholeConversation();
      const result = buildResult();
      window.__chatgptConversationExport = result;
      const ok = await copyText(JSON.stringify(result, null, 2));
      setStatus(ok ? '<b>JSON 已复制到剪贴板</b>' : '<b>复制失败</b>');
      setTimeout(() => refreshStatus(), 1600);
    }));

    document.body.appendChild(panel);
    state.panel = panel;
    state.status = status;
    return panel;
  }

  function resetForRoute() {
    state.messages.clear();
    state.fallbackOrder = 0;
    state.currentScroller = null;
    state.routeKey = getRouteKey();

    if (!state.panel?.isConnected) createPanel();
    state.panel.hidden = !isSupportedPath();

    if (isSupportedPath()) {
      setStatus('正在读取当前对话……');
      scheduleCollect(300);
      setTimeout(() => scheduleCollect(0), 1000);
    }
  }

  function observeDom() {
    state.observer?.disconnect();
    state.observer = new MutationObserver(mutations => {
      if (!isSupportedPath()) return;
      const meaningful = mutations.some(mutation => {
        const target = mutation.target instanceof Element
          ? mutation.target
          : mutation.target.parentElement;
        if (target?.closest(`#${PANEL_ID}`)) return false;
        return (
          mutation.type === 'characterData' ||
          mutation.addedNodes.length > 0 ||
          mutation.removedNodes.length > 0
        );
      });
      if (meaningful) scheduleCollect(100);
    });
    state.observer.observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true
    });
  }

  function watchRoute() {
    clearInterval(state.routeTimer);
    state.routeTimer = window.setInterval(() => {
      const key = getRouteKey();
      if (key !== state.routeKey) resetForRoute();
    }, 700);
  }

  function init() {
    createPanel();
    resetForRoute();
    observeDom();
    watchRoute();
    collect();

    console.log('[ChatGPT Conversation Exporter v3] loaded');
  }

  function destroy() {
    clearTimeout(state.collectTimer);
    clearInterval(state.routeTimer);
    state.observer?.disconnect();
    state.panel?.remove();
    document.getElementById(STYLE_ID)?.remove();
  }

  window.__chatgptConversationExporterV3 = {
    collect,
    scan: scanWholeConversation,
    buildResult,
    toMarkdown,
    destroy
  };

  init();
})();
