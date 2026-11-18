// One scope of its own: pywebview injects globals (value, key, target,
// timeout, ...) into the page, which would replace functions of the same name.
(() => {
"use strict";

/* ------------------------------------------------------------------------
 * Helpers
 * --------------------------------------------------------------------- */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function call(name, ...args) {
  const fn = window.pywebview && window.pywebview.api && window.pywebview.api[name];
  if (!fn) throw new Error(`a janela não tem a função ${name}`);
  const r = await fn(...args);
  if (r && typeof r === "object" && !Array.isArray(r) && r.error) throw new Error(r.error);
  return r;
}

let toastTimer;
function toast(msg, bad = false, ms = 4200) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), bad ? ms * 2 : ms);
}

function fail(e) { toast(e.message || String(e), true); }

function dialog(title, body, buttons) {
  return new Promise((resolve) => {
    const d = $("#dialog");
    $("#dialog-title").textContent = title;
    const b = $("#dialog-body");
    b.replaceChildren(...(Array.isArray(body) ? body : [body]).map((x) => (typeof x === "string" ? h("p", { text: x }) : x)));
    const menu = $("#dialog-menu");
    menu.replaceChildren(
      ...buttons.map(([label, value, cls]) =>
        h("button", { class: "btn " + (cls || ""), value, text: label })
      )
    );
    d.onclose = () => resolve(d.returnValue);
    d.showModal();
  });
}

async function confirmAction(title, text, okLabel, danger) {
  const v = await dialog(title, text, [["Voltar", "no", "ghost"], [okLabel, "yes", danger ? "primary danger" : "primary"]]);
  return v === "yes";
}

const nf = (n, d = 0) => Number(n).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
function usd(n) {
  if (n === null || n === undefined) return "sem custo informado";
  if (n === 0) return "US$ 0";
  const d = n < 0.01 ? 4 : 2;
  return "US$ " + nf(n, d);
}
function secs(s) {
  if (s === null || s === undefined) return "";
  if (s < 60) return `${nf(s, s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  if (m < 60) return r ? `${m} min ${r} s` : `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}
function ms(v) { return v === null || v === undefined ? "" : v < 1000 ? `${v} ms` : `${nf(v / 1000, 1)} s`; }
function when(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const now = new Date();
  const hm = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const day = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return `hoje ${hm}`;
  if (diff === 1) return `ontem ${hm}`;
  return `${d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })} ${hm}`;
}
const short = (model) => (model || "").split("/").pop();

/* ------------------------------------------------------------------------
 * Config state: what is on disk, plus pending edits
 * --------------------------------------------------------------------- */

const S = {
  cfg: null,         // get_config(): {path, text, data, reference}
  pending: new Map(), // key -> {path, value, unset}
  status: { state: "loading" },
  overview: null,
  hist: null,
  histFilter: "all",
  histSel: null,
  stt: [],
  llm: [],
};

const pk = (path) => path.join("\u0001");

function diskValue(path) {
  let v = S.cfg ? S.cfg.data : undefined;
  for (const p of path) {
    if (v === undefined || v === null || typeof v !== "object") return undefined;
    v = v[p];
  }
  return v;
}

function value(path) {
  const p = S.pending.get(pk(path));
  if (p) return p.unset ? undefined : p.value;
  return diskValue(path);
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function setValue(path, v) {
  const key = pk(path);
  if (same(v, diskValue(path))) S.pending.delete(key);
  else S.pending.set(key, { path, value: v });
  pendingChanged();
}

function unsetValue(path) {
  const key = pk(path);
  if (diskValue(path) === undefined) S.pending.delete(key);
  else S.pending.set(key, { path, unset: true });
  pendingChanged();
}

function pendingChanged() {
  const n = S.pending.size;
  $("#savebar").hidden = n === 0;
  $("#savebar-text").textContent = n === 1 ? "1 alteração não salva" : `${n} alterações não salvas`;
  for (const el of $$(".field[data-path]")) el.classList.toggle("changed", S.pending.has(el.dataset.path));
  $$(".all-nav button").forEach((b) => {
    const has = [...S.pending.values()].some((p) => p.path.slice(0, -1).join(".") === b.dataset.section);
    b.querySelector(".dot").textContent = has ? "●" : "";
  });
  renderPromptPreview();
}

function ops() {
  return [...S.pending.values()].map((p) => (p.unset ? { op: "unset", path: p.path } : { op: "set", path: p.path, value: p.value }));
}

async function loadConfig() {
  S.cfg = await call("get_config");
  S.pending.clear();
  pendingChanged();
}

function refKey(section, key) {
  const s = (S.cfg.reference || []).find((x) => x.name === section);
  return s && s.keys.find((k) => k.name === key);
}

/* ------------------------------------------------------------------------
 * Fields
 * --------------------------------------------------------------------- */

function parseDefault(ref) {
  if (!ref) return undefined;
  const d = (ref.default || "").trim();
  if (ref.kind === "bool") return d === "true";
  return d;
}

// A field for the config key at `path` ([section..., key]).
function field(path, opt = {}) {
  const section = path.slice(0, -1).join(".");
  const key = path[path.length - 1];
  const ref = opt.ref || refKey(section, key) || { kind: "text", default: "", description: "" };
  const kind = opt.kind || ref.kind;
  const cur = value(path);
  const wrap = h("div", { class: "field", "data-path": pk(path) });
  const id = "f-" + pk(path).replace(/[^\w]/g, "_");
  const label = h("label", { for: id }, h("span", { text: opt.label || key }), h("span", { class: "key", text: `${section}.${key}` }));
  wrap.append(label);
  if (!opt.noHelp && (opt.help || ref.description)) wrap.append(h("p", { class: "help", text: opt.help || ref.description }));
  const control = h("div", { class: "control" });
  wrap.append(control);
  const unsetLabel = ref.default ? `padrão: ${ref.default}` : "não definido";

  if (kind === "bool") {
    const box = h("input", { type: "checkbox", id });
    box.checked = cur === undefined ? parseDefault(ref) === true : !!cur;
    box.addEventListener("change", () => setValue(path, box.checked));
    control.append(box, h("span", { class: "dflt", text: cur === undefined ? `(${unsetLabel})` : "" }));
  } else if (kind === "int" || kind === "float") {
    const inp = h("input", { type: "number", id, step: kind === "int" ? "1" : "any", placeholder: unsetLabel });
    if (ref.minimum !== null && ref.minimum !== undefined) inp.min = ref.minimum;
    if (ref.maximum !== null && ref.maximum !== undefined) inp.max = ref.maximum;
    if (cur !== undefined) inp.value = cur;
    inp.addEventListener("change", () => {
      if (inp.value === "") return unsetValue(path);
      const n = kind === "int" ? parseInt(inp.value, 10) : parseFloat(inp.value);
      if (Number.isNaN(n)) return;
      setValue(path, n);
    });
    control.append(inp);
  } else if (kind === "choice") {
    const sel = h("select", { id });
    sel.append(h("option", { value: "\u0000", text: `(${unsetLabel})` }));
    for (const c of ref.choices || opt.choices || []) sel.append(h("option", { value: c, text: c }));
    if (cur !== undefined && ![...sel.options].some((o) => o.value === cur)) sel.append(h("option", { value: cur, text: cur }));
    sel.value = cur === undefined ? "\u0000" : cur;
    sel.addEventListener("change", () => (sel.value === "\u0000" ? unsetValue(path) : setValue(path, sel.value)));
    control.append(sel);
  } else if (kind === "list") {
    const ta = h("textarea", { id, rows: Math.min(8, Math.max(3, (cur || []).length + 1)), class: "mono", placeholder: `um por linha (${unsetLabel})` });
    ta.value = (cur || []).join("\n");
    ta.addEventListener("change", () => {
      const items = ta.value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!items.length && cur === undefined) return unsetValue(path);
      setValue(path, items);
    });
    control.append(ta);
  } else if (kind === "table") {
    control.append(h("span", { class: "dflt", text: "Uma tabela: edite no Arquivo TOML." }));
  } else {
    const secret = ref.secret || opt.secret;
    const inp = h("input", { type: secret ? "password" : "text", id, placeholder: unsetLabel, autocomplete: "off", spellcheck: "false" });
    if (cur !== undefined) inp.value = typeof cur === "string" ? cur : JSON.stringify(cur);
    if (opt.list) {
      const lid = id + "-list";
      inp.setAttribute("list", lid);
      control.append(h("datalist", { id: lid }, opt.list.map(([v, l]) => h("option", { value: v, label: l || "" }))));
    }
    inp.addEventListener("change", () => (inp.value.trim() === "" ? unsetValue(path) : setValue(path, inp.value.trim())));
    control.prepend(inp);
    if (secret) {
      control.append(h("button", { class: "btn ghost small", type: "button", text: "Mostrar", onclick: (e) => {
        inp.type = inp.type === "password" ? "text" : "password";
        e.target.textContent = inp.type === "password" ? "Mostrar" : "Esconder";
      } }));
    }
  }
  if (S.pending.has(pk(path))) wrap.classList.add("changed");
  return wrap;
}

