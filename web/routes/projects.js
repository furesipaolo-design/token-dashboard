import { api, fmt, $ } from '/web/app.js';
import { donutChart, sparklineChart } from '/web/charts.js';

const VIEW_KEY = 'td.projects-view';

export default async function render(root) {
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
  if (view === 'cards') await renderCards(body);
  else await renderList(body);
}

async function renderList(body) {
  const rows = await api('/api/projects');
  body.innerHTML = `
    <div class="card">
      <p class="muted" style="margin:0 0 14px">Sorted by billable token spend. Cache reads are billed cheaper, so high cache-read columns are good.</p>
      <table>
        <thead><tr><th>project</th><th class="num">sessions</th><th class="num">turns</th><th class="num">billable tokens</th><th class="num">cache reads</th></tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}</td>
              <td class="num">${fmt.int(r.sessions)}</td>
              <td class="num">${fmt.int(r.turns)}</td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

async function renderCards(body) {
  const rows = await api('/api/projects/cards');
  body.innerHTML = `
    <p class="muted" style="margin:0 0 12px">One card per project — sparkline is billable tokens over the last 30 days. Click a card for models, tools and files. ✎ edits the description.</p>
    <div class="cards-grid">
      ${rows.map((r, i) => `
        <div class="card proj-card" data-slug="${fmt.htmlSafe(r.project_slug)}" data-i="${i}">
          <h3 style="margin-bottom:2px" title="${fmt.htmlSafe(r.project_slug)}">
            ${fmt.htmlSafe(r.project_name || r.project_slug)}
            <a class="edit-desc" href="#" data-edit title="Edit description">✎</a>
          </h3>
          <div class="desc" data-desc>${r.description ? fmt.htmlSafe(r.description) : '<span class="muted">no description — ✎ to add one</span>'}</div>
          <div class="kpis">
            <span class="cost"><b>${fmt.usd(r.cost_usd)}</b></span>
            <span><b>${fmt.int(r.sessions)}</b> sessions</span>
            <span><b>${fmt.compact(r.billable_tokens)}</b> tokens</span>
          </div>
          <div class="spark" style="height:48px"></div>
          <div class="detail" style="display:none"></div>
        </div>`).join('')}
    </div>`;

  const cards = Array.from(body.querySelectorAll('.proj-card'));
  for (const el of cards) {
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
      if (e.target.closest('[data-edit]') || e.target.closest('.desc-form')) return;
      toggleDetail(el, r);
    });
    el.querySelector('[data-edit]').addEventListener('click', e => {
      e.preventDefault();
      e.stopPropagation();
      editDescription(el, r);
    });
  }
}

async function toggleDetail(el, r) {
  const box = el.querySelector('.detail');
  if (box.style.display !== 'none') { box.style.display = 'none'; return; }
  box.style.display = '';
  if (!box.dataset.loaded) {
    box.innerHTML = '<span class="muted">loading…</span>';
    const d = await api('/api/projects/detail?slug=' + encodeURIComponent(r.project_slug));
    box.innerHTML = `
      <div class="row cols-2">
        <div><h3 style="font-size:12px">Models</h3><div class="mini-donut" style="height:150px"></div></div>
        <div>
          <h3 style="font-size:12px">Top tools</h3>
          <ul>${d.top_tools.slice(0, 5).map(t => `<li><span>${fmt.htmlSafe(t.tool_name)}</span><span class="num">${fmt.int(t.calls)}</span></li>`).join('') || '<li class="muted">none</li>'}</ul>
          <h3 style="font-size:12px;margin-top:12px">Top files</h3>
          <ul>${d.top_files.slice(0, 5).map(f => `<li><span title="${fmt.htmlSafe(f.file)}">${fmt.htmlSafe(fmt.basename(f.file))}</span><span class="num">${fmt.int(f.calls)}</span></li>`).join('') || '<li class="muted">none</li>'}</ul>
        </div>
      </div>`;
    const donutData = d.models.map(m => ({
      name: fmt.modelShort(m.model),
      value: (m.input_tokens || 0) + (m.output_tokens || 0)
           + (m.cache_create_5m_tokens || 0) + (m.cache_create_1h_tokens || 0),
    })).filter(x => x.value > 0);
    if (donutData.length) donutChart(box.querySelector('.mini-donut'), donutData);
    else box.querySelector('.mini-donut').innerHTML = '<span class="muted" style="font-size:11px">no model data</span>';
    box.dataset.loaded = '1';
  }
}

function editDescription(el, r) {
  const descEl = el.querySelector('[data-desc]');
  if (el.querySelector('.desc-form')) return;
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
  };
  form.querySelector('[data-save]').addEventListener('click', () => save(form.querySelector('textarea').value));
  form.querySelector('[data-cancel]').addEventListener('click', close);
  const reset = form.querySelector('[data-reset]');
  if (reset) reset.addEventListener('click', () => save(''));
}
