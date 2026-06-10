import { api, fmt } from '/web/app.js';

const VIEW_KEY = 'td.sessions-view';
const VIEWS = [
  { key: 'recent',  label: 'Recent' },
  { key: 'tokens',  label: 'By tokens' },
  { key: 'project', label: 'By project' },
];

export default async function render(root) {
  const id = decodeURIComponent(location.hash.split('?')[0].split('/')[2] || '');
  if (id) {
    const mod = await import('/web/routes/session-detail.js');
    return mod.default(root, id);
  }
  return renderList(root);
}

async function renderList(root) {
  const saved = localStorage.getItem(VIEW_KEY);
  const view = VIEWS.some(v => v.key === saved) ? saved : 'recent';
  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Sessions</h2>
      <div class="spacer"></div>
      <div class="range-tabs">
        ${VIEWS.map(v => `<button data-view="${v.key}" class="${v.key === view ? 'active' : ''}">${v.label}</button>`).join('')}
      </div>
    </div>
    <div id="sessions-body"></div>`;
  root.querySelectorAll('[data-view]').forEach(btn => btn.addEventListener('click', () => {
    localStorage.setItem(VIEW_KEY, btn.dataset.view);
    render(root);
  }));
  const body = root.querySelector('#sessions-body');
  if (view === 'project') return renderByProject(body);
  return renderTable(body, view);
}

function sessionRow(s, { withProject = true } = {}) {
  const title = s.first_prompt ? fmt.short(s.first_prompt, 90) : '—';
  return `
    <tr class="rowlink" data-sid="${fmt.htmlSafe(s.session_id)}">
      <td class="mono">${fmt.ts(s.started)}</td>
      ${withProject ? `<td title="${fmt.htmlSafe(s.project_slug)}">${fmt.htmlSafe(s.project_name || s.project_slug)}</td>` : ''}
      <td class="blur-sensitive" title="${fmt.htmlSafe(s.first_prompt || '')}">${fmt.htmlSafe(title)}</td>
      <td class="num">${fmt.int(s.turns)}</td>
      <td class="num">${fmt.compact(s.tokens)}</td>
      <td class="mono">${fmt.htmlSafe(s.session_id.slice(0, 8))}…</td>
    </tr>`;
}

function wireRows(scope) {
  scope.querySelectorAll('tr.rowlink').forEach(tr => {
    tr.addEventListener('click', () => {
      location.hash = '#/sessions/' + encodeURIComponent(tr.dataset.sid);
    });
  });
}

async function renderTable(body, view) {
  const sort = view === 'tokens' ? 'tokens' : 'recent';
  const list = await api(`/api/sessions?limit=100&sort=${sort}`);
  body.innerHTML = `
    <div class="card">
      <table>
        <thead><tr><th>started</th><th>project</th><th>first prompt</th><th class="num">turns</th><th class="num">tokens</th><th>session</th></tr></thead>
        <tbody>${list.map(s => sessionRow(s)).join('')}</tbody>
      </table>
    </div>`;
  wireRows(body);
}

async function renderByProject(body) {
  const [list, cards] = await Promise.all([
    api('/api/sessions?limit=500&sort=recent'),
    api('/api/projects/cards'),
  ]);
  const meta = Object.fromEntries(cards.map(c => [c.project_slug, c]));
  const groups = new Map();
  for (const s of list) {
    if (!groups.has(s.project_slug)) groups.set(s.project_slug, []);
    groups.get(s.project_slug).push(s);
  }
  const ordered = Array.from(groups.entries())
    .sort((a, b) => (b[1][0].ended || '').localeCompare(a[1][0].ended || ''));

  body.innerHTML = ordered.map(([slug, sessions]) => {
    const m = meta[slug] || {};
    const name = m.project_name || sessions[0].project_name || slug;
    const tokens = sessions.reduce((acc, s) => acc + (s.tokens || 0), 0);
    return `
      <details class="proj-group">
        <summary>
          <strong>${fmt.htmlSafe(name)}</strong>
          <span class="muted" style="font-size:12px">${sessions.length} sessions · ${fmt.compact(tokens)} tokens${m.cost_usd != null ? ` · ${fmt.usd(m.cost_usd)}` : ''}</span>
        </summary>
        ${m.description ? `<div class="gdesc">${fmt.htmlSafe(m.description)}</div>` : ''}
        <table>
          <thead><tr><th>started</th><th>first prompt</th><th class="num">turns</th><th class="num">tokens</th><th>session</th></tr></thead>
          <tbody>${sessions.map(s => sessionRow(s, { withProject: false })).join('')}</tbody>
        </table>
      </details>`;
  }).join('') || '<div class="card muted">no sessions</div>';
  wireRows(body);
}
