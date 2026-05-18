// ─── CONFIG ──────────────────────────────────────────────────
const REFRESH_INTERVAL_MS = 6000; // how often to refresh all data

// ─── STATE ───────────────────────────────────────────────────
let pendingAction = null; // 'shutdown' | 'reboot'

// ─── INIT ────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  // First load immediately, then poll
  refreshAll();
  setInterval(refreshAll, REFRESH_INTERVAL_MS);

  // Warm up CPU percent (psutil needs one prior call)
  fetch("/api/hardware").catch(() => {});
});

async function refreshAll() {
  await Promise.allSettled([
    loadDisks(),
    loadHardware(),
  ]);
}

// ─── STATUS INDICATOR ────────────────────────────────────────
function setStatus(state) {
  const dot   = document.getElementById("statusDot");
  const label = document.getElementById("statusLabel");
  dot.className = "status-dot " + state;
  if (state === "online")       label.textContent = "online";
  else if (state === "error")   label.textContent = "fehler";
  else                          label.textContent = "verbinde…";
}

function updateTimestamp() {
  const el = document.getElementById("lastUpdate");
  const now = new Date();
  el.textContent = now.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ─── HELPER: FORMAT BYTES ─────────────────────────────────────
function fmtBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(1)} ${units[i]}`;
}

// ─── HELPER: BAR COLOR CLASS ──────────────────────────────────
function barClass(pct) {
  if (pct < 70) return "";
  if (pct < 85) return "mid";
  return "high";
}

function pctClass(pct) {
  if (pct < 70) return "pct-low";
  if (pct < 85) return "pct-mid";
  return "pct-high";
}

// ─── DISKS ───────────────────────────────────────────────────
async function loadDisks() {
  const container = document.getElementById("volumeList");
  try {
    const res = await fetch("/api/disks");
    if (!res.ok) throw new Error(res.statusText);
    const disks = await res.json();
    setStatus("online");
    updateTimestamp();

    if (!disks.length) {
      container.innerHTML = `<div class="error-msg">Keine Volumes gefunden</div>`;
      return;
    }

    container.innerHTML = disks.map(d => {
      const pct = Math.round(d.percent);
      const cls = barClass(pct);
      const pCls = pctClass(pct);
      return `
        <div class="volume-item">
          <div class="volume-label-row">
            <span class="volume-mount">${d.mountpoint}</span>
            <span class="volume-percent ${pCls}">${pct}%</span>
          </div>
          <div class="bar-track">
            <div class="bar-fill ${cls}" style="width: ${pct}%"></div>
          </div>
          <div class="volume-meta">
            ${fmtBytes(d.used)} von ${fmtBytes(d.total)} belegt
            &nbsp;·&nbsp; ${d.fstype}
            &nbsp;·&nbsp; ${d.device}
          </div>
        </div>`;
    }).join("");

  } catch (e) {
    setStatus("error");
    container.innerHTML = `<div class="error-msg">Fehler beim Laden: ${e.message}</div>`;
  }
}

// ─── HARDWARE ────────────────────────────────────────────────
async function loadHardware() {
  try {
    const res = await fetch("/api/hardware");
    if (!res.ok) throw new Error(res.statusText);
    const hw = await res.json();

    // RAM
    const ramPct = Math.round(hw.ram.percent);
    document.getElementById("ramPercent").textContent = `${ramPct}%`;
    document.getElementById("ramPercent").className = `hw-value ${pctClass(ramPct)}`;
    const ramBar = document.getElementById("ramBar");
    ramBar.style.width = `${ramPct}%`;
    ramBar.className = `bar-fill ${barClass(ramPct)}`;
    document.getElementById("ramSub").textContent =
      `${fmtBytes(hw.ram.used)} von ${fmtBytes(hw.ram.total)} belegt`;

    // CPU
    const cpuPct = Math.round(hw.cpu_percent);
    document.getElementById("cpuPercent").textContent = `${cpuPct}%`;
    document.getElementById("cpuPercent").className = `hw-value ${pctClass(cpuPct)}`;
    const cpuBar = document.getElementById("cpuBar");
    cpuBar.style.width = `${cpuPct}%`;
    cpuBar.className = `bar-fill ${barClass(cpuPct)}`;

    // Temperatures
    renderTemps(hw.temperatures);

  } catch (e) {
    setStatus("error");
    document.getElementById("tempsList").innerHTML =
      `<div class="error-msg">Fehler: ${e.message}</div>`;
  }
}

function renderTemps(temps) {
  const list = document.getElementById("tempsList");

  if (!temps || Object.keys(temps).length === 0) {
    list.innerHTML = `<div class="error-msg">Keine Sensoren gefunden<br><small style="color:var(--text-muted)">ggf. lm-sensors installieren: sudo apt install lm-sensors</small></div>`;
    return;
  }

  const rows = [];
  for (const [chip, sensors] of Object.entries(temps)) {
    for (const s of sensors) {
      const t = s.current;
      let cls = "cool";
      if (t >= 70) cls = "hot";
      else if (t >= 50) cls = "warm";

      rows.push(`
        <div class="temp-row">
          <span class="temp-chip">${chip}</span>
          <span class="temp-label">${s.label}</span>
          <span class="temp-value ${cls}">${t.toFixed(1)} °C</span>
        </div>`);
    }
  }

  list.innerHTML = rows.join("");
}

// ─── SYSTEM ACTIONS ──────────────────────────────────────────
const ACTIONS = {
  shutdown: {
    title:  "Wirklich herunterfahren?",
    desc:   "Der Server wird sofort heruntergefahren. Alle laufenden Dienste werden beendet.",
    icon:   "⏻",
    label:  "Herunterfahren",
    endpoint: "/api/system/shutdown",
  },
  reboot: {
    title:  "Wirklich neu starten?",
    desc:   "Der Server wird sofort neu gestartet. Das dauert in der Regel 1–2 Minuten.",
    icon:   "↺",
    label:  "Neu starten",
    endpoint: "/api/system/reboot",
  },
};

function confirmAction(action) {
  const cfg = ACTIONS[action];
  if (!cfg) return;
  pendingAction = action;

  document.getElementById("modalIcon").textContent = cfg.icon;
  document.getElementById("modalTitle").textContent = cfg.title;
  document.getElementById("modalDesc").textContent  = cfg.desc;

  const btn = document.getElementById("modalConfirmBtn");
  btn.textContent = cfg.label;
  // reboot button is warn color
  btn.className = action === "reboot" ? "btn btn-warn" : "btn btn-danger";

  document.getElementById("modalBackdrop").classList.add("open");
}

function closeModal() {
  document.getElementById("modalBackdrop").classList.remove("open");
  pendingAction = null;
}

async function executeAction() {
  const action = pendingAction;
  if (!action) return;
  closeModal();

  const cfg = ACTIONS[action];
  try {
    const res = await fetch(cfg.endpoint, { method: "POST" });
    const data = await res.json();
    if (data.status === "ok") {
      setStatus("error"); // server is going down
      document.getElementById("statusLabel").textContent =
        action === "shutdown" ? "wird gestoppt…" : "wird neugestartet…";
    } else {
      alert("Fehler: " + (data.detail || data.message));
    }
  } catch (e) {
    alert("Anfrage fehlgeschlagen: " + e.message);
  }
}

// Close modal on Escape key
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeModal();
});
