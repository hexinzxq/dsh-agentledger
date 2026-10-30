const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

let state = { session: null, expanded: new Set() };

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 1800);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`${r.status} ${detail.slice(0, 200)}`);
  }
  return r.json();
}

async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("#stats").textContent =
      `${s.sessions} 会话 · ${s.observations} 观察 · ${s.summaries} 摘要 · ` +
      `${s.embeddings} 向量 · ${s.embed_model}`;
  } catch (e) {
    $("#stats").textContent = "stats 读取失败";
  }
}

async function loadSessions() {
  const { items } = await api("/api/sessions?limit=100");
  const box = $("#sessions");
  if (!items.length) {
    box.innerHTML = '<div class="empty" style="padding:12px 16px">还没有会话</div>';
    return;
  }
  box.innerHTML = items.map((s) => `
    <div class="session ${state.session === s.id ? "active" : ""}" data-sid="${s.id}">
      <div class="sid">#${s.id} · ${esc(s.harness)} · ${s.obs_count} 条</div>
      <div class="proj">${esc(s.project || s.cwd || "未命名")}</div>
      <div class="hint">${esc((s.started_at || "").replace("T", " ").slice(0, 19))}</div>
    </div>`).join("");
  box.querySelectorAll(".session").forEach((el) => {
    el.onclick = () => openSession(Number(el.dataset.sid));
  });
}

function obsCard(o, withActions = true) {
  const open = state.expanded.has(o.id) ? "open" : "";
  return `
    <div class="obs" data-oid="${o.id}">
      <div class="head">
        <span class="id">#${o.id}</span>
        <span class="badge ${esc(o.kind)}">${esc(o.kind)}</span>
        <span class="title">${esc(o.title)}</span>
      </div>
      <div class="body ${open}">${esc(o.body)}</div>
      <div class="meta">
        session ${o.session_id} · importance ${o.importance} · ${esc((o.created_at || "").slice(0, 19))}
        ${o.source_ref ? " · " + esc(o.source_ref) : ""}
      </div>
      ${withActions ? `<div class="actions">
        <button data-act="toggle">展开/收起</button>
        <button data-act="timeline">看上下文</button>
      </div>` : ""}
    </div>`;
}

function bindObsActions(root) {
  root.querySelectorAll(".obs").forEach((el) => {
    const id = Number(el.dataset.oid);
    const body = el.querySelector(".body");
    const toggle = el.querySelector('[data-act="toggle"]');
    if (toggle) toggle.onclick = () => {
      body.classList.toggle("open");
      if (body.classList.contains("open")) state.expanded.add(id);
      else state.expanded.delete(id);
    };
    const tl = el.querySelector('[data-act="timeline"]');
    if (tl) tl.onclick = () => showTimeline(id);
  });
}

async function openSession(sid) {
  state.session = sid;
  loadSessions();
  const data = await api(`/api/sessions/${sid}/observations`);
  const sum = data.summary;
  $("#content").innerHTML = `
    <h2>会话 #${sid}</h2>
    <div class="hint">${data.items.length} 条观察</div>
    ${sum ? `<div class="summary">${esc(sum.text)}</div>` : ""}
    <div style="margin:10px 0">
      <button class="primary" id="btn-sum">生成摘要</button>
    </div>
    <div id="obs-list">
      ${data.items.map((o) => obsCard(o)).join("") || '<div class="empty">这个会话没有观察</div>'}
    </div>`;
  bindObsActions($("#content"));
  const btn = $("#btn-sum");
  if (btn) btn.onclick = async () => {
    btn.disabled = true;
    try {
      const r = await api(`/api/sessions/${sid}/summarize`, { method: "POST" });
      toast("摘要已生成");
      openSession(sid);
      loadStats();
    } catch (e) { toast("失败：" + e.message); btn.disabled = false; }
  };
}

async function showTimeline(oid) {
  const { items } = await api(`/api/timeline/${oid}`);
  $("#content").innerHTML = `
    <h2>#${oid} 附近的上下文</h2>
    <div class="timeline">
      ${items.map((o) => `
        <div class="obs" data-oid="${o.id}">
          <div class="head">
            <span class="id">#${o.id}${o.id === oid ? " ←" : ""}</span>
            <span class="badge ${esc(o.kind)}">${esc(o.kind)}</span>
            <span class="title">${esc(o.title)}</span>
          </div>
          <div class="body open">${esc(o.body)}</div>
        </div>`).join("")}
    </div>`;
  bindObsActions($("#content"));
}

async function search() {
  const q = $("#q").value.trim();
  if (!q) return;
  const mode = $("#mode").value;
  try {
    const { items, mode: used } = await api(`/api/search?q=${encodeURIComponent(q)}&mode=${mode}&limit=30`);
    $("#content").innerHTML = `
      <h2>「${esc(q)}」的搜索结果</h2>
      <div class="hint">命中 ${items.length} 条 · ${used === "semantic" ? "语义" : "关键词"}</div>
      <div>${items.map((o) => obsCard(o)).join("") || '<div class="empty">没搜到</div>'}</div>`;
    bindObsActions($("#content"));
  } catch (e) { toast("搜索失败：" + e.message); }
}

$("#btn-search").onclick = search;
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
$("#btn-reindex").onclick = async () => {
  try { const r = await api("/api/reindex", { method: "POST" }); toast(`重建了 ${r.indexed} 条`); loadStats(); }
  catch (e) { toast("失败：" + e.message); }
};
$("#btn-export").onclick = () => { window.location.href = "/api/export"; };

(async function init() {
  await loadStats();
  await loadSessions();
  const { items } = await api("/api/observations?limit=20");
  if (items.length) {
    $("#content").innerHTML = `
      <h2>最近的观察</h2>
      <div>${items.map((o) => obsCard(o)).join("")}</div>`;
    bindObsActions($("#content"));
  }
})();