/* ------------------------------------------------------------------------
 * Navigation
 * --------------------------------------------------------------------- */

const pages = {
  painel: renderPainel,
  modelos: renderModelos,
  vocabulario: renderVocab,
  prompt: renderPrompt,
  historico: renderHist,
  recuperacao: renderRecovery,
  comandos: renderCommands,
  sons: renderSounds,
  tudo: renderAll,
  toml: renderToml,
};
let current = "painel";

function go(page) {
  current = page;
  $$(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + page));
  $("#main").scrollTop = 0;
  Promise.resolve(pages[page]()).catch(fail);
}

/* ------------------------------------------------------------------------
 * Status (polled)
 * --------------------------------------------------------------------- */

const STATE_WORD = { idle: "pronto", recording: "gravando", transcribing: "transcrevendo", synthesizing: "sintetizando", speaking: "lendo em voz alta", parado: "parado" };

async function pollStatus() {
  try {
    const st = await call("status");
    const prev = S.status.state;
    S.status = st;
    const el = $("#state");
    el.dataset.state = st.state;
    $("#state-word").textContent = STATE_WORD[st.state] || st.state;
    el.title = st.detail || "";
    if (prev !== st.state && current === "painel") renderChainLive();
    if (prev === "transcribing" && st.state === "idle" && current === "painel") renderPainel();
  } catch (e) {
    $("#state").dataset.state = "parado";
    $("#state-word").textContent = "sem resposta";
  }
}

/* ------------------------------------------------------------------------
 * Painel
 * --------------------------------------------------------------------- */

function micName(dev) {
  if (!dev || dev === "default") return "Microfone padrão";
  let s = String(dev).replace(/^alsa_input\./, "").replace(/^usb-/, "");
  s = s.split(/[-_]\d{2}\./)[0].split("-00")[0];
  const parts = s.split("_").filter((p) => p && !/^\d+$/.test(p) && !/^[0-9A-F]{4,}$/.test(p));
  // "BLUE_MICROPHONE_Blue_Snowball_SUGA": the vendor in capitals, then the name.
  const named = parts.filter((p) => p !== p.toUpperCase());
  return (named.length ? named : parts).slice(0, 3).join(" ") || dev;
}

function stage(n, what, name, detail, health, page) {
  return h("button", { class: "stage", "data-stage": n, onclick: () => go(page) },
    h("span", { class: "what", text: what }),
    h("span", { class: "name", text: name }),
    h("span", { class: "detail", text: detail }),
    h("span", { class: "meter", "aria-hidden": "true" }, h("i"), h("i"), h("i"), h("i"), h("i")),
    health ? h("span", { class: "health " + (health[0] || ""), text: health[1] }) : null,
  );
}

function renderChain() {
  const d = (p) => value(p);
  const ov = S.overview || {};
  const t = (ov.totals || {}).today || {};
  const retries = d(["general", "transcription_retries"]) || 0;
  const fb = d(["general", "transcription_fallback_model"]);
  const asrDetail = [retries ? `${retries} retries` : "sem retry", fb ? `reserva ${short(fb)}` : null].filter(Boolean).join(", ");
  let asrHealth = null;
  if (t.count) {
    const bad = t.failed;
    asrHealth = bad
      ? ["bad", `${bad} ${bad === 1 ? "falha" : "falhas"} hoje`]
      : ["ok", `${t.count} hoje${ov.latency && ov.latency.asr_ms ? `, ${ms(ov.latency.asr_ms)} típico` : ""}${t.fallbacks ? `, ${t.fallbacks} pela reserva` : ""}`];
  }
  const llmOn = d(["general", "llm_post_process"]);
  let llmHealth = null;
  const lastLlm = (ov.recent || []).find((r) => r.llm_model);
  if (!llmOn) llmHealth = [null, "desligada"];
  else if (lastLlm && !lastLlm.llm_used && lastLlm.llm_failures) llmHealth = ["bad", "falhou no último ditado"];
  else if (lastLlm) llmHealth = ["ok", `respondeu${ov.latency && ov.latency.llm_ms ? ` em ${ms(ov.latency.llm_ms)} típico` : ""}`];
  const effort = d(["llm", "reasoning_effort"]);
  const tier = d(["llm", "service_tier"]);
  const paste = d(["input", "paste"]);
  const delivery = [
    d(["input", "queue"]) ? "fila ligada" : null,
    d(["input", "soft_line_breaks"]) ? "Shift+Enter" : null,
    d(["input", "trailing_space"]) ? "espaço no fim" : null,
  ].filter(Boolean).join(", ");
  $("#chain").replaceChildren(
    stage(0, "Microfone", micName(d(["audio", "device"])), `atalho ${d(["hotkeys", "double_tap"]) ? d(["hotkeys", "double_tap"]) + " duas vezes" : d(["hotkeys", "toggle"]) || "não definido"}`, null, "tudo"),
    stage(1, "Transcrição", short(d(["asr-sidecar", "model"])) || d(["general", "backend"]) || "?", asrDetail, asrHealth, "modelos"),
    stage(2, "Limpeza", llmOn ? short(d(["llm", "model"])) || "?" : "sem limpeza", llmOn ? [effort, tier].filter(Boolean).join(", ") || "parâmetros padrão" : "o texto vai como foi transcrito", llmHealth, "prompt"),
    stage(3, "Entrega", paste ? "Cola no cursor" : "Digita no cursor", delivery || "sem ajustes", null, "tudo"),
  );
  renderChainLive();
}

