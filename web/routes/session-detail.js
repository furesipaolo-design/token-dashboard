import { api, fmt } from '/web/app.js';

export default async function (root, id) {
  const [turns, meta] = await Promise.all([
    api('/api/sessions/' + encodeURIComponent(id)),
    api('/api/sessions/' + encodeURIComponent(id) + '/meta'),
  ]);

  const files = meta.files_edited || [];
  const filesLabel = files.length
    ? files.slice(0, 5).map(fmt.basename).join(', ') + (files.length > 5 ? ` +${files.length - 5}` : '')
    : null;
  const toolsLabel = (meta.top_tools || []).slice(0, 4)
    .map(t => `${t.tool_name}×${t.calls}`).join(' ');
  const modelBadges = (meta.models || [])
    .map(m => `<span class="badge ${fmt.modelClass(m.model)}">${fmt.htmlSafe(fmt.modelShort(m.model))}</span>`)
    .join(' ');

  const groups = groupByTurn(turns);
  markExpensive(groups, 3);

  root.innerHTML = `
    <a class="btn-back" href="#/sessions">← All sessions</a>

    <div class="card">
      <h2 style="display:flex;align-items:center;margin-bottom:4px">
        <span>Session <span class="mono" style="font-weight:400;font-size:13px">${fmt.htmlSafe(id.slice(0, 8))}…</span></span>
        <span class="spacer"></span>
        <a href="#/prompts?session=${encodeURIComponent(id)}" style="font-weight:400;font-size:12px">This session's prompts →</a>
      </h2>
      ${meta.first_prompt ? `<p class="session-title blur-sensitive">“${fmt.htmlSafe(fmt.short(meta.first_prompt, 220))}”</p>` : ''}
      <div class="session-facts">
        <span>📁 <b>${fmt.htmlSafe(meta.project_name || meta.project_slug || '')}</b></span>
        <span>${fmt.ts(meta.started)} → ${fmt.ts(meta.ended)} (<b>${fmt.duration(meta.started, meta.ended)}</b>)</span>
        <span><b>${fmt.int(meta.turns)}</b> turns</span>
        <span><b>${fmt.compact(meta.input_tokens)}</b> in · <b>${fmt.compact(meta.output_tokens)}</b> out · <b>${fmt.compact(meta.cache_read_tokens)}</b> cache rd</span>
        ${meta.cost_usd != null ? `<span>est. <b style="color:var(--good)">${fmt.usd(meta.cost_usd)}</b></span>` : ''}
        ${modelBadges ? `<span>${modelBadges}</span>` : ''}
      </div>
      <div class="session-facts" style="margin-top:6px">
        ${filesLabel ? `<span title="${fmt.htmlSafe(files.join('\n'))}">✏️ ${fmt.htmlSafe(filesLabel)}</span>` : ''}
        ${toolsLabel ? `<span>🔧 ${fmt.htmlSafe(toolsLabel)}</span>` : ''}
      </div>
      ${renderTips(meta.tips)}
    </div>

    <div class="card" style="margin-top:16px">
      <h3 style="display:flex;align-items:baseline;gap:8px">
        <span>Turn-by-turn</span>
        <span class="muted" style="font-size:11px;font-weight:400">grouped by prompt — the ⚡ turns ate the most tokens, expand them first</span>
      </h3>
      ${groups.map(g => renderTurn(g)).join('')}
    </div>`;
}

function groupByTurn(turns) {
  // A group = one user prompt + everything until the next prompt
  // (assistant snapshots, tool results, sidechain records).
  const groups = [];
  let current = null;
  for (const t of turns) {
    const startsTurn = t.type === 'user' && t.prompt_text;
    if (startsTurn || !current) {
      current = { prompt: startsTurn ? t.prompt_text : null, time: t.timestamp, records: [] };
      groups.push(current);
    }
    current.records.push(t);
  }
  for (const g of groups) {
    g.in = 0; g.out = 0; g.cacheRd = 0; g.billable = 0; g.tools = 0;
    for (const t of g.records) {
      g.in += t.input_tokens || 0;
      g.out += t.output_tokens || 0;
      g.cacheRd += t.cache_read_tokens || 0;
      g.billable += (t.input_tokens || 0) + (t.output_tokens || 0)
        + (t.cache_create_5m_tokens || 0) + (t.cache_create_1h_tokens || 0);
      if (t.tool_calls_json) {
        try { g.tools += JSON.parse(t.tool_calls_json).length; } catch { /* ignore */ }
      }
    }
  }
  return groups;
}

function markExpensive(groups, n) {
  [...groups].sort((a, b) => b.billable - a.billable).slice(0, n)
    .forEach(g => { if (g.billable > 0) g.hot = true; });
}

function renderTips(tips) {
  if (!tips || !tips.length) return '';
  return `
    <div class="tips-strip">
      ${tips.map(t => `
        <div class="tip">
          <div class="tip-head">💡 <strong>${fmt.htmlSafe(t.title)}</strong></div>
          <p class="tip-body">${fmt.htmlSafe(t.body)}</p>
        </div>`).join('')}
    </div>`;
}

function renderTurn(g) {
  const isSystem = g.prompt && g.prompt.startsWith('<');
  const label = g.prompt
    ? (isSystem ? '(system) ' + fmt.short(g.prompt.replace(/^<[^>]*>\s*/, ''), 90) : fmt.short(g.prompt, 110))
    : '(session preamble)';
  return `
    <details class="turn ${g.hot ? 'hot' : ''}">
      <summary>
        <span class="mono" style="font-size:11px;color:var(--muted-2)">${fmt.time(g.time)}</span>
        <span class="prompt blur-sensitive" style="${isSystem || !g.prompt ? 'color:var(--muted-2)' : ''}">${g.hot ? '⚡ ' : ''}${fmt.htmlSafe(label)}</span>
        <span class="tok">${fmt.compact(g.billable)} billable · ${fmt.compact(g.cacheRd)} cache rd${g.tools ? ` · ${g.tools} tools` : ''}</span>
      </summary>
      <table>
        <thead><tr><th>time</th><th>type</th><th>model</th><th class="blur-sensitive">prompt / tools</th><th class="num">in</th><th class="num">out</th><th class="num">cache rd</th></tr></thead>
        <tbody>
          ${g.records.map(t => {
            let tools = [];
            if (t.tool_calls_json) { try { tools = JSON.parse(t.tool_calls_json); } catch { /* ignore */ } }
            const summary = t.prompt_text ? fmt.short(t.prompt_text, 110)
              : tools.length ? tools.map(x => x.name).join(' · ')
              : '';
            return `<tr>
              <td class="mono">${fmt.time(t.timestamp, true)}</td>
              <td>${fmt.htmlSafe(t.type)}${t.is_sidechain ? ' <span class="badge">side</span>' : ''}</td>
              <td>${t.model ? `<span class="badge ${fmt.modelClass(t.model)}">${fmt.htmlSafe(fmt.modelShort(t.model))}</span>` : ''}</td>
              <td class="blur-sensitive">${fmt.htmlSafe(summary)}</td>
              <td class="num">${fmt.int(t.input_tokens)}</td>
              <td class="num">${fmt.int(t.output_tokens)}</td>
              <td class="num">${fmt.int(t.cache_read_tokens)}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </details>`;
}
