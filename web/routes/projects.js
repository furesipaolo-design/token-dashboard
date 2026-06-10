import { api, fmt, $ } from '/web/app.js';
import { donutChart, sparklineChart, stackedBarChart } from '/web/charts.js';

const VIEW_KEY = 'td.projects-view';
let pageRoot = null;

export default async function render(root) {
  pageRoot = root;
  const view = localStorage.getItem(VIEW_KEY) === 'cards' ? 'cards' : 'list';
  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Projects</h2>
      <div class="spacer"></div>
      <div class="range-tabs">
        <button data-view="list"  class="${view === 'list'  ? 'active' : ''}">List</button>
        <button data-view="cards" class="${view === 'cards' ? 'active' : ''}">Cards</button>
      </div>
    </div>
    <div id="projects-body"></div>`;
  root.querySelectorAll('[data-view]').forEach(btn => btn.addEventListener('click', () => {
    localStorage.setItem(VIEW_KEY, btn.dataset.view);
    render(root);
  }));

  const body = $('#projects-body', root);
  const rows = await api('/api/projects/cards');
  const active = rows.filter(r => !r.archived);
  const archived = rows.filter(r => r.archived);
  if (view === 'cards') renderCards(body, active);
  else renderList(body, active);
  body.insertAdjacentHTML('beforeend', archivedSection(archived));
  wireArchived(body, archived);
}

const rerender = () => { if (pageRoot) render(pageRoot); };

function projectRow(r, { restore = false } = {}) {
  return `
    <tr class="rowlink" data-slug="${fmt.htmlSafe(r.project_slug)}">
      <td title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}</td>
      <td class="num" style="color:var(--good)">${fmt.usd(r.cost_usd)}</td>
      <td class="num">${fmt.int(r.sessions)}</td>
      <td class="num">${fmt.int(r.turns)}</td>
      <td class="num">${fmt.int(r.billable_tokens)}</td>
      <td class="num">${fmt.int(r.cache_read_tokens)}</td>
      ${restore ? `<td class="num">
        <button class="ghost" data-archive="false" title="Restore to the main list">↩ restore</button>
      </td>` : ''}
    </tr>`;
}

const TABLE_HEAD = (actions) => `
  <thead><tr><th>project</th><th class="num">est. cost</th><th class="num">sessions</th>
  <th class="num">turns</th><th class="num">billable tokens</th><th class="num">cache reads</th>${actions ? '<th></th>' : ''}</tr></thead>`;

function renderList(body, rows) {
  body.insertAdjacentHTML('beforeend', `
    <div class="card">
      <p class="muted" style="margin:0 0 14px">Sorted by billable token spend. Click a row for the project sum-up (archiving lives there and in the cards view).</p>
      <table>
        ${TABLE_HEAD(false)}
        <tbody>${rows.map(r => projectRow(r)).join('') || '<tr><td colspan="6" class="muted">no active projects</td></tr>'}</tbody>
      </table>
    </div>`);
  wireProjectRows(body.lastElementChild, rows);
}

function wireProjectRows(scope, rows) {
  const bySlug = Object.fromEntries(rows.map(r => [r.project_slug, r]));
  scope.querySelectorAll('tr.rowlink').forEach(tr => {
    const r = bySlug[tr.dataset.slug];
    tr.addEventListener('click', e => {
      if (e.target.closest('[data-archive]')) return;
      openProjectModal(r);
    });
    const btn = tr.querySelector('[data-archive]');
    if (btn) btn.addEventListener('click', async e => {
      e.stopPropagation();
      await setArchived(r.project_slug, btn.dataset.archive === 'true');
    });
  });
}

function renderCards(body, rows) {
  body.insertAdjacentHTML('beforeend', `
    <p class="muted" style="margin:0 0 12px">One card per project — sparkline is billable tokens over the last 30 days. Click a card for the full project sum-up. ✎ edits the description, ▤ archives.</p>
    <div class="cards-grid">
      ${rows.map((r, i) => `
        <div class="card proj-card" data-i="${i}">
          <h3 style="margin-bottom:2px" title="${fmt.htmlSafe(r.project_slug)}">
            ${fmt.htmlSafe(r.project_name || r.project_slug)}
            <span class="card-icons">
              <a class="edit-desc" href="#" data-edit title="Edit description">✎</a>
              <a class="archive-btn" href="#" data-archive-card title="Archive this project">▤</a>
            </span>
          </h3>
          <div class="desc" data-desc>${r.description ? fmt.htmlSafe(r.description) : '<span class="muted">no description — ✎ to add one</span>'}</div>
          <div class="kpis">
            <span class="cost"><b>${fmt.usd(r.cost_usd)}</b></span>
            <span><b>${fmt.int(r.sessions)}</b> sessions</span>
            <span><b>${fmt.compact(r.billable_tokens)}</b> tokens</span>
          </div>
          <div class="spark" style="height:48px"></div>
        </div>`).join('') || '<p class="muted">no active projects</p>'}
    </div>`);

  for (const el of body.querySelectorAll('.proj-card')) {
    const r = rows[Number(el.dataset.i)];
    if (r.daily && r.daily.length > 1) {
      sparklineChart(el.querySelector('.spark'), {
        x: r.daily.map(d => d.day),
        values: r.daily.map(d => d.tokens),
        color: '#4A9EFF',
      });
    } else {
      el.querySelector('.spark').innerHTML = '<span class="muted" style="font-size:11px">no activity in the last 30 days</span>';
    }
    el.addEventListener('click', e => {
      if (e.target.closest('[data-edit]') || e.target.closest('[data-archive-card]') || e.target.closest('.desc-form')) return;
      openProjectModal(r);
    });
    el.querySelector('[data-edit]').addEventListener('click', e => {
      e.preventDefault();
      e.stopPropagation();
      editDescription(el.querySelector('[data-desc]'), r);
    });
    el.querySelector('[data-archive-card]').addEventListener('click', async e => {
      e.preventDefault();
      e.stopPropagation();
      await setArchived(r.project_slug, true);
    });
  }
}

function archivedSection(archived) {
  if (!archived.length) return '';
  return `
    <details class="proj-group" style="margin-top:18px" id="archived-projects">
      <summary>
        <strong>Archived projects</strong>
        <span class="muted" style="font-size:12px">${archived.length} — hidden from the main view, still sorted by billable tokens</span>
      </summary>
      <table>
        ${TABLE_HEAD(true)}
        <tbody>${archived.map(r => projectRow(r, { restore: true })).join('')}</tbody>
      </table>
    </details>`;
}

function wireArchived(body, archived) {
  const section = body.querySelector('#archived-projects');
  if (section) wireProjectRows(section, archived);
}

async function setArchived(slug, archived) {
  await api('/api/projects/archive', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slug, archived }),
  });
  rerender();
}

async function openProjectModal(r) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal wide">
      <div class="flex">
        <h2 style="margin:0">${fmt.htmlSafe(r.project_name || r.project_slug)}</h2>
        <a class="edit-desc" href="#" data-edit title="Edit description">✎</a>
        <span class="spacer"></span>
        <button data-archive-modal title="${r.archived ? 'Restore to the main list' : 'Move to the archived list'}">
          ${r.archived ? '↩ Restore' : '▤ Archive'}
        </button>
        <button class="close-x" title="Close (Esc)">✕</button>
      </div>
      <p class="muted" data-desc style="margin:6px 0 0">${r.description ? fmt.htmlSafe(r.description) : '<span class="muted">no description — ✎ to add one</span>'}</p>
      <div class="kpis">
        <span class="cost"><b>${fmt.usd(r.cost_usd)}</b> est. cost</span>
        <span><b>${fmt.int(r.sessions)}</b> sessions</span>
        <span><b>${fmt.int(r.turns)}</b> turns</span>
        <span><b>${fmt.compact(r.billable_tokens)}</b> billable</span>
        <span><b>${fmt.compact(r.cache_read_tokens)}</b> cache reads</span>
      </div>
      <div id="pm-body"><p class="muted">loading…</p></div>
    </div>`;
  document.body.appendChild(overlay);

  const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  overlay.querySelector('.close-x').addEventListener('click', close);
  overlay.querySelector('[data-edit]').addEventListener('click', e => {
    e.preventDefault();
    editDescription(overlay.querySelector('[data-desc]'), r);
  });
  overlay.querySelector('[data-archive-modal]').addEventListener('click', async () => {
    await setArchived(r.project_slug, !r.archived);
    close();
  });

  const d = await api('/api/projects/detail?slug=' + encodeURIComponent(r.project_slug));
  const pm = overlay.querySelector('#pm-body');
  pm.innerHTML = `
    <div class="row cols-2" style="margin-top:14px">
      <div>
        <h3>Daily work (30 days)</h3>
        <div id="pm-daily" style="height:200px"></div>
      </div>
      <div>
        <h3>Models</h3>
        <div id="pm-models" style="height:200px"></div>
      </div>
    </div>
    <div class="row cols-2" style="margin-top:14px">
      <div>
        <h3>Top tools</h3>
        <ul class="kv">${d.top_tools.slice(0, 6).map(t => `<li><span>${fmt.htmlSafe(t.tool_name)}</span><span class="num">${fmt.int(t.calls)}</span></li>`).join('') || '<li class="muted">none</li>'}</ul>
      </div>
      <div>
        <h3>Top files</h3>
        <ul class="kv">${d.top_files.slice(0, 6).map(f => `<li><span title="${fmt.htmlSafe(f.file)}">${fmt.htmlSafe(fmt.basename(f.file))}</span><span class="num">${fmt.int(f.calls)}</span></li>`).join('') || '<li class="muted">none</li>'}</ul>
      </div>
    </div>
    <h3 style="margin-top:16px">Most expensive sessions</h3>
    <table>
      <thead><tr><th>started</th><th>first prompt</th><th class="num">turns</th><th class="num">tokens</th></tr></thead>
      <tbody>
        ${d.top_sessions.map(s => `
          <tr class="rowlink" data-sid="${fmt.htmlSafe(s.session_id)}">
            <td class="mono">${fmt.ts(s.started)}</td>
            <td class="blur-sensitive">${fmt.htmlSafe(fmt.short(s.first_prompt || '—', 80))}</td>
            <td class="num">${fmt.int(s.turns)}</td>
            <td class="num">${fmt.compact(s.tokens)}</td>
          </tr>`).join('') || '<tr><td colspan="4" class="muted">no sessions</td></tr>'}
      </tbody>
    </table>`;

  pm.querySelectorAll('tr.rowlink').forEach(tr => tr.addEventListener('click', () => {
    close();
    location.hash = '#/sessions/' + encodeURIComponent(tr.dataset.sid);
  }));

  if (d.daily && d.daily.length) {
    stackedBarChart(overlay.querySelector('#pm-daily'), {
      categories: d.daily.map(x => x.day),
      series: [
        { name: 'input',        values: d.daily.map(x => x.input_tokens),        color: '#4A9EFF' },
        { name: 'output',       values: d.daily.map(x => x.output_tokens),       color: '#7C5CFF' },
        { name: 'cache create', values: d.daily.map(x => x.cache_create_tokens), color: '#E8A23B' },
      ],
    });
  } else {
    overlay.querySelector('#pm-daily').innerHTML = '<span class="muted" style="font-size:12px">no activity in the last 30 days</span>';
  }
  const donutData = d.models.map(m => ({
    name: fmt.modelShort(m.model),
    value: (m.input_tokens || 0) + (m.output_tokens || 0)
         + (m.cache_create_5m_tokens || 0) + (m.cache_create_1h_tokens || 0),
  })).filter(x => x.value > 0);
  if (donutData.length) donutChart(overlay.querySelector('#pm-models'), donutData);
  else overlay.querySelector('#pm-models').innerHTML = '<span class="muted" style="font-size:12px">no model data</span>';
}