function renderChainLive() {
  const st = S.status.state;
  const live = st === "recording" ? [0] : st === "transcribing" ? [1, 2] : [];
  $$("#chain .stage").forEach((s) => s.classList.toggle("live", live.includes(Number(s.dataset.stage))));
}

async function renderPainel() {
  if (!S.cfg) return;
  renderChain();
  try {
    S.overview = await call("overview");
  } catch (e) {
    fail(e);
    return;
  }
  renderChain();
  const ov = S.overview;
  const alerts = [];
  if (ov.problem) alerts.push(h("div", { class: "alert warn" }, h("p", {}, h("strong", { text: "Arquivo fora do ar" }), h("span", { class: "why", text: `${ov.problem}. Os ditados novos esperam no disco local e vão para lá quando ela voltar.` }))));
  const llmErr = ov.last_llm_error;
  const lastLlm = (ov.recent || []).find((r) => r.llm_model);
  if (llmErr && lastLlm && lastLlm.id === llmErr.id) {
    alerts.push(h("div", { class: "alert bad" },
      h("p", {}, h("strong", { text: `O Luna não respondeu ao último ditado (${when(llmErr.timestamp)})` }),
        h("span", { class: "why", text: `${llmErr.status_code ? "HTTP " + llmErr.status_code + ": " : ""}${llmErr.message || "sem detalhe"}. O texto foi colado sem limpeza.` })),
      h("button", { class: "btn", text: "Testar o LLM", onclick: () => go("modelos") })));
  }
  if ((ov.recovery || []).length) {
    const n = ov.recovery.length;
    alerts.push(h("div", { class: "alert warn" },
      h("p", {}, h("strong", { text: n === 1 ? "1 áudio esperando recuperação" : `${n} áudios esperando recuperação` }),
        h("span", { class: "why", text: "Ditados que falharam e ficaram guardados para transcrever de novo." })),
      h("button", { class: "btn", text: "Ver", onclick: () => go("recuperacao") })));
  }
  $("#alerts").replaceChildren(...alerts);
  const badge = $("#nav-recovery");
  badge.hidden = !(ov.recovery || []).length;
  badge.textContent = (ov.recovery || []).length;

  const t = ov.totals.today, all = ov.totals.all;
  const creditFig = h("div", { class: "figure" }, h("div", { class: "n", text: "…" }), h("div", { class: "l", text: "crédito no OpenRouter" }));
  $("#figures").replaceChildren(
    h("div", { class: "figure" }, h("div", { class: "n", text: nf(t.count) }), h("div", { class: "l", text: `ditados hoje, ${nf(all.count)} no total` })),
    h("div", { class: "figure" }, h("div", { class: "n", text: secs(t.seconds) || "0 s" }), h("div", { class: "l", text: "de fala hoje" })),
    h("div", { class: "figure" }, h("div", { class: "n", text: usd(t.usd) }), h("div", { class: "l", text: `gastos hoje, ${usd(all.usd)} no total` })),
    creditFig,
  );
  call("openrouter_credit").then((c) => {
    $(".n", creditFig).textContent = c.remaining === null || c.remaining === undefined ? "sem limite" : usd(c.remaining);
  }).catch(() => { $(".n", creditFig).textContent = "?"; });

  $("#recent").replaceChildren(...(ov.recent || []).map((r) => h("li", { onclick: () => { S.histSel = r.id; go("historico"); } },
    h("span", { class: "when", text: when(r.timestamp) }),
    h("span", { class: "txt", text: r.preview || (r.status === "failed" ? "falhou: " + (r.error || "") : "sem texto") }),
    h("span", { class: "tags" }, tagsFor(r)),
  )));
  if (!(ov.recent || []).length) $("#recent").replaceChildren(h("li", {}, h("span", {}), h("span", { class: "txt", text: "Nenhum ditado guardado ainda. Dite com o atalho e ele aparece aqui." })));
}

function tagsFor(r) {
  const tags = [];
  if (r.status === "failed") tags.push(h("span", { class: "tag bad", text: "falhou" }));
  if (r.fallback) tags.push(h("span", { class: "tag warn", text: short(r.asr_model) }));
  if (r.asr_failures && r.status !== "failed") tags.push(h("span", { class: "tag warn", text: `${r.asr_failures} retry` }));
  if (r.llm_model && !r.llm_used && r.status !== "failed") tags.push(h("span", { class: "tag bad", text: "sem limpeza" }));
  if (r.where === "spool") tags.push(h("span", { class: "tag", text: "no disco local" }));
  return tags;
}

/* ------------------------------------------------------------------------
 * Modelos
 * --------------------------------------------------------------------- */

async function renderModelos() {
  const sttList = () => S.stt.map((m) => [m.id, m.per_hour ? `US$ ${nf(Number(m.per_hour), 2)} por hora` : m.by_token ? "cobrado por token" : ""]);
  const draw = () => {
    $("#f-asr").replaceChildren(
      field(["general", "backend"], { label: "Serviço de transcrição", help: "asr-sidecar é o que fala com o OpenRouter (ou outro servidor no formato da OpenAI)." }),
      field(["asr-sidecar", "model"], { label: "Modelo", list: sttList(), help: "Quem transcreve cada ditado. A lista traz os modelos de voz do OpenRouter com o preço por hora de áudio; dá para digitar outro." }),
      field(["general", "transcription_retries"], { label: "Tentativas de novo", help: "Quantas vezes mandar de novo quando o provedor recusa por limite (429), fica sobrecarregado ou não responde. Espera de 1 a 5 s antes de cada uma." }),
      field(["general", "transcription_fallback_model"], { label: "Modelo reserva", list: sttList(), help: "Pedido uma vez quando as tentativas acabam. Não recebe o vocabulário; o Luna continua recebendo." }),
      field(["general", "language"], { label: "Idioma", list: [["pt", "português"], ["en", "inglês"], ["auto", "detectar"]], help: "O idioma em que você dita, ou auto." }),
      field(["asr-sidecar", "url"], { label: "Endereço", help: "O endpoint de transcrição." }),
      field(["asr-sidecar", "api_key"], { label: "Chave", secret: true, help: "A chave do OpenRouter." }),
    );
    const effort = [["none"], ["minimal"], ["low"], ["medium"], ["high"], ["xhigh"]];
    $("#f-llm").replaceChildren(
      field(["general", "llm_post_process"], { label: "Limpar cada ditado", help: "Passa cada transcrição pelo LLM, com o prompt da página Prompt do Luna, antes de colar." }),
      field(["llm", "model"], { label: "Modelo", list: S.llm.map((m) => [m]), help: "A lista vem do próprio endereço abaixo (o CLIProxy)." }),
      field(["llm", "reasoning_effort"], { label: "Raciocínio", list: effort, help: "Quanto o modelo pensa antes de responder. low é rápido e basta para limpar texto." }),
      field(["llm", "service_tier"], { label: "Prioridade", list: [["priority", "rápido"], ["default"], ["flex"], ["auto"]], help: "priority pede o modo rápido da OpenAI." }),
      field(["llm", "api_url"], { label: "Endereço", help: "O endpoint de chat completions." }),
      field(["llm", "api_key"], { label: "Chave", secret: true, help: "A chave do CLIProxy (ou do provedor)." }),
    );
  };
  draw();
  const jobs = [];
  if (!S.stt.length) jobs.push(call("stt_models").then((r) => (S.stt = r.models || [])).catch(() => {}));
  if (!S.llm.length) jobs.push(call("llm_models").then((r) => (S.llm = r.models || [])).catch(() => {}));
  call("openrouter_credit").then((c) => {
    $("#credit").textContent = c.remaining === null || c.remaining === undefined
      ? "Chave do OpenRouter sem limite de crédito."
      : `Crédito no OpenRouter: ${usd(c.remaining)} restantes de ${usd(c.limit)}. Hoje: ${usd(c.daily)}.`;
  }).catch((e) => ($("#credit").textContent = `Não consegui ver o crédito: ${e.message}`));
  if (jobs.length) { await Promise.all(jobs); if (current === "modelos") draw(); }
}

