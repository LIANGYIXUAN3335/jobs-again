const BASES = ["http://127.0.0.1:8787", "http://localhost:8787"];

async function check() {
  for (const base of BASES) {
    try {
      const r = await fetch(base + "/health");
      if (r.ok) return base;
    } catch (_) { /* try next */ }
  }
  return null;
}

check().then((base) => {
  const s = document.getElementById("status");
  const hint = document.getElementById("hint");
  if (base) {
    s.innerHTML = '<span class="dot ok"></span>connected to local JobScan';
    hint.textContent = base;
  } else {
    s.innerHTML = '<span class="dot bad"></span>server not running';
    hint.innerHTML = "start it: <code>python -m app.web</code>";
  }
});
