// Мелкие помощники для DOM, форматирования и всплывающих окон.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "html") el.innerHTML = value;
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else el.append(child instanceof Node ? child : String(child));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export const fmt = {
  pct(p, digits = 1) {
    return p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}%`;
  },
  signedPct(p, digits = 1) {
    if (p === null || p === undefined) return "—";
    const v = (p * 100).toFixed(digits);
    return p > 0 ? `+${v}%` : `${v}%`;
  },
  num(x, digits = 0) {
    return x === null || x === undefined ? "—" : Number(x).toLocaleString("ru-RU", {
      minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  odds(k) {
    return k ? Number(k).toFixed(2) : "—";
  },
  money(x) {
    return x === null || x === undefined ? "—" : Number(x).toLocaleString("ru-RU", {
      maximumFractionDigits: 2 });
  },
  time(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  },
  ago(ts) {
    if (!ts) return "давно";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return "только что";
    if (s < 3600) return `${Math.round(s / 60)} мин назад`;
    if (s < 86400) return `${Math.round(s / 3600)} ч назад`;
    return `${Math.round(s / 86400)} дн назад`;
  },
  roles(list) {
    const short = { CARRY: "керри", MIDLANER: "мид", OFFLANER: "оффлейн",
      SOFT_SUPPORT: "4 поз", HARD_SUPPORT: "5 поз" };
    return (list || []).map((r) => short[r] || r.toLowerCase()).join(", ");
  },
};

export function toast(message, kind = "info", ms = 4000) {
  const root = document.getElementById("toasts");
  const el = h("div", { class: `toast ${kind}` }, message);
  root.append(el);
  setTimeout(() => el.remove(), ms);
}

export function openModal(build, { wide = false, onClose } = {}) {
  const root = document.getElementById("modal-root");
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    backdrop.remove();
    document.removeEventListener("keydown", onKey);
    if (onClose) onClose();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const modal = h("div", { class: `modal${wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true" });
  const backdrop = h("div", { class: "modal-backdrop", onclose: close,
    onmousedown: (e) => { if (e.target === backdrop) close(); } }, modal);
  modal.append(build(close));
  root.append(backdrop);
  document.addEventListener("keydown", onKey);
  const focusable = modal.querySelector("[autofocus], input, select, button");
  if (focusable) setTimeout(() => focusable.focus(), 0);
  return { close, modal };
}

export function modalHead(title, close) {
  return h("div", { class: "modal-head" },
    h("h2", null, title),
    h("button", { class: "close", type: "button", "aria-label": "Закрыть", onclick: close }, "×"));
}

export function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(value));
  } catch (_) {
    return null;
  }
  return value;
}

// Картинки героев идут со Steam CDN; если он недоступен — подпись вместо картинки.
export function heroImg(hero, cls) {
  return h("img", { src: hero.img_url, alt: hero.name, loading: "lazy", class: cls,
    onerror: (e) => e.target.replaceWith(h("span", { class: `img-fallback ${cls || ""}` }, hero.name)) });
}

export function plural(n, forms) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5) return forms[1];
  if (b === 1) return forms[0];
  return forms[2];
}

export const nGames = (n) => `${n} ${plural(n, ["игра", "игры", "игр"])}`;

export function closeAllModals() {
  document.querySelectorAll("#modal-root .modal-backdrop").forEach((el) => el.dispatchEvent(new Event("close")));
}