/* ------------------------------------------------------------------------
 * Vocabulário
 * --------------------------------------------------------------------- */

const VOCAB = ["general", "vocabulary"];

function vocab() { return [...(value(VOCAB) || [])]; }

function renderVocab() {
  const list = vocab();
  const disk = new Set(diskValue(VOCAB) || []);
  const filter = $("#vocab-filter").value.trim().toLowerCase();
  $("#vocab-count").textContent = `${list.length} termos`;
  $("#vocab-chips").replaceChildren(...list
    .map((t, i) => [t, i])
    .filter(([t]) => !filter || t.toLowerCase().includes(filter))
    .map(([t, i]) => h("span", { class: "chip" + (disk.has(t) ? "" : " new") }, t,
      h("button", { type: "button", title: `Remover ${t}`, "aria-label": `Remover ${t}`, text: "×", onclick: () => {
        const l = vocab(); l.splice(i, 1); setValue(VOCAB, l); renderVocab();
      } }))));
  const bulk = $("#vocab-bulk");
  if (!bulk.hidden) bulk.value = list.join("\n");
  renderRepl();
}

function renderRepl() {
  const diskR = diskValue(["replacements"]) || {};
  const rows = Object.entries(replValue());
  const body = $("#repl tbody");
  body.replaceChildren(...rows.map(([from, to]) => h("tr", {},
    h("td", {}, h("input", { type: "text", value: from, "aria-label": "Ouvido como", onchange: (e) => renameRepl(from, e.target.value.trim(), to) })),
    h("td", {}, h("input", { type: "text", value: to, "aria-label": "Escrever", onchange: (e) => setValue(["replacements", from], e.target.value) })),
    h("td", {}, h("button", { class: "btn ghost small danger", type: "button", text: "×", "aria-label": `Remover ${from}`, onclick: () => { unsetValue(["replacements", from]); renderRepl(); } })),
  )));
  if (!rows.length && !Object.keys(diskR).length) body.replaceChildren(h("tr", {}, h("td", { colspan: 3, class: "note", text: "Nenhuma substituição." })));
}

function replValue() {
  const out = { ...(diskValue(["replacements"]) || {}) };
  for (const p of S.pending.values()) {
    if (p.path[0] !== "replacements" || p.path.length !== 2) continue;
    if (p.unset) delete out[p.path[1]]; else out[p.path[1]] = p.value;
  }
  return out;
}

function renameRepl(from, to, target) {
  if (!to || to === from) return;
  unsetValue(["replacements", from]);
  setValue(["replacements", to], target);
  renderRepl();
}

/* ------------------------------------------------------------------------
 * Prompt
 * --------------------------------------------------------------------- */

const INSTR = ["general", "llm_instruction"];

function renderPrompt() {
  const ta = $("#pp-text");
  ta.value = value(INSTR) || "";
  $("#pp-on").checked = !!value(["general", "llm_post_process"]);
  renderPromptPreview();
}

function renderPromptPreview() {
  if (!S.cfg) return;
  const instr = (value(INSTR) || "").trim();
  const terms = (value(VOCAB) || []).map((t) => t.trim()).filter(Boolean);
  $("#pp-count").textContent = `${nf(instr.length)} caracteres, ${terms.length} termos anexados`;
  const pre = $("#pp-preview");
  if (!value(["general", "llm_post_process"])) {
    pre.replaceChildren("A limpeza está desligada: nada vai para o LLM e o texto é colado como foi transcrito.");
    return;
  }
  if (!instr) { pre.replaceChildren("Instrução vazia: o whisrs pula a limpeza."); return; }
  const kids = [instr];
  if (terms.length) {
    kids.push(h("span", { class: "vocab", text: "\n\nVocabulary: the speaker uses these names and terms. When the transcript holds something that sounds like one of them, write it exactly as listed. Do not add a term that was not spoken.\n" + terms.join(", ") }));
  }
  pre.replaceChildren(...kids);
}

/* ------------------------------------------------------------------------
 * Histórico
 * --------------------------------------------------------------------- */

async function renderHist() {
  $("#hist-items").replaceChildren(h("li", {}, h("span", { class: "note" }, h("span", { class: "spin" }), " carregando")));
  try { S.hist = await call("history"); } catch (e) { fail(e); return; }
  const t = S.hist.totals.all;
  $("#hist-lede").textContent = S.hist.problem
    ? S.hist.problem
    : `${nf(t.count)} ditados, ${secs(t.seconds)} de fala, ${usd(t.usd)}. ${t.failed ? `${t.failed} falharam. ` : ""}${t.fallbacks ? `${t.fallbacks} foram pela reserva.` : ""}`;
  drawHistList();
  const first = S.histSel || (S.hist.entries[0] && S.hist.entries[0].id);
  if (first) selectHist(first);
}

function histRows() {
  const q = $("#hist-search").value.trim().toLowerCase();
  return (S.hist ? S.hist.entries : []).filter((r) => {
    if (S.histFilter === "failed" && r.status !== "failed") return false;
    if (S.histFilter === "retry" && !r.asr_failures) return false;
    if (S.histFilter === "fallback" && !r.fallback) return false;
    if (S.histFilter === "llm" && !(r.llm_model && !r.llm_used)) return false;
    if (q && !((r.preview || "") + " " + (r.error || "")).toLowerCase().includes(q)) return false;
    return true;
  });
}

function drawHistList() {
  const rows = histRows();
  if (!rows.length) {
    const log = S.hist && S.hist.log;
    $("#hist-items").replaceChildren(h("li", {}, h("span", { class: "note", text: log && log.length ? "Sem arquivo; mostrando o histórico simples abaixo." : "Nada encontrado." })));
    if (log && log.length) {
      $("#hist-items").append(...log.map((e) => h("li", {}, h("span", { class: "top" }, h("span", { text: when(e.timestamp) }), h("span", { text: secs(e.duration_secs) })), h("span", { class: "txt", text: e.text }))));
    }
    return;
  }
  $("#hist-items").replaceChildren(...rows.slice(0, 600).map((r) => h("li", { "data-id": r.id, class: r.id === S.histSel ? "sel" : "", onclick: () => selectHist(r.id) },
    h("span", { class: "top" }, h("span", { text: when(r.timestamp) }), h("span", {}, h("span", { text: secs(r.duration) }))),
    h("span", { class: "txt" + (r.preview ? "" : " none"), text: r.preview || (r.status === "failed" ? `falhou: ${r.error || ""}` : "sem texto") }),
    tagsFor(r).length ? h("span", { class: "tags", style: "justify-content:flex-start" }, tagsFor(r)) : null,
  )));
}

