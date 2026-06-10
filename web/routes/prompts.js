import { api, fmt } from '/web/app.js';

const SORTS = [
  { key: 'tokens', label: 'Most tokens' },
  { key: 'recent', label: 'Most recent' },
];

function readQuery() {
  const q = (location.hash.split('?')[1] || '');
  const sortM = /(?:^|&)sort=([^&]+)/.exec(q);
  const sessM = /(?:^|&)session=([^&]+)/.exec(q);
  return {
    sort: SORTS.find(s => s.key === (sortM && decodeURIComponent(sortM[1]))) || SORTS[0],
    session: sessM ? decodeURIComponent(sessM[1]) : null,
  };
}

function writeQuery(sortKey, session) {
  const parts = ['sort=' + encodeURIComponent(sortKey)];
  if (session) parts.push('session=' + encodeURIComponent(session));
  location.hash = '#/prompts?' + parts.join('&');
}

export default async function (root) {
  const { sort, session } = readQuery();
  let url = '/api/prompts?limit=100&sort=' + encodeURIComponent(sort.key);
  if (session) url += '&session=' + encodeURIComponent(session);
  const rows = await api(url);

  const sortTabs = `
    <div class="range-tabs" role="tablist">
      ${SORTS.map(s => `<button data-sort="${s.key}" class="${s.key === sort.key ? 'active' : ''}">${s.label}</button>`).join('')}
    </div>`;

  const sessionChip = session ? `
    <span class="pill" style="display:inline-flex;align-items:center;gap:6px">
      session ${fmt.htmlSafe(session.slice(0, 8))}…
      <a href="#" data-clear-session title="Remove filter" style="color:var(--muted)">✕</a>
    </span>` : '';

  const subtitle = sort.key === 'recent'
    ? 'Your latest prompts and the turn each one triggered. Click a row for the full breakdown.'
    : 'The prompts that cost the most tokens. Click a row for the full breakdown.';

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Prompts</h2>
      ${sessionChip}
      <div class="spacer"></div>
      ${sortTabs}
    </div>

    <div class="card">
      <p class="muted" style="margin:0 0 14px">${subtitle}</p>
      <table id="prompts">
        <thead><tr>
          <th>${sort.key === 'recent' ? 'when' : 'cache cost'}</th>
          <th>prompt</th>
          <th>model</th>
          <th class="num">tokens</th>
          <th class="num">cache rd</th>
          <th>session</th>
        </tr></thead>
        <tbody>
          ${rows.map((r, i) => `
            <tr data-i="${i}" style="cursor:pointer">
              <td class="${sort.key === 'recent' ? 'mono' : 'num mono'}">${sort.key === 'recent' ? fmt.ts(r.timestamp) : fmt.usd4(r.estimated_cost_usd)}</td>
              <td class="blur-sensitive">${fmt.htmlSafe(fmt.short(r.prompt_text, 110))}</td>
              <td><span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span></td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td><a href="#/sessions/${encodeURIComponent(r.session_id)}" class="mono" onclick="event.stopPropagation()">${fmt.htmlSafe(r.session_id.slice(0, 8))}…</a></td>
            </tr>`).join('') || '<tr><td colspan="6" class="muted">no prompts yet</td></tr>'}
        </tbody>
      </table>
    </div>
    <div id="drawer"></div>
  `;

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeQuery(btn.dataset.sort, session));
  });
  const clear = root.querySelector('[data-clear-session]');
  if (clear) clear.addEventListener('click', e => { e.preventDefault(); writeQuery(sort.key, null); });

  root.querySelectorAll('#prompts tbody tr').forEach(tr => {
    tr.addEventListener('click', () => openDrawer(rows[Number(tr.dataset.i)]));
  });
}

async function openDrawer(r) {
  const drawer = document.getElementById('drawer');
  drawer.innerHTML = '<div class="card"><p class="muted">loading…</p></div>';
  drawer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  let turn = null;
  if (r.prompt_id) {
    try {
      turn = await api(`/api/prompts/turn?session=${encodeURIComponent(r.session_id)}&prompt=${encodeURIComponent(r.prompt_id)}`);
    } catch { /* older records may have no prompt_id */ }
  }

  const usage = turn && turn.models.length ? turn.models.reduce((acc, m) => ({
    in: acc.in + m.input_tokens, out: acc.out + m.output_tokens,
    cacheRd: acc.cacheRd + m.cache_read_tokens,
    cacheCr: acc.cacheCr + m.cache_create_5m_tokens + m.cache_create_1h_tokens,
    calls: acc.calls + m.turns,
  }), { in: 0, out: 0, cacheRd: 0, cacheCr: 0, calls: 0 }) : null;

  drawer.innerHTML = `
    <div class="card">
      <h3 style="display:flex;align-items:center">
        <span>Prompt detail</span>
        <span class="spacer"></span>
        <span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span>
      </h3>
      <pre class="blur-sensitive">${fmt.htmlSafe(r.prompt_text || '')}</pre>

      ${usage ? `
        <h3 style="margin-top:14px">What this turn cost</h3>
        <div class="session-facts" style="margin-top:4px">
          <span><b>${fmt.compact(usage.in)}</b> in · <b>${fmt.compact(usage.out)}</b> out · <b>${fmt.compact(usage.cacheCr)}</b> cache cr · <b>${fmt.compact(usage.cacheRd)}</b> cache rd</span>
          <span><b>${usage.calls}</b> API calls</span>
          ${turn.cost_usd != null ? `<span>est. <b style="color:var(--good)">${fmt.usd4(turn.cost_usd)}</b></span>` : ''}
          ${turn.result_count ? `<span><b>${fmt.compact(turn.result_tokens_total)}</b> tool-result tokens (max <b>${fmt.compact(turn.result_tokens_max)}</b>)</span>` : ''}
        </div>
        ${turnTips(turn)}
        ${turn.tool_calls.length ? `
          <h3 style="margin-top:14px">Tool calls in this turn</h3>
          <ul class="kv" style="max-width:560px">
            ${turn.tool_calls.slice(0, 12).map(t => `
              <li><span title="${fmt.htmlSafe(t.target || '')}">${fmt.htmlSafe(t.tool_name)}${t.target ? ' · ' + fmt.htmlSafe(fmt.short(fmt.basename(t.target), 48)) : ''}</span><span class="num">×${t.calls}</span></li>`).join('')}
          </ul>` : ''}
      ` : ''}

      <div class="flex" style="margin-top:14px;flex-wrap:wrap;gap:14px">
        <span class="muted">${fmt.ts(r.timestamp)}</span>
        <span class="muted">${fmt.int(r.billable_tokens)} billable (first response) · ${fmt.int(r.cache_read_tokens)} cache rd · ~${fmt.usd4(r.estimated_cost_usd)} cache cost</span>
        <span class="spacer"></span>
        <a href="#/sessions/${encodeURIComponent(r.session_id)}">Open session →</a>
      </div>
    </div>`;
}

function turnTips(turn) {
  const tips = [];
  if (turn.result_tokens_max >= 20000) {
    tips.push(`One tool result was ${fmt.compact(turn.result_tokens_max)} tokens — prefer Grep or partial reads over full-file dumps, and pipe long command output through head/tail.`);
  }
  const repeated = turn.tool_calls.filter(t => t.calls >= 3 && t.target);
  if (repeated.length) {
    tips.push(`${fmt.basename(repeated[0].target)} was hit ${repeated[0].calls}× in this single turn — asking for a summary up front would avoid the repeats.`);
  }
  if (!tips.length) return '';
  return `
    <div class="tips-strip">
      ${tips.map(t => `<div class="tip"><p class="tip-body" style="margin:0">💡 ${fmt.htmlSafe(t)}</p></div>`).join('')}
    </div>`;
}
