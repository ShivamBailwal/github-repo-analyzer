'use strict';

/* ---------- small helpers ---------- */

const $ = (id) => document.getElementById(id);

function h(tag, props, ...kids) {
  const node = document.createElement(tag);

  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }

  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : String(kid));
  }

  return node;
}

const pad2 = (n) => String(n).padStart(2, '0');
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${pad2(Math.floor(s / 60))}:${pad2(s % 60)}`;
}

function duration(seconds) {
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${pad2(Math.floor(seconds % 60))}s`;
}

function shortRepo(url) {
  return url
    .replace(/^[a-z]+:\/\//i, '')
    .replace(/^git@([^:]+):/, '$1/')
    .replace(/^(www\.)?github\.com\//, '')
    .replace(/\.git$/, '')
    .replace(/\/$/, '');
}

function dayGroup(seconds) {
  const day = new Date(seconds * 1000);
  day.setHours(0, 0, 0, 0);
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const days = Math.round((today - day) / 86400000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return 'Previous 7 days';
  return 'Older';
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

if (window.DOMPurify) {
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
}

function renderMarkdown(text) {
  if (window.marked && window.DOMPurify) {
    return DOMPurify.sanitize(marked.parse(text, { gfm: true }));
  }
  // CDN unreachable: keep paragraphs readable without markdown
  return text.split(/\n{2,}/).map((p) => `<p>${escapeHtml(p).replace(/\n/g, '<br>')}</p>`).join('');
}

/* ---------- api ---------- */

async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function* streamEvents(url, signal) {
  const res = await fetch(url, { signal });
  if (!res.ok || !res.body) throw new Error(`Stream failed (${res.status})`);

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';

  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;

    buffer += value;
    let newline;
    while ((newline = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (line.trim()) yield JSON.parse(line);
    }
  }
}

/* ---------- vocabulary ---------- */

const NODE_NAMES = { clone: 'clone', router: 'router', frontend: 'frontend', backend: 'backend' };

const PENDING_TEXT = {
  clone: 'Cloning the repository',
  router: 'Router is reviewing the evidence',
  frontend: 'Frontend specialist is reading the code',
  backend: 'Backend specialist is reading the code',
};

const STATUS_TEXT = { running: 'investigating', done: 'answered', error: 'failed', interrupted: 'interrupted' };

const TOOL_VERBS = {
  read_text_file: 'read', read_file: 'read', read_multiple_files: 'read', read_media_file: 'read',
  list_directory: 'ls', list_directory_with_sizes: 'ls', directory_tree: 'tree',
  search_files: 'find', search_code: 'grep', get_file_info: 'stat', list_allowed_directories: 'roots',
  write_file: 'write', edit_file: 'edit', create_directory: 'mkdir', move_file: 'move',
};

// The specialists are told not to modify files, but the filesystem MCP server
// does expose these. Highlight them in the trace if one is ever called.
const WRITE_TOOLS = new Set(['write_file', 'edit_file', 'create_directory', 'move_file']);

const dim = (text) => h('span', { class: 'dim' }, text);

function toolArgument(name, args = {}) {
  switch (name) {
    case 'search_code':
      return [`"${args.pattern ?? ''}"`, args.file_glob && args.file_glob !== '*' ? dim(` in ${args.file_glob}`) : null];
    case 'search_files':
      return [`"${args.pattern ?? ''}"`, args.path ? dim(` under ${args.path}`) : null];
    case 'read_multiple_files':
      return [(args.paths || []).join(', ')];
    case 'move_file':
      return [`${args.source} → ${args.destination}`];
    case 'list_allowed_directories':
      return [dim('allowed directories')];
  }

  if (typeof args.path === 'string') return [args.path || '.'];

  const json = JSON.stringify(args);
  return [json === '{}' ? '' : json];
}

function readPaths(event) {
  if (event.type !== 'tool' || event.phase !== 'start' || !event.name.startsWith('read')) return [];
  const args = event.args || {};
  if (Array.isArray(args.paths)) return args.paths;
  return typeof args.path === 'string' ? [args.path] : [];
}

/* ---------- store ---------- */

const store = {
  threads: [],             // summaries for the sidebar, most recent first
  runningThread: null,     // the one thread investigating right now, if any
  view: 'new',             // 'new' (pick a repository) or 'thread'
  thread: null,            // the open thread: { id, title, repoUrl, repoPath, createdAt }
  reports: { frontend: '', backend: '' },
  turns: [],
  reportTab: 'frontend',
  follow: null,            // { controller, view } while streaming a live turn
  navToken: 0,             // ignores responses for threads we already left
  noteUntil: 0,            // keeps an error note in the composer for a moment
};

/* ---------- a single question and its investigation ---------- */

class TurnView {
  constructor(turn) {
    this.number = turn.number;
    this.status = turn.status;
    this.startedAt = turn.started_at * 1000;
    this.events = [];
    this.steps = [];
    this.tools = new Map();
    this.current = null;
    this.lastT = 0;
    this.answerText = '';

    this.build(turn.question);
    for (const event of turn.events) this.apply(event);
  }

  build(question) {
    this.statusEl = h('span', {}, STATUS_TEXT.running);
    this.clockEl = h('span', { class: 'trace-clock' }, '00:00');
    this.summaryEl = h('span', { class: 'trace-summary' });
    this.traceBody = h('ol', { class: 'trace-body' });

    this.traceHead = h('button', {
      class: 'trace-head', type: 'button', 'aria-expanded': 'true',
      onclick: () => this.setOpen(!this.trace.classList.contains('is-open')),
    },
      h('span', { class: 'trace-chev', 'aria-hidden': 'true' }, '▸'),
      h('span', { class: 'trace-title' }, 'Trace'),
      this.summaryEl,
      this.clockEl,
    );

    this.trace = h('section', { class: 'trace is-open' }, this.traceHead, this.traceBody);

    this.pendingEl = h('p', { class: 'pending' }, 'Starting');
    this.answerEl = h('section', { class: 'answer' }, this.pendingEl);

    const started = new Date(this.startedAt);
    const sameDay = started.toDateString() === new Date().toDateString();
    const time = started.toLocaleString([], sameDay
      ? { hour: '2-digit', minute: '2-digit' }
      : { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

    this.el = h('article', { class: 'turn', id: `turn-${this.number}` },
      h('div', { class: 'turn-meta' },
        h('span', { class: 'turn-num' }, pad2(this.number)),
        this.statusEl,
        h('span', { class: 'turn-time' }, time),
      ),
      h('h2', { class: `turn-q${question.length > 140 ? ' is-long' : ''}` }, question),
      this.trace,
      this.answerEl,
    );
  }

  apply(event) {
    this.events.push(event);
    this.lastT = Math.max(this.lastT, event.t || 0);

    if (event.type === 'step' && event.phase === 'start') this.stepStart(event);
    else if (event.type === 'step') this.stepEnd(event);
    else if (event.type === 'tool' && event.phase === 'start') this.toolStart(event);
    else if (event.type === 'tool') this.toolEnd(event);
    else if (event.type === 'answer') this.showAnswer(event.text);
    else if (event.type === 'error') this.showError(event);
    else if (event.type === 'end') this.finish(event.status);

    this.updateSummary();
  }

  stepStart(event) {
    const step = {
      node: event.node,
      start: event.t,
      toolCount: 0,
      detail: h('span', { class: 'step-detail' }, 'working'),
      dur: h('span', { class: 'dur' }),
      toolsEl: null,
    };

    step.el = h('li', { class: `step step--${event.node} is-running` },
      h('span', { class: 't' }, clock(event.t)),
      h('span', { class: 'mark' }),
      h('span', { class: 'step-name' }, NODE_NAMES[event.node] || event.node),
      step.detail,
      step.dur,
    );

    this.traceBody.append(step.el);
    this.steps.push(step);
    this.current = step;
    this.pendingEl.textContent = PENDING_TEXT[event.node] || 'Working';
  }

  stepEnd(event) {
    const step = this.current && this.current.node === event.node
      ? this.current
      : this.steps.findLast((s) => s.node === event.node);
    if (!step) return;

    step.ended = true;
    step.el.classList.remove('is-running');
    step.dur.textContent = duration(event.t - step.start);

    if (event.node === 'clone') {
      step.detail.textContent = 'repository cloned';
    } else if (event.node === 'router') {
      step.detail.replaceChildren(...routeDescription(event));
    } else {
      this.fileReport(step, event.report || '');
    }
  }

  fileReport(step, report) {
    const calls = plural(step.toolCount, 'tool call');
    const body = h('div', { class: 'step-report prose prose--small', html: renderMarkdown(report || '_Empty report._') });
    body.hidden = true;

    const toggle = h('button', {
      class: 'report-toggle', type: 'button',
      onclick: () => {
        body.hidden = !body.hidden;
        toggle.textContent = body.hidden ? 'read report' : 'hide report';
      },
    }, 'read report');

    step.detail.replaceChildren('report filed · ', calls, ' · ', toggle);
    step.el.append(body);
  }

  toolStart(event) {
    const step = this.current;
    if (!step) return;

    if (!step.toolsEl) {
      step.toolsEl = h('ol', { class: 'tools' });
      step.el.append(step.toolsEl);
    }

    const parts = toolArgument(event.name, event.args);
    const arg = h('span', { class: 'tool-arg' }, ...parts);
    arg.title = arg.textContent;

    const dur = h('span', { class: 'dur' });
    const row = h('li', { class: `tool is-running${WRITE_TOOLS.has(event.name) ? ' is-write' : ''}` },
      h('span', { class: 't' }, clock(event.t)),
      h('span', { class: 'mark' }),
      h('span', { class: 'tool-verb', title: event.name }, TOOL_VERBS[event.name] || event.name),
      arg,
      dur,
    );

    step.toolsEl.append(row);
    step.toolCount += 1;
    step.detail.textContent = `investigating · ${plural(step.toolCount, 'call')}`;
    this.tools.set(event.id, { row, dur, start: event.t });
  }

  toolEnd(event) {
    const tool = this.tools.get(event.id);
    if (!tool) return;

    tool.row.classList.remove('is-running');
    if (!event.ok) tool.row.classList.add('is-error');

    const took = event.t - tool.start;
    if (took >= 1) tool.dur.textContent = duration(took);
  }

  showAnswer(text) {
    this.answerText = text;

    const copy = h('button', {
      class: 'link-btn', type: 'button',
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(text);
          copy.textContent = 'copied';
        } catch {
          copy.textContent = 'copy failed';
        }
        setTimeout(() => { copy.textContent = 'copy markdown'; }, 1600);
      },
    }, 'copy markdown');

    this.answerEl.replaceChildren(
      h('div', { class: 'answer-label' }, 'Answer', copy),
      h('div', { class: 'prose', html: renderMarkdown(text) }),
    );
  }

  showError(event) {
    for (const step of this.steps) {
      step.el.classList.remove('is-running');
      if (!step.ended) step.detail.textContent = 'interrupted';
    }

    this.traceBody.append(h('li', { class: 'step step--error' },
      h('span', { class: 't' }, clock(event.t || this.lastT)),
      h('span', { class: 'mark' }),
      h('span', { class: 'step-name' }, 'error'),
      h('span', { class: 'step-detail' }, 'investigation stopped'),
      h('span', { class: 'dur' }),
      h('div', { class: 'step-error' }, event.message),
    ));

    let hint = 'No answer this time. The error is in the trace above, and the server console has the full traceback. Ask again to retry; this attempt is left out of the conversation.';
    if (/git clone failed/i.test(event.message)) {
      hint = 'The repository could not be cloned. Check the URL and that it is public or that your git credentials can reach it.';
    } else if (/server stopped/i.test(event.message)) {
      hint = 'The server stopped before this investigation finished. Ask again to rerun it; this attempt is left out of the conversation.';
    }

    this.answerEl.replaceChildren(h('p', { class: 'answer-note' }, hint));
  }

  finish(status) {
    this.status = status;
    for (const step of this.steps) step.el.classList.remove('is-running');
    for (const tool of this.tools.values()) tool.row.classList.remove('is-running');

    this.clockEl.textContent = clock(this.lastT);
    this.statusEl.textContent = STATUS_TEXT[status] || status;

    // Collapse the trace once the answer is in; keep it open otherwise
    if (status === 'done') this.setOpen(false);
  }

  setOpen(open) {
    this.trace.classList.toggle('is-open', open);
    this.traceHead.setAttribute('aria-expanded', String(open));
  }

  tick() {
    if (this.status === 'running') this.clockEl.textContent = clock((Date.now() - this.startedAt) / 1000);
  }

  updateSummary() {
    const visited = [];
    for (const step of this.steps) {
      if ((step.node === 'frontend' || step.node === 'backend') && visited.at(-1) !== step.node) visited.push(step.node);
    }

    const idle = { running: 'routing', error: 'stopped early', interrupted: 'stopped early' }[this.status] || 'no specialist needed';
    const route = visited.length ? visited.join(' → ') : idle;
    this.summaryEl.textContent = `${route} · ${plural(this.tools.size, 'tool call')}`;
  }
}

function routeDescription(event) {
  const strong = (text) => h('b', {}, text);

  if (event.next === 'frontend') return ['→ ', strong('frontend specialist')];
  if (event.next === 'backend') return ['→ ', strong('backend specialist')];
  if (event.route === 'END') return ['→ ', strong('answer')];
  return ['→ ', strong('stop'), dim(' (step limit reached)')];
}

/* ---------- rendering ---------- */

const threadScroll = $('thread-scroll');
const threadEl = $('thread');
const questionEl = $('question');

const DEFAULT_NOTE = '<kbd>Shift</kbd> + <kbd>↵</kbd> for a new line &middot; <kbd>/</kbd> to focus';

function nearBottom() {
  return threadScroll.scrollHeight - threadScroll.scrollTop - threadScroll.clientHeight < 160;
}

function scrollToBottom() {
  threadScroll.scrollTop = threadScroll.scrollHeight;
}

function inThread() {
  return store.view === 'thread' && store.thread;
}

function renderAll() {
  renderSidebar();
  renderScreens();
  renderBusy();
  renderDossier();
  renderRecent();
}

function renderSidebar() {
  const list = $('threads-list');

  if (!store.threads.length) {
    list.replaceChildren(h('p', { class: 'threads-empty' }, 'Your threads will show up here.'));
    return;
  }

  const groups = new Map();
  for (const thread of store.threads) {
    const label = dayGroup(thread.updated_at);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(thread);
  }

  list.replaceChildren(...[...groups].map(([label, threads]) =>
    h('section', { class: 'thread-group' },
      h('h2', { class: 'group-label' }, label),
      h('ol', {}, threads.map(threadRow)),
    )));
}

function threadRow(thread) {
  const active = inThread() && store.thread.id === thread.id;
  const running = thread.id === store.runningThread;

  return h('li', {
    class: `thread-row${active ? ' is-active' : ''}${running ? ' is-running' : ''}`,
    'data-id': thread.id,
  },
    h('a', { class: 'thread-link', href: `/t/${thread.id}`, 'data-nav': true, 'aria-current': active ? 'page' : null },
      h('span', { class: 'thread-title' }, thread.title || 'New thread'),
      h('span', { class: 'thread-repo' }, shortRepo(thread.repo_url)),
    ),
    h('div', { class: 'thread-actions' },
      h('button', { class: 'row-btn', type: 'button', onclick: () => startRename(thread) }, 'rename'),
      h('button', { class: 'row-btn', type: 'button', disabled: running, onclick: () => deleteThread(thread) }, 'delete'),
    ),
  );
}

function renderScreens() {
  const open = inThread();

  $('setup').hidden = Boolean(open);
  $('intro').hidden = !open || store.turns.length > 0;
  threadEl.hidden = !open;
  $('composer').hidden = !open;

  $('setup-eyebrow').textContent = store.threads.length ? 'New thread' : 'Step one';
  $('intro-repo').textContent = open ? shortRepo(store.thread.repoUrl) : '';

  $('bar-repo').hidden = !open;
  $('bar-repo-name').textContent = open ? shortRepo(store.thread.repoUrl) : '';

  document.title = open
    ? `${store.thread.title || shortRepo(store.thread.repoUrl)} · Repo Investigator`
    : 'Repo Investigator';
}

function renderBusy() {
  const open = inThread();
  const runningHere = open && store.runningThread === store.thread.id;

  $('send').disabled = Boolean(store.runningThread);
  $('send-label').textContent = runningHere && store.follow
    ? `Investigating ${clock((Date.now() - store.follow.view.startedAt) / 1000)}`
    : runningHere ? 'Investigating' : 'Investigate';

  if (Date.now() < store.noteUntil) return;

  const note = $('composer-note');
  note.classList.remove('is-error');

  if (store.runningThread && !runningHere) {
    const other = store.threads.find((t) => t.id === store.runningThread);
    note.replaceChildren(
      'Investigating in ',
      h('a', { href: `/t/${store.runningThread}`, 'data-nav': true }, other?.title || 'another thread'),
      '. You can ask here once it finishes.',
    );
  } else {
    note.innerHTML = DEFAULT_NOTE;
  }
}

function composerNote(message) {
  const note = $('composer-note');
  note.textContent = message;
  note.classList.add('is-error');
  store.noteUntil = Date.now() + 4000;
  setTimeout(renderBusy, 4100);
}

function renderRecent() {
  const repos = new Map();
  for (const thread of store.threads) {
    repos.set(thread.repo_url, (repos.get(thread.repo_url) || 0) + 1);
  }

  $('recent').hidden = repos.size === 0;
  $('recent-list').replaceChildren(...[...repos].slice(0, 6).map(([url, count]) =>
    h('li', {},
      h('button', { type: 'button', onclick: () => startThread(url) },
        h('span', { class: 'recent-name' }, shortRepo(url)),
        h('span', { class: 'recent-meta' }, plural(count, 'thread')),
      ),
    )));
}

function renderDossier() {
  const open = inThread();
  const info = $('repo-info');

  if (!open) {
    info.replaceChildren(h('span', { class: 'muted' }, 'No thread open'));
  } else {
    const { repoUrl, repoPath, createdAt } = store.thread;
    const isWeb = /^https?:\/\//.test(repoUrl);
    const started = new Date(createdAt * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });

    info.replaceChildren(
      isWeb
        ? h('a', { href: repoUrl, target: '_blank', rel: 'noopener noreferrer' }, shortRepo(repoUrl))
        : h('span', { class: 'mono' }, repoUrl),
      h('dl', {},
        h('dt', {}, 'host'), h('dd', {}, isWeb ? new URL(repoUrl).host : 'git'),
        h('dt', {}, 'clone'),
        repoPath
          ? h('dd', { title: repoPath }, repoPath.split(/[\\/]/).pop())
          : h('dd', {}, 'on first question'),
        h('dt', {}, 'thread'), h('dd', {}, plural(store.turns.length, 'question')),
        h('dt', {}, 'started'), h('dd', {}, started),
      ),
    );
  }

  for (const tab of document.querySelectorAll('.report-tab')) {
    const key = tab.dataset.report;
    tab.setAttribute('aria-selected', String(key === store.reportTab));
    const state = tab.querySelector('.report-state');
    state.textContent = store.reports[key] ? 'on file' : '—';
    state.classList.toggle('is-filed', Boolean(store.reports[key]));
  }

  const report = store.reports[store.reportTab];
  $('report-body').innerHTML = report
    ? renderMarkdown(report)
    : `<p class="empty">${open
      ? `The ${store.reportTab} specialist hasn't investigated in this thread yet.`
      : 'Open a thread to see its specialist reports.'}</p>`;

  renderFiles();
}

function renderFiles() {
  const counts = new Map();
  for (const view of store.turns) {
    for (const event of view.events) {
      for (const path of readPaths(event)) counts.set(path, (counts.get(path) || 0) + 1);
    }
  }

  const list = $('files');
  $('files-count').textContent = counts.size ? String(counts.size) : '';

  if (!counts.size) {
    list.replaceChildren(h('li', { class: 'empty' }, 'Files the specialists open will be listed here.'));
    return;
  }

  const rows = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 60);
  list.replaceChildren(...rows.map(([path, n]) => {
    const cut = path.lastIndexOf('/') + 1;
    return h('li', { title: path },
      h('span', { class: 'path' }, path.slice(0, cut), h('b', {}, path.slice(cut))),
      h('span', { class: 'n' }, n > 1 ? `×${n}` : ''),
    );
  }));
}

function setRepoNote(message, isError) {
  const note = $('repo-note');
  if (!note.dataset.default) note.dataset.default = note.innerHTML;

  if (message) note.textContent = message;
  else note.innerHTML = note.dataset.default;
  note.classList.toggle('is-error', Boolean(isError));
}

/* ---------- threads ---------- */

async function loadThreads() {
  try {
    const data = await request('GET', '/api/threads');
    store.threads = data.threads;
    store.runningThread = data.running_thread;

    // The server names a thread after its first question
    const current = inThread() && store.threads.find((t) => t.id === store.thread.id);
    if (current) store.thread.title = current.title;
  } catch {
    return false;
  }

  renderSidebar();
  renderScreens();
  renderBusy();
  renderRecent();
  return true;
}

function showNewThread() {
  stopFollowing();
  store.navToken += 1;
  store.view = 'new';
  store.thread = null;
  store.turns = [];
  store.reports = { frontend: '', backend: '' };
  threadEl.replaceChildren();
  setDrawer(null);
  renderAll();
}

async function openThread(id) {
  if (inThread() && store.thread.id === id) {
    setDrawer(null);
    return;
  }

  stopFollowing();
  const token = ++store.navToken;

  let snapshot;
  try {
    snapshot = await request('GET', `/api/threads/${id}`);
  } catch (err) {
    if (token !== store.navToken) return;
    navigate('/', { replace: true });
    setRepoNote(err.message === 'No such thread.' ? 'That thread no longer exists.' : err.message, true);
    return;
  }

  if (token === store.navToken) showThread(snapshot);
}

function showThread(snapshot) {
  store.view = 'thread';
  store.thread = {
    id: snapshot.id,
    title: snapshot.title,
    repoUrl: snapshot.repo_url,
    repoPath: snapshot.repo_path,
    createdAt: snapshot.created_at,
  };
  store.reports = { frontend: snapshot.frontend_report, backend: snapshot.backend_report };
  store.turns = snapshot.turns.map((turn) => new TurnView(turn));
  if (snapshot.running) store.runningThread = snapshot.id;

  threadEl.replaceChildren(...store.turns.map((view) => view.el));
  setDrawer(null);
  renderAll();
  scrollToBottom();

  const live = store.turns.find((view) => view.status === 'running');
  if (live) follow(live);
}

async function refreshThread(id) {
  if (!inThread() || store.thread.id !== id) return;

  try {
    const snapshot = await request('GET', `/api/threads/${id}`);
    if (!inThread() || store.thread.id !== id) return;

    store.thread.repoPath = snapshot.repo_path;
    store.thread.title = snapshot.title;
    store.reports = { frontend: snapshot.frontend_report, backend: snapshot.backend_report };
    renderDossier();
  } catch {
    // The dossier just stays as it was
  }
}

async function startThread(repoUrl) {
  let snapshot;
  try {
    snapshot = await request('POST', '/api/threads', { repo_url: repoUrl });
  } catch (err) {
    setRepoNote(err.message, true);
    $('repo-input').focus();
    return;
  }

  setRepoNote(null, false);
  $('repo-input').value = '';
  history.pushState({}, '', `/t/${snapshot.id}`);
  store.navToken += 1;
  store.threads = [snapshot, ...store.threads];
  showThread(snapshot);
  questionEl.focus();
  loadThreads();
}

function startRename(thread) {
  const row = document.querySelector(`.thread-row[data-id="${thread.id}"]`);
  if (!row) return;

  const input = h('input', { class: 'rename-input', value: thread.title, maxlength: '120', 'aria-label': 'Thread title' });
  let done = false;

  const finish = async (save) => {
    if (done) return;
    done = true;

    const title = input.value.trim();
    if (save && title && title !== thread.title) {
      try {
        await request('PATCH', `/api/threads/${thread.id}`, { title });
      } catch (err) {
        alert(err.message);
      }
    }
    await loadThreads();
  };

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));

  row.replaceChildren(input);
  input.focus();
  input.select();
}

async function deleteThread(thread) {
  const name = thread.title || 'this thread';
  if (!confirm(`Delete "${name}"?\n\nIts conversation, reports and traces are removed for good. The repository clone is kept.`)) return;

  try {
    await request('DELETE', `/api/threads/${thread.id}`);
  } catch (err) {
    alert(err.message);
    return;
  }

  if (inThread() && store.thread.id === thread.id) navigate('/', { replace: true });
  await loadThreads();
}

/* ---------- running an investigation ---------- */

function stopFollowing() {
  // The run carries on server-side; coming back to the thread reattaches
  store.follow?.controller.abort();
  store.follow = null;
}

async function follow(view) {
  const threadId = store.thread.id;
  const controller = new AbortController();
  store.follow = { controller, view };
  store.runningThread = threadId;
  renderBusy();
  renderSidebar();

  try {
    const url = `/api/threads/${threadId}/turns/${view.number}/events?from=${view.events.length}`;

    for await (const event of streamEvents(url, controller.signal)) {
      const stick = nearBottom();
      view.apply(event);

      if (event.type === 'step' && event.phase === 'end' && (event.node === 'frontend' || event.node === 'backend')) {
        store.reports[event.node] = event.report || '';
        store.reportTab = event.node;
        renderDossier();
      } else if (event.type === 'tool' && event.phase === 'start') {
        renderFiles();
      }

      if (stick) scrollToBottom();
    }

    if (view.status === 'running') throw new Error('stream closed early');
  } catch {
    if (controller.signal.aborted) return;

    if (view.status === 'running') {
      view.apply({ type: 'error', t: view.lastT, message: 'Lost the connection to the server. If it is still running, reload the page to reattach.' });
      view.apply({ type: 'end', t: view.lastT, status: 'error' });
    }
  } finally {
    if (store.follow?.controller === controller) store.follow = null;

    if (!controller.signal.aborted) {
      await refreshThread(threadId);
      await loadThreads();
    }
  }
}

async function submitQuestion() {
  const question = questionEl.value.trim();
  if (!question || !inThread() || store.runningThread) return;

  const threadId = store.thread.id;
  let turn;
  try {
    turn = await request('POST', `/api/threads/${threadId}/ask`, { question });
  } catch (err) {
    composerNote(err.message);
    loadThreads();
    return;
  }

  if (!inThread() || store.thread.id !== threadId) return;

  questionEl.value = '';
  autosize();

  const view = new TurnView(turn);
  store.turns.push(view);
  threadEl.append(view.el);
  renderScreens();
  renderDossier();
  scrollToBottom();

  follow(view);
  loadThreads();
}

/* ---------- navigation ---------- */

function route() {
  const match = location.pathname.match(/^\/t\/([0-9a-f]{32})\/?$/);
  if (match) openThread(match[1]);
  else showNewThread();
}

function navigate(path, { replace = false } = {}) {
  if (location.pathname !== path) history[replace ? 'replaceState' : 'pushState']({}, '', path);
  route();
}

window.addEventListener('popstate', route);

document.addEventListener('click', (e) => {
  const link = e.target.closest('a[data-nav]');
  if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  navigate(new URL(link.href).pathname);
});

/* ---------- wiring ---------- */

function autosize() {
  questionEl.style.height = 'auto';
  questionEl.style.height = `${Math.min(questionEl.scrollHeight, 220)}px`;
}

$('composer').addEventListener('submit', (e) => {
  e.preventDefault();
  submitQuestion();
});

questionEl.addEventListener('input', autosize);
questionEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    submitQuestion();
  }
});

$('starters').addEventListener('click', (e) => {
  const button = e.target.closest('button');
  if (!button) return;
  questionEl.value = button.textContent;
  autosize();
  questionEl.focus();
  questionEl.setSelectionRange(questionEl.value.length, questionEl.value.length);
});

$('repo-form').addEventListener('submit', (e) => {
  e.preventDefault();
  startThread($('repo-input').value);
});

for (const tab of document.querySelectorAll('.report-tab')) {
  tab.addEventListener('click', () => {
    store.reportTab = tab.dataset.report;
    renderDossier();
  });
}

function setDrawer(name) {
  $('threads').classList.toggle('is-open', name === 'threads');
  $('dossier').classList.toggle('is-open', name === 'dossier');
  $('scrim').hidden = !name;
}

const toggleDrawer = (name) => setDrawer($(name).classList.contains('is-open') ? null : name);
$('toggle-threads').addEventListener('click', () => toggleDrawer('threads'));
$('toggle-dossier').addEventListener('click', () => toggleDrawer('dossier'));
$('scrim').addEventListener('click', () => setDrawer(null));

$('toggle-theme').addEventListener('click', () => {
  const current = document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('theme', next); } catch {}
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') setDrawer(null);

  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName);
  if (e.key === '/' && !typing && !$('composer').hidden) {
    e.preventDefault();
    questionEl.focus();
  }
});

let ticks = 0;
setInterval(() => {
  ticks += 1;

  if (store.follow) {
    store.follow.view.tick();
    renderBusy();
  } else if (store.runningThread && ticks % 3 === 0) {
    // Another thread is investigating: notice when it finishes
    loadThreads();
  }
}, 1000);

/* ---------- boot ---------- */

(async function boot() {
  const reachable = await loadThreads();
  route();

  if (!reachable) {
    setRepoNote("Can't reach the server. Is server.py running?", true);
  } else if (location.pathname === '/') {
    $('repo-input').focus();
  }
})();