let player = null;

async function selectHist(id) {
  S.histSel = id;
  $$("#hist-items li").forEach((li) => li.classList.toggle("sel", li.dataset.id === id));
  const box = $("#hist-detail");
  box.replaceChildren(h("p", { class: "note" }, h("span", { class: "spin" }), " abrindo"));
  let d;
  try { d = await call("entry", id); } catch (e) { box.replaceChildren(h("p", { class: "empty", text: e.message })); return; }
  if (S.histSel !== id) return;
  const e = d.entry, s = d.summary, asr = e.asr || {}, llm = e.llm || {}, cost = e.cost || {};
  const failures = [...(asr.failures || []).map((f) => ["Transcrição", f]), ...((llm.failures || []).map((f) => ["Limpeza", f]))];
  const parts = [
    h("header", { class: "page-head row", style: "margin:0" },
      h("div", {}, h("h2", { style: "font-size:22px;margin:0", text: new Date(e.timestamp).toLocaleString("pt-BR", { dateStyle: "full", timeStyle: "short" }) }),
        h("div", { class: "tags", style: "justify-content:flex-start;margin-top:6px" }, tagsFor(s).length ? tagsFor(s) : h("span", { class: "tag ok", text: "certo" }))),
      h("div", { class: "row-actions", style: "margin:0" },
        e.text ? h("button", { class: "btn", text: "Copiar texto", onclick: () => copy(e.text) }) : null,
        d.audio ? h("button", { class: "btn primary", text: "Transcrever de novo", onclick: () => retranscribe(id) }) : null)),
  ];
  if (d.audio) parts.push(h("div", { style: "margin-top:16px" }, wavePlayer(d.audio, d.peaks, e.duration_secs)));
  if (e.status === "failed") parts.push(h("h2", { text: "Erro" }), h("p", { class: "textbox raw", text: e.error || "sem detalhe" }));
  if (e.text) parts.push(h("h2", { text: s.llm_used ? "Texto colado (limpo pelo LLM)" : "Texto colado" }), h("p", { class: "textbox", text: e.text }));
  if (asr.transcript && asr.transcript !== e.text) parts.push(h("h2", { text: "Transcrição antes da limpeza" }), h("p", { class: "textbox raw", text: asr.transcript }));
  parts.push(h("dl", { class: "meta" },
    metaItem("Duração", secs(e.duration_secs)),
    metaItem("Idioma", e.language),
    metaItem("Transcreveu", short(s.asr_model)),
    metaItem("Tempo da transcrição", ms(asr.latency_ms)),
    llm.model ? metaItem("LLM", short(llm.model)) : null,
    llm.model ? metaItem("Tempo do LLM", s.llm_used ? ms(llm.latency_ms) : "não respondeu") : null,
    metaItem("Custo", usd(cost.reported_usd)),
    cost.llm && cost.llm.total_tokens ? metaItem("Tokens do LLM", nf(cost.llm.total_tokens)) : null,
    metaItem("Guardado em", s.where === "spool" ? "disco local (aguardando o NAS)" : "arquivo"),
  ));
  if (failures.length) {
    parts.push(h("h2", { text: failures.length === 1 ? "1 pedido falhou" : `${failures.length} pedidos falharam` }),
      h("ol", { class: "failures" }, failures.map(([stageName, f]) => {
        const resp = f.response;
        const msg = resp && typeof resp === "object" ? ((resp.error && (resp.error.message || resp.error)) || JSON.stringify(resp)) : resp;
        return h("li", {}, h("strong", { text: `${stageName}, ${short(f.model)}: ${f.status ? "HTTP " + f.status : "sem resposta"}` }), " ", h("span", { class: "note", text: `em ${ms(f.latency_ms)}` }), h("br"), h("code", { text: typeof msg === "string" ? msg : JSON.stringify(msg) }));
      })));
  }
  parts.push(h("details", { class: "json" }, h("summary", { text: "Registro completo (JSON)" }), h("pre", { class: "preview", text: JSON.stringify(e, null, 2) })));
  parts.push(h("div", { class: "row-actions" }, h("button", { class: "btn ghost small", text: "Abrir pasta", onclick: () => call("open_path", "archive").catch(fail) })));
  box.replaceChildren(...parts);
}

function metaItem(label, v) {
  if (v === undefined || v === null || v === "") return null;
  return h("div", {}, h("dt", { text: label }), h("dd", { text: v }));
}

async function retranscribe(id) {
  const ok = await confirmAction("Transcrever este ditado de novo?",
    "O áudio passa pelo pipeline atual (MAI, retries, reserva e limpeza pelo LLM) como um ditado novo. O texto vai para a área de transferência e para o histórico. Custa uma transcrição.",
    "Transcrever", false);
  if (!ok) return;
  toast("Transcrevendo…");
  try {
    const r = await call("retranscribe", id);
    if (!r.ok) throw new Error(r.error);
    await dialog("Texto transcrito de novo", [h("p", { class: "note", text: "Já está na área de transferência." }), h("pre", { class: "preview", text: r.text })], [["Fechar", "ok", "primary"]]);
  } catch (e) { fail(e); }
}

const PLAY = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15l13-7.5z"/></svg>';
const PAUSE = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="5.5" y="4.5" width="4.5" height="15" rx="1"/><rect x="14" y="4.5" width="4.5" height="15" rx="1"/></svg>';

function wavePlayer(src, peaks, duration) {
  if (player) { player.pause(); player = null; }
  const audio = new Audio(src);
  player = audio;
  const canvas = h("canvas", { "aria-label": "Forma de onda; clique para ir ao ponto" });
  const btn = h("button", { class: "play", type: "button", "aria-label": "Tocar", html: PLAY });
  const time = h("span", { class: "t", text: `0:00 / ${clock(duration)}` });
  const draw = () => {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, hgt = canvas.clientHeight;
    if (!w) return;
    canvas.width = w * dpr; canvas.height = hgt * dpr;
    const c = canvas.getContext("2d");
    c.scale(dpr, dpr);
    const css = getComputedStyle(document.documentElement);
    const done = css.getPropertyValue("--amber").trim(), rest = css.getPropertyValue("--line").trim();
    const p = peaks && peaks.length ? peaks : [0];
    const bars = Math.max(1, Math.floor(w / 3));
    const pos = audio.duration ? audio.currentTime / audio.duration : 0;
    for (let i = 0; i < bars; i++) {
      const v = p[Math.floor((i / bars) * p.length)] || 0;
      const bh = Math.max(2, v * (hgt - 6));
      c.fillStyle = i / bars < pos ? done : rest;
      c.fillRect(i * 3, (hgt - bh) / 2, 2, bh);
    }
    time.textContent = `${clock(audio.currentTime)} / ${clock(audio.duration || duration)}`;
  };
  let raf;
  const loop = () => { draw(); if (!audio.paused) raf = requestAnimationFrame(loop); };
  btn.addEventListener("click", () => { if (audio.paused) audio.play(); else audio.pause(); });
  audio.addEventListener("play", () => { btn.innerHTML = PAUSE; btn.setAttribute("aria-label", "Pausar"); loop(); });
  audio.addEventListener("pause", () => { btn.innerHTML = PLAY; btn.setAttribute("aria-label", "Tocar"); cancelAnimationFrame(raf); draw(); });
  audio.addEventListener("ended", () => { audio.currentTime = 0; draw(); });
  canvas.addEventListener("click", (ev) => {
    const r = canvas.getBoundingClientRect();
    if (audio.duration) { audio.currentTime = ((ev.clientX - r.left) / r.width) * audio.duration; draw(); }
  });
  requestAnimationFrame(draw);
  new ResizeObserver(draw).observe(canvas);
  return h("div", { class: "wave" }, btn, canvas, time);
}

