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

  root.innerHTML = `
    <div class="card">
      <h2 style="display:flex;align-items:center;margin-bottom:4px">
        <span>Session <span class="mono" style="font-weight:400;font-size:13px">${fmt.htmlSafe(id.slice(0, 8))}…</span></span>
        <span class="spacer"></span>
        <a href="#/sessions" class="muted">← all sessions</a>
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
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Turn-by-turn</h3>
      <table>
        <thead><tr><th>time</th><th>type</th><th>model</th><th class="blur-sensitive">prompt / tools</th><th class="num">in</th><th class="num">out</th><th class="num">cache rd</th></tr></thead>
        <tbody>
          ${turns.map(t => {
            const tools = t.tool_calls_json ? JSON.parse(t.tool_calls_json) : [];
            const summary = t.prompt_text ? fmt.short(t.prompt_text, 110)
              : tools.length ? tools.map(x => x.name).join(' · ')
              : '';
            return `<tr>
              <td class="mono">${(t.timestamp || '').slice(11, 19)}</td>
              <td>${t.type}${t.is_sidechain ? ' <span class="badge">side</span>' : ''}</td>
              <td>${t.model ? `<span class="badge ${fmt.modelClass(t.model)}">${fmt.htmlSafe(fmt.modelShort(t.model))}</span>` : ''}</td>
              <td class="blur-sensitive">${fmt.htmlSafe(summary)}</td>
              <td class="num">${fmt.int(t.input_tokens)}</td>
              <td class="num">${fmt.int(t.output_tokens)}</td>
              <td class="num">${fmt.int(t.cache_read_tokens)}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;
}