// Inline description editor — works on any [data-desc] container (card or modal).
function editDescription(descEl, r) {
  if (descEl.parentElement.querySelector('.desc-form')) return;
  const form = document.createElement('div');
  form.className = 'desc-form';
  form.innerHTML = `
    <textarea>${fmt.htmlSafe(r.description || '')}</textarea>
    <div class="actions">
      <button class="primary" data-save>Save</button>
      <button data-cancel>Cancel</button>
      ${r.description_source === 'manual' ? '<button data-reset title="Back to the auto-extracted description">Reset to auto</button>' : ''}
      <span class="muted" style="font-size:11px">${r.description_source === 'manual' ? 'manual override' : 'auto from CLAUDE.md/README'}</span>
    </div>`;
  descEl.style.display = 'none';
  descEl.after(form);
  form.addEventListener('click', e => e.stopPropagation());

  const close = () => { form.remove(); descEl.style.display = ''; };
  const save = async (text) => {
    const res = await api('/api/projects/description', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug: r.project_slug, description: text }),
    });
    r.description = res.description;
    r.description_source = res.source;
    descEl.innerHTML = fmt.htmlSafe(res.description || '') ||
      '<span class="muted">no description — ✎ to add one</span>';
    close();
    // The modal floats over the page: re-render it behind so the card/list
    // shows the new description too (the modal itself is left untouched).
    if (descEl.closest('.modal')) rerender();
  };
  form.querySelector('[data-save]').addEventListener('click', () => save(form.querySelector('textarea').value));
  form.querySelector('[data-cancel]').addEventListener('click', close);
  const reset = form.querySelector('[data-reset]');
  if (reset) reset.addEventListener('click', () => save(''));
}