function clock(s) {
  if (!s || !isFinite(s)) return "0:00";
  const m = Math.floor(s / 60), r = Math.floor(s % 60);
  return `${m}:${String(r).padStart(2, "0")}`;
}

async function copy(text) {
  try { await call("copy_text", text); toast("Copiado."); } catch (e) { fail(e); }
}

/* ------------------------------------------------------------------------
 * Recuperação
 * --------------------------------------------------------------------- */

async function renderRecovery() {
  let files;
  try { files = await call("recovery"); } catch (e) { fail(e); return; }
  const badge = $("#nav-recovery");
  badge.hidden = !files.length; badge.textContent = files.length;
  if (!files.length) {
    $("#rec-items").replaceChildren(h("li", {}, h("span", { class: "note", text: "Nenhum áudio esperando. Quando um ditado falha depois das tentativas, o áudio fica aqui." })));
    return;
  }
  $("#rec-items").replaceChildren(...files.map((f) => {
    const slot = h("span", { class: "slot" });
    return h("li", {},
      h("span", { class: "name" }, h("strong", { text: when(f.mtime) }), h("span", { text: `${f.name}, ${secs(f.seconds)}` })),
      slot,
      h("button", { class: "btn ghost small", text: "Ouvir", onclick: async (ev) => {
        try { const a = await call("recovery_audio", f.name); slot.replaceChildren(wavePlayer(a.audio, a.peaks, f.seconds)); ev.target.remove(); } catch (e) { fail(e); }
      } }),
      h("button", { class: "btn primary small", text: "Transcrever de novo", onclick: async (ev) => {
        const b = ev.target; b.disabled = true; b.replaceChildren(h("span", { class: "spin" }), " transcrevendo");
        try {
          const r = await call("transcribe_recovery", f.name, null);
          if (!r.ok) throw new Error(r.error);
          $("#rec-result").hidden = false; $("#rec-text").textContent = r.text;
          toast("Transcrito. O texto está na área de transferência.");
          renderRecovery();
        } catch (e) { fail(e); b.disabled = false; b.textContent = "Transcrever de novo"; }
      } }),
      h("button", { class: "btn ghost small danger", text: "Apagar", onclick: async () => {
        if (!(await confirmAction("Apagar este áudio?", `${f.name} sai da pasta de recuperação para sempre.`, "Apagar", true))) return;
        try { await call("delete_recovery", f.name); renderRecovery(); } catch (e) { fail(e); }
      } }),
    );
  }));
}

/* ------------------------------------------------------------------------
 * Comandos
 * --------------------------------------------------------------------- */

function cmdRow(label, name, cmd, desc, opt = {}) {
  return h("div", { class: "cmd" },
    h("button", { class: "btn" + (opt.danger ? " danger" : ""), text: label, onclick: () => runCommand(name, opt.extra, opt) }),
    h("code", { text: cmd }),
    h("p", { text: desc }));
}

async function runCommand(name, extra, opt = {}) {
  if (opt.confirm && !(await confirmAction(opt.confirm[0], opt.confirm[1], label(opt.confirm[2]), opt.danger))) return;
  const con = $("#console");
  con.textContent = `$ ${opt.shown || "whisrs " + name}\n…`;
  try {
    const r = await call("command", name, extra || null);
    con.textContent = `$ ${r.args.join(" ")}\n${r.out}${r.err ? "\n" + r.err : ""}${r.ok ? "" : "\n(terminou com erro)"}`;
    if (name === "restart") setTimeout(pollStatus, 1500);
  } catch (e) { con.textContent += "\n" + e.message; }
}
const label = (x) => x || "Rodar";

function renderCommands() {
  const cfg = S.cfg ? S.cfg.data : {};
  const llmCmds = cfg.llm_commands || [];
  const agents = cfg.agents || [];
  const group = (title, ...rows) => h("div", { class: "panel" }, h("h2", { text: title }), rows);
  $("#cmd-groups").replaceChildren(
    group("Ditado",
      cmdRow("Começar ou parar", "toggle", "whisrs toggle", "Como o atalho. O texto vai para a janela em foco quando ficar pronto."),
      cmdRow("Cancelar", "cancel", "whisrs cancel", "Descarta o ditado em andamento e o áudio dele."),
      cmdRow("Estado", "status", "whisrs status", "Parado, gravando ou transcrevendo, e o perfil ativo.")),
    group("Serviço",
      cmdRow("Reiniciar", "restart", "whisrs restart", "Recarrega o config.toml. Salvar por esta janela já reinicia."),
      cmdRow("Últimos textos", "log", "whisrs log -n 20", "Os 20 últimos ditados do histórico simples."),
      cmdRow("Apagar histórico", "log-clear", "whisrs log --clear", "Esvazia o histórico simples. O arquivo de ditados não muda.", { danger: true, confirm: ["Apagar o histórico simples?", "As entradas de whisrs log somem. O arquivo com áudios e respostas continua.", "Apagar"] }),
      cmdRow("Versão", "version", "whisrs --version", "A versão instalada do cliente.")),
    group("Perfis",
      cmdRow("Listar", "profile-list", "whisrs profile list", "Os perfis em profiles/ ao lado do config.toml."),
      cmdRow("Próximo", "profile-next", "whisrs profile next", "Troca para o próximo perfil e reinicia o whisrs."),
      cmdRow("Base", "profile-base", "whisrs profile base", "Volta para o config.toml sem perfil.")),
    group("Gravação de reunião",
      cmdRow("Começar", "recording-start", "whisrs recording start", "Grava o microfone e o áudio do sistema."),
      cmdRow("Parar", "recording-stop", "whisrs recording stop", "Para e manda transcrever."),
      cmdRow("Cancelar", "recording-cancel", "whisrs recording cancel", "Para e apaga a gravação.", { danger: true, confirm: ["Cancelar a gravação?", "A gravação em andamento é apagada.", "Cancelar gravação"] }),
      cmdRow("Estado", "recording-status", "whisrs recording status", "Se há uma gravação em andamento ou processando."),
      cmdRow("Listar", "recording-list", "whisrs recording list", "As gravações guardadas, mais novas primeiro.")),
    group("Agentes e comandos do LLM",
      cmdRow("Listar agentes", "agent-list", "whisrs agent", agents.length ? `${agents.length} agentes configurados.` : "Nenhum agente configurado em [[agents]]."),
      ...llmCmds.map((c) => cmdRow(c.name, "llm-command", `whisrs llm-command ${c.name}`, (c.instruction || "").slice(0, 140), { extra: [c.name], shown: `whisrs llm-command ${c.name}` })),
      llmCmds.length ? null : h("p", { class: "note", text: "Nenhum comando em [[llm_commands]]. Crie um no Arquivo TOML." })),
    h("div", { class: "panel" }, h("h2", { text: "Ferramentas" }),
      h("div", { class: "cmd" }, h("button", { class: "btn", text: "Abrir no terminal", onclick: () => call("open_terminal_config").catch(fail) }), h("code", { text: "whisrs config" }), h("p", { text: "O editor de configuração do próprio whisrs, no terminal." })),
      h("div", { class: "cmd" }, h("button", { class: "btn", text: "Pasta de configuração", onclick: () => call("open_path", "config").catch(fail) }), h("code", { text: "~/.config/whisrs" }), h("p", { text: "config.toml, sons e perfis." })),
      h("div", { class: "cmd" }, h("button", { class: "btn", text: "Pasta do arquivo", onclick: () => call("open_path", "archive").catch(fail) }), h("code", { text: (cfg.general || {}).archive_dir || "não definida" }), h("p", { text: "Áudio, respostas e custo de cada ditado." }))),
  );
}

