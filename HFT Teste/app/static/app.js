const $ = (id) => document.getElementById(id);
let modelOptions = [];

// A2: token do painel. Vem do fragmento da URL (#token=...) impresso no console ao subir o app
// e fica so nesta aba (sessionStorage). O fragmento nunca e enviado ao servidor.
function panelToken() {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    sessionStorage.setItem("hft_token", decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname);
  }
  return sessionStorage.getItem("hft_token") || "";
}
const TOKEN = panelToken();

// B1: tudo que vem da API e escapado antes de ir para innerHTML
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function detailText(data) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
  if (d && typeof d === "object") return JSON.stringify(d);
  if (data && data.message) return data.message;
  return JSON.stringify(data || {});
}

async function api(path, method = "GET", body) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90000);
  try {
    const headers = { "X-Token": TOKEN };
    if (body) headers["Content-Type"] = "application/json";
    const res = await fetch(path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) throw new Error("Token do painel ausente/inválido. Abra a URL com #token=... mostrada no console do app.");
    if (!res.ok) throw new Error(detailText(data));
    return data;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("A operação demorou demais para responder.");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function fillSelect(el, values, current, extra) {
  const opts = ["", ...values.filter(Boolean)];
  if (extra && !opts.includes(extra)) opts.push(extra);
  const html = opts.map((v) => `<option value="${esc(v)}">${esc(v || "(nenhum)")}</option>`).join("");
  if (el.dataset.hash !== html) {
    el.innerHTML = html;
    el.dataset.hash = html;
  }
  if (current != null) el.value = current;
}

function editing() {
  const tag = document.activeElement?.tagName;
  return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
}

function render(s) {
  const h = s.health || {};
  $("pill-conn").textContent = s.connected ? s.status : "desconectado";
  $("pill-conn").classList.toggle("on", !!h.iq_ok);
  $("pill-acc").textContent = s.account;
  $("pill-acc").classList.toggle("real", s.account === "REAL");
  $("pill-llm").textContent = h.ollama_ok ? (s.ollama_model || "Ollama") : "Ollama offline";
  $("pill-llm").classList.toggle("on", !!h.ollama_ok);
  $("pill-ready").textContent = h.ready_to_trade ? "pronto" : "não pronto";
  $("pill-ready").classList.toggle("on", !!h.ready_to_trade);
  $("pill-ready").classList.toggle("warn", !h.ready_to_trade);
  $("real-lock").textContent = s.allow_real ? "ALLOW_REAL=1: conta real liberada no .env." : "Conta REAL bloqueada (ALLOW_REAL=0).";

  $("health-iq").textContent = h.iq_detail || "desconectado";
  $("health-iq").className = h.iq_ok ? "win" : "loss";
  $("health-llm").textContent = h.ollama_detail || "offline";
  $("health-llm").className = h.ollama_ok ? "win" : "loss";
  const mem = s.memory || {};
  $("health-mem").textContent = `${mem.total || 0} trades · wr ${((mem.win_rate || 0) * 100).toFixed(0)}%`;

  $("kpi-balance").textContent = `${s.currency} ${Number(s.balance).toFixed(2)}`;
  const pnl = Number(s.profit_today);
  $("kpi-pnl").textContent = (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + ` (${s.account} ${s.trading_date || ""})`;
  $("kpi-pnl").className = pnl >= 0 ? "win" : "loss";
  $("kpi-wl").textContent = `${s.wins} / ${s.losses}` + (s.errors ? ` · ${s.errors} erro(s)` : "");
  $("kpi-open").textContent = `${s.open_trades} ($${Number(s.open_exposure || 0).toFixed(2)})`;
  $("kpi-sig").textContent = s.last_signal || "—";
  if (!$("err").dataset.sticky) $("err").textContent = s.last_error || "";

  $("btn-practice").classList.toggle("active", s.account === "PRACTICE");
  $("btn-real").classList.toggle("active", s.account === "REAL");

  const st = s.settings || {};
  modelOptions = s.ollama_models || modelOptions;
  if (!editing()) {
    fillSelect($("model"), modelOptions, st.ollama_model, st.ollama_model);
    fillSelect($("fast-model"), modelOptions, st.ollama_fast_model || "", st.ollama_fast_model);
    $("amount").value = st.amount ?? 2;
    $("duration").value = String(st.duration_min ?? 1);
    $("payout").value = st.min_payout ?? 80;
    $("conf").value = st.min_confidence ?? 0.72;
    $("maxloss").value = st.max_daily_loss ?? 40;
    $("maxconsec").value = st.max_consecutive_losses ?? 3;
    $("assets").value = (st.assets || []).join(",");
    $("llm-timeout").value = st.llm_timeout_sec ?? 4;
    $("fallback").value = st.fallback_mode || "safe";
    $("use-llm").checked = st.use_llm !== false;
    $("fast-first").checked = !!st.use_fast_model_first;
    $("use-memory").checked = st.memory_enabled !== false;
    if (st.assets && st.assets[0] && !$("bt-asset").value) $("bt-asset").value = st.assets[0];
  }

  $("decisions").innerHTML = (s.decisions || [])
    .map((d) => {
      const cls = d.action === "aceite" ? "win" : d.action === "recusa" ? "loss" : "";
      const hour = (d.ts || "").slice(11, 19);
      return `<tr>
        <td>${esc(hour)}</td><td>${esc(d.asset)}</td><td class="${cls}">${esc(d.action)}</td>
        <td>${esc((d.ta_direction || "").toUpperCase())} ${Number(d.ta_score || 0).toFixed(2)}</td>
        <td>${esc(d.llm_outcome)}</td><td>${esc(d.used_model || (d.fallback_used ? "fallback" : ""))}</td>
        <td>${esc(d.detail)}</td>
      </tr>`;
    })
    .join("");

  $("trades").innerHTML = (s.trades || [])
    .map((t) => {
      const cls = t.result === "WIN" ? "win" : t.result === "LOSS" || t.result === "ERROR" ? "loss" : "";
      const hour = (t.opened_at || "").slice(11, 19);
      return `<tr>
        <td>${esc(hour)}</td><td>${esc(t.asset)}</td><td>${esc(String(t.direction).toUpperCase())}</td>
        <td>${esc(t.amount)}</td><td>${esc(t.source)} [${esc(t.account)}]</td>
        <td class="${cls}">${esc(t.result)}</td>
        <td class="${cls}">${Number(t.profit).toFixed(2)}</td>
      </tr>`;
    })
    .join("");

  $("log").textContent = (s.logs || [])
    .map((l) => `${(l.ts || "").slice(11, 19)} [${l.level}] ${l.message}`)
    .join("\n");

  if (s.last_backtest) $("backtest").textContent = backtestText(s.last_backtest);
}

function backtestText(b) {
  return (
    `${b.asset}: ${b.candles} velas, ${b.signals} sinais, ${b.taken} entradas, ` +
    `${b.wins}W/${b.losses}L/${b.equals || 0}E, wr ${(b.win_rate * 100).toFixed(1)}% ` +
    `(empate ${(b.breakeven_win_rate * 100).toFixed(1)}%), EV/unid ${Number(b.expected_value_per_unit || 0).toFixed(3)}, ` +
    `memória bloqueou ${b.skipped_memory}\n` +
    (b.notes || []).join("\n")
  );
}

async function refresh() {
  try {
    render(await api("/api/state"));
  } catch (e) {
    $("err").textContent = e.message;
  }
}

$("btn-connect").onclick = async () => {
  $("err").textContent = "Conectando...";
  try { render(await api("/api/connect", "POST")); $("err").textContent = ""; }
  catch (e) { $("err").textContent = e.message; }
};
$("btn-start").onclick = async () => {
  const btn = $("btn-start");
  btn.disabled = true;
  $("err").textContent = "Iniciando...";
  try { render(await api("/api/start", "POST")); $("err").textContent = ""; }
  catch (e) { $("err").textContent = e.message; }
  finally { btn.disabled = false; }
};
$("btn-stop").onclick = async () => {
  try { render(await api("/api/stop", "POST")); } catch (e) { $("err").textContent = e.message; }
};
$("btn-practice").onclick = async () => {
  try { render(await api("/api/account", "POST", { mode: "PRACTICE", confirm: "" })); }
  catch (e) { $("err").textContent = e.message; }
};
$("btn-real").onclick = async () => {
  try {
    render(await api("/api/account", "POST", { mode: "REAL", confirm: $("confirm-real").value }));
  } catch (e) { $("err").textContent = e.message; }
};
$("btn-save").onclick = async () => {
  try {
    await api("/api/settings", "POST", {
      amount: Number($("amount").value),
      duration_min: Number($("duration").value),
      min_payout: Number($("payout").value),
      min_confidence: Number($("conf").value),
      max_daily_loss: Number($("maxloss").value),
      max_consecutive_losses: Number($("maxconsec").value),
      assets: $("assets").value.split(",").map((x) => x.trim()).filter(Boolean),
      use_llm: $("use-llm").checked,
      llm_timeout_sec: Number($("llm-timeout").value),
      ollama_model: $("model").value,
      ollama_fast_model: $("fast-model").value,
      use_fast_model_first: $("fast-first").checked,
      fallback_mode: $("fallback").value,
      memory_enabled: $("use-memory").checked,
    });
    $("err").textContent = "Parâmetros salvos.";
  } catch (e) { $("err").textContent = e.message; }
};
$("btn-backtest").onclick = async () => {
  $("backtest").textContent = "Rodando...";
  try {
    const b = await api("/api/backtest", "POST", { asset: $("bt-asset").value || null });
    $("backtest").textContent = backtestText(b);
  } catch (e) {
    $("backtest").textContent = e.message;
  }
};

if (!TOKEN) $("err").textContent = "Sem token: abra a URL com #token=... mostrada no console ao iniciar o app (ou em data/panel_token.txt).";
setInterval(refresh, 1500);
refresh();
