const params = new URLSearchParams(location.search);
const inReview = (params.get("status") || "new") === "new";

async function api(path, method = "POST", body = null) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  if (!r.ok) {
    alert(`${r.status}: ${await r.text()}`);
    throw new Error(`api ${path} -> ${r.status}`);
  }
  return r.json();
}

const rows = () => [...document.querySelectorAll("tr[data-job-id]")];
const rowOf = (el) => el.closest("tr[data-job-id]");

/* ---------- status actions ---------- */
async function setStatus(tr, status) {
  if (!tr) return;
  const variant = status === "applied" ? (tr.querySelector(".variant")?.value || "base") : null;
  await api(`/api/roles/${encodeURIComponent(tr.dataset.jobId)}/status`, "POST",
    { status, resume_variant: variant });
  const chip = tr.querySelector(".status-chip");
  if (chip) { chip.textContent = status; chip.dataset.status = status; }
  if (inReview && status !== "new") {
    const jd = tr.nextElementSibling;
    tr.classList.add("gone");
    setTimeout(() => {
      if (jd && jd.classList.contains("jd-row")) jd.remove();
      tr.remove();
      focusStep(0);
    }, 250);
  }
}

/* ---------- JD expand ---------- */
async function toggleJD(tr) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("jd-row")) { next.remove(); return; }
  const role = await api(`/api/roles/${encodeURIComponent(tr.dataset.jobId)}`, "GET");
  const d = document.createElement("tr");
  d.className = "jd-row";
  const td = document.createElement("td");
  td.colSpan = tr.children.length;
  const meta = document.createElement("div");
  meta.className = "jd-meta";
  meta.textContent = `${role.job_id} · ${role.department || "no dept"} · why: ${role.why || "-"} · caution: ${role.caution || "-"}`;
  const pre = document.createElement("pre");
  pre.textContent = role.jd_text || "(no JD text)";
  td.append(meta, pre);
  d.append(td);
  tr.after(d);
}

/* ---------- notes editing ---------- */
function editNotes(cell) {
  if (cell.querySelector("textarea")) return;
  const tr = rowOf(cell);
  const old = cell.textContent.trim() === "+" ? "" : cell.textContent.trim();
  cell.textContent = "";
  const ta = document.createElement("textarea");
  ta.value = old;
  cell.append(ta);
  ta.focus();
  let done = false;
  const finish = (text) => {
    if (done) return;
    done = true;
    cell.textContent = text || "+";
  };
  const save = async () => {
    const v = ta.value;
    await api(`/api/roles/${encodeURIComponent(tr.dataset.jobId)}/notes`, "POST", { notes: v });
    finish(v);
  };
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
    if (e.key === "Escape") finish(old);
  });
  ta.addEventListener("blur", () => { if (!done) save(); });
}

/* ---------- keyboard ---------- */
let focusIdx = -1;
function focusStep(delta) {
  const rs = rows();
  if (!rs.length) { focusIdx = -1; return; }
  focusIdx = focusIdx < 0 ? 0 : Math.max(0, Math.min(rs.length - 1, focusIdx + delta));
  rs.forEach((r) => r.classList.remove("focused"));
  rs[focusIdx].classList.add("focused");
  rs[focusIdx].scrollIntoView({ block: "nearest" });
}

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  const tr = rows()[focusIdx];
  const acts = { a: "applied", s: "skip", x: "rejected", i: "interviewing", n: "new" };
  if (e.key === "j") { e.preventDefault(); focusStep(focusIdx < 0 ? 0 : 1); }
  else if (e.key === "k") { e.preventDefault(); focusStep(-1); }
  else if (e.key === "Enter" && tr) { e.preventDefault(); toggleJD(tr); }
  else if (acts[e.key] && tr) { e.preventDefault(); setStatus(tr, acts[e.key]); }
});

/* ---------- delegated clicks / changes ---------- */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (btn) { setStatus(rowOf(btn), btn.dataset.act); return; }
  if (e.target.closest(".title-link")) { toggleJD(rowOf(e.target)); return; }
  const notes = e.target.closest(".notes-cell");
  if (notes) { editNotes(notes); return; }
  const rm = e.target.closest(".co-remove");
  if (rm) {
    api(`/api/applied-companies/${encodeURIComponent(rm.dataset.name)}`, "DELETE")
      .then(() => location.reload());
  }
});

document.addEventListener("change", (e) => {
  if (e.target.matches("select.more")) {
    const v = e.target.value;
    e.target.value = "";
    if (v) setStatus(rowOf(e.target), v);
  } else if (e.target.matches("#filters select")) {
    e.target.form.submit();
  }
});

document.querySelector("#add-co")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const inp = e.target.querySelector("input");
  const name = inp.value.trim();
  if (name) { await api("/api/applied-companies", "POST", { name }); location.reload(); }
});