/* ------------------------------------------------------------------------
 * Sons
 * --------------------------------------------------------------------- */

const STATE_TEXT = { file: "toca um arquivo", silent: "silencioso", tone: "tom padrão do whisrs" };
let soundPlayer = null;

async function playSource(source) {
  try {
    const a = await call("play", source);
    if (!a.audio) { toast(a.silent ? "Este som está silencioso." : "Sem áudio."); return; }
    if (soundPlayer) soundPlayer.pause();
    soundPlayer = new Audio(a.audio);
    soundPlayer.play();
  } catch (e) { fail(e); }
}

async function renderSounds() {
  let s;
  try { s = await call("sounds"); } catch (e) { fail(e); return; }
  $("#cues").replaceChildren(...s.cues.map((c) => {
    const sel = h("select", { "aria-label": `Som: ${c.label}` }, h("option", { value: "", text: "Trocar por um som do Wispr Flow…" }), s.library.map((f) => h("option", { value: f, text: f.replace(/\.wav$/, "") })));
    sel.addEventListener("change", () => sel.value && playSource("lib:" + sel.value));
    return h("li", {},
      h("span", { class: "name" }, h("strong", { text: c.label }), h("span", { text: `${c.cue}.wav, ${STATE_TEXT[c.state]}` })),
      h("button", { class: "btn ghost small", text: "Ouvir", disabled: c.state !== "file", onclick: () => playSource("cue:" + c.cue) }),
      s.library.length ? sel : null,
      s.library.length ? h("button", { class: "btn small", text: "Usar este", onclick: async () => {
        if (!sel.value) return toast("Escolha um som na lista.");
        try { await call("set_cue", c.cue, "lib:" + sel.value); toast(`${c.label}: ${sel.value}.`); renderSounds(); } catch (e) { fail(e); }
      } }) : null,
      h("button", { class: "btn ghost small", text: "Arquivo…", onclick: async () => {
        try { const p = await call("pick_wav"); if (!p.path) return; await call("set_cue", c.cue, "file:" + p.path); toast(`${c.label}: ${p.path}.`); renderSounds(); } catch (e) { fail(e); }
      } }),
      h("button", { class: "btn ghost small", text: "Silenciar", disabled: c.state === "silent", onclick: async () => {
        try { await call("set_cue", c.cue, "silent"); toast(`${c.label}: silencioso.`); renderSounds(); } catch (e) { fail(e); }
      } }),
    );
  }));
}

/* ------------------------------------------------------------------------
 * Todas as configurações
 * --------------------------------------------------------------------- */

function renderAll() {
  const ref = S.cfg.reference || [];
  const q = $("#all-search").value.trim().toLowerCase();
  const nav = [], body = [];
  for (const sec of ref) {
    const path0 = sec.name.split(".");
    let rows;
    if (sec.array) {
      const count = (diskValue([sec.name]) || []).length;
      rows = [h("p", { class: "note", style: "padding:14px 0", text: `Uma lista de entradas [[${sec.name}]]: ${count} configuradas. Edite no Arquivo TOML; as chaves de cada entrada estão abaixo.` }),
        ...sec.keys.filter((k) => matches(k, sec, q)).map((k) => h("div", { class: "field" }, h("label", {}, h("span", { text: k.name }), h("span", { class: "key", text: k.type })), h("p", { class: "help", text: k.description })))];
    } else if (sec.name === "replacements") {
      rows = [h("p", { class: "note", style: "padding:14px 0" }, "As substituições ficam em ", h("button", { class: "btn ghost small", text: "Vocabulário", onclick: () => go("vocabulario") }), ".")];
    } else {
      rows = sec.keys.filter((k) => matches(k, sec, q)).map((k) => {
        const f = field([...path0, k.name], { ref: k });
        const dflt = h("span", { class: "dflt", text: `padrão: ${k.default || "nenhum"}` });
        $("label", f).append(dflt);
        return f;
      });
    }
    if (q && !rows.length) continue;
    const id = "sec-" + sec.name.replace(/[^\w]/g, "_");
    nav.push(h("li", {}, h("button", { "data-section": sec.name, onclick: () => document.getElementById(id).scrollIntoView({ block: "start" }) }, h("span", { text: sec.array ? `[[${sec.name}]]` : `[${sec.name}]` }), h("span", { class: "dot" }))));
    body.push(h("section", { class: "all-section", id },
      h("h2", { text: sec.array ? `[[${sec.name}]]` : `[${sec.name}]` }),
      sec.guide ? h("p", { class: "guide", text: sec.guide }) : null,
      h("div", { class: "fields" }, rows)));
  }
  $("#all-nav").replaceChildren(...nav);
  $("#all-body").replaceChildren(...(body.length ? body : [h("p", { class: "empty", text: "Nenhuma chave com esse texto." })]));
  pendingChanged();
}

function matches(k, sec, q) {
  if (!q) return true;
  return `${sec.name}.${k.name} ${k.description}`.toLowerCase().includes(q);
}

/* ------------------------------------------------------------------------
 * TOML
 * --------------------------------------------------------------------- */

async function renderToml() {
  $("#toml-path").textContent = S.cfg.real_path === S.cfg.path ? S.cfg.path : `${S.cfg.path}, que aponta para ${S.cfg.real_path}`;
  const ta = $("#toml-text");
  if (!ta.dataset.dirty) ta.value = S.cfg.text;
  try {
    const list = await call("backups");
    $("#backups").replaceChildren(...(list.length ? list.map((b) => h("li", {},
      h("span", { text: new Date(b.mtime * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "medium" }) }),
      h("span", {},
        h("button", { class: "btn ghost small", text: "Ver", onclick: async () => {
          const t = await call("backup_text", b.name);
          const v = await dialog(b.name, h("pre", { class: "preview", text: t.text }), [["Fechar", "no", "ghost"], ["Pôr no editor", "use", "primary"]]);
          if (v === "use") { ta.value = t.text; ta.dataset.dirty = "1"; toast("Versão anterior no editor. Salve para usá-la."); }
        } })))) : [h("li", {}, h("span", { class: "note", text: "Nenhuma ainda." }))]));
  } catch (e) { fail(e); }
}

async function saveWith(promise, busyBtn) {
  const old = busyBtn ? busyBtn.innerHTML : null;
  if (busyBtn) { busyBtn.disabled = true; busyBtn.replaceChildren(h("span", { class: "spin" }), " salvando e reiniciando"); }
  try {
    const r = await promise;
    if (r.error) throw new Error(r.error);
    toast(r.message || "Salvo.");
    await loadConfig();
    $("#toml-text").dataset.dirty = "";
    go(current);
  } catch (e) {
    fail(e);
  } finally {
    if (busyBtn) { busyBtn.disabled = false; busyBtn.innerHTML = old; }
  }
}

/* ------------------------------------------------------------------------
 * Wiring
 * --------------------------------------------------------------------- */

function wire() {
  $$(".nav button").forEach((b) => b.addEventListener("click", () => go(b.dataset.page)));

  $("#savebar-save").addEventListener("click", (e) => saveWith(call("save_ops", ops()), e.currentTarget));
  $("#savebar-discard").addEventListener("click", () => { S.pending.clear(); pendingChanged(); go(current); });
  $("#savebar-list").addEventListener("click", () => {
    const items = [...S.pending.values()].map((p) => h("li", {}, h("code", { text: p.path.join(".") }), " ",
      p.unset ? "volta ao padrão" : typeof p.value === "string" && p.value.length > 80 ? `texto de ${nf(p.value.length)} caracteres` : Array.isArray(p.value) ? `${p.value.length} itens` : `= ${JSON.stringify(p.value)}`));
    dialog("Alterações não salvas", h("ul", {}, items), [["Fechar", "ok", "primary"]]);
  });

  $("#test-llm").addEventListener("click", async (e) => {
    const out = $("#test-llm-out");
    if (S.pending.size) out.textContent = "Testa o que está salvo, não as alterações pendentes. ";
    else out.textContent = "";
    out.append(h("span", { class: "spin" }));
    try { const r = await call("test_llm"); out.textContent = `Respondeu "${r.reply}" em ${ms(r.ms)}.`; }
    catch (err) { out.textContent = `Falhou: ${err.message}`; }
  });

  const add = $("#vocab-add");
  add.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const t = add.value.trim();
    if (!t) return;
    const l = vocab();
    if (l.includes(t)) { toast(`${t} já está na lista.`); return; }
    l.push(t); setValue(VOCAB, l); add.value = ""; renderVocab();
  });
  $("#vocab-filter").addEventListener("input", renderVocab);
  $("#vocab-bulk-toggle").addEventListener("click", (e) => {
    const bulk = $("#vocab-bulk");
    bulk.hidden = !bulk.hidden;
    $("#vocab-chips").hidden = !bulk.hidden;
    e.target.textContent = bulk.hidden ? "Editar como texto" : "Ver como lista";
    renderVocab();
  });
  $("#vocab-bulk").addEventListener("change", (e) => setValue(VOCAB, e.target.value.split("\n").map((s) => s.trim()).filter(Boolean)));
  $("#repl-add").addEventListener("click", async () => {
    const from = h("input", { type: "text", placeholder: "como sai na transcrição" });
    const to = h("input", { type: "text", placeholder: "como deve ficar" });
    const v = await dialog("Nova substituição", [h("div", { class: "fields" }, h("div", { class: "field" }, h("label", { text: "Ouvido como" }), from), h("div", { class: "field" }, h("label", { text: "Escrever" }), to))], [["Voltar", "no", "ghost"], ["Incluir", "yes", "primary"]]);
    if (v === "yes" && from.value.trim()) { setValue(["replacements", from.value.trim()], to.value); renderRepl(); }
  });

  $("#pp-text").addEventListener("input", (e) => setValue(INSTR, e.target.value));
  $("#pp-on").addEventListener("change", (e) => setValue(["general", "llm_post_process"], e.target.checked));

  $("#hist-search").addEventListener("input", drawHistList);
  $$("#hist-filter button").forEach((b) => b.addEventListener("click", () => {
    S.histFilter = b.dataset.f;
    $$("#hist-filter button").forEach((x) => x.classList.toggle("on", x === b));
    drawHistList();
  }));
  $("#hist-open").addEventListener("click", () => call("open_path", "archive").catch(fail));
  $("#rec-open").addEventListener("click", () => call("open_path", "recovery").catch(fail));
  $("#rec-copy").addEventListener("click", () => copy($("#rec-text").textContent));
  $("#snd-open").addEventListener("click", () => call("open_path", "sounds").catch(fail));
  $("#journal").addEventListener("click", async () => { try { $("#console").textContent = (await call("journal", 80)).text; } catch (e) { fail(e); } });

  $("#all-search").addEventListener("input", renderAll);

  $("#toml-text").addEventListener("input", (e) => (e.target.dataset.dirty = "1"));
  $("#toml-save").addEventListener("click", async (e) => {
    if (S.pending.size && !(await confirmAction("Salvar o arquivo como está no editor?", "As alterações pendentes das outras páginas não estão neste texto e serão descartadas.", "Salvar o arquivo"))) return;
    saveWith(call("save_text", $("#toml-text").value), e.currentTarget);
  });
  $("#toml-reload").addEventListener("click", async () => { await loadConfig(); $("#toml-text").dataset.dirty = ""; renderToml(); });
  $("#toml-open").addEventListener("click", () => call("open_path", "config").catch(fail));
  $("#toml-tui").addEventListener("click", () => call("open_terminal_config").catch(fail));

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); if (S.pending.size) $("#savebar-save").click(); }
  });
}

async function init() {
  wire();
  try { await loadConfig(); } catch (e) { fail(e); return; }
  await pollStatus();
  setInterval(pollStatus, 1500);
  setInterval(() => { if (current === "painel" && !document.hidden) renderPainel(); }, 15000);
  go("painel");
}

// In the whisrs window the API comes from pywebview. Served over HTTP by the
// development server (python -m whisrs_ui.devserver), each call is a POST.
let started = false;
function start() {
  if (started) return;
  started = true;
  init();
}
if (location.protocol.startsWith("http")) {
  window.pywebview = { api: new Proxy({}, { get: (_, name) => (...args) =>
    fetch("/api/" + String(name), { method: "POST", body: JSON.stringify(args) }).then((r) => r.json()) }) };
  start();
} else {
  // pywebview defines window.pywebview before its API is filled in, and
  // "pywebviewready" may come before or after this script: wait for a
  // function the page needs, whichever way it arrives.
  const ready = () => window.pywebview && window.pywebview.api && typeof window.pywebview.api.get_config === "function";
  window.addEventListener("pywebviewready", () => ready() && start());
  const wait = setInterval(() => { if (ready()) { clearInterval(wait); start(); } }, 50);
}

// For the developer console and page tests.
window.whisrsUi = { S, go, runCommand };
})();
