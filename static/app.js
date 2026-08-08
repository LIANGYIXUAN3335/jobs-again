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
async function setStatus(tr, status, scrollAfter = true) {
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
      // Re-anchor the keyboard cursor onto whatever row slid up into this slot — but ONLY
      // if a cursor exists. focusStep() treats focusIdx < 0 as "start at row 0", so calling
      // it unconditionally made every MOUSE click scroll the page back to the top: a mouse
      // user never presses j/k, so focusIdx is always -1.
      if (focusIdx >= 0) focusStep(0, scrollAfter);
    }, 250);
  }
}

/* ---------- queue for tailoring ---------- */
async function queueTailoring(tr) {
  if (!tr) return;
  const res = await api(`/api/roles/${encodeURIComponent(tr.dataset.jobId)}/queue-tailoring`,
    "POST", { angle: null });
  const chip = tr.querySelector(".chip.tailor");
  if (!chip) {
    const span = document.createElement("span");
    span.className = "chip tailor";
    span.textContent = "queued";
    tr.querySelector(".title .apply").before(span, document.createTextNode(" "));
  }
  alert(`Prompt written:\n${res.prompt_path}\n\nOpen it in VS Code, generate with Claude Code, save the YAML, then click "ingest résumés".`);
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
function focusStep(delta, scroll = true) {
  const rs = rows();
  if (!rs.length) { focusIdx = -1; return; }
  focusIdx = focusIdx < 0 ? 0 : Math.max(0, Math.min(rs.length - 1, focusIdx + delta));
  rs.forEach((r) => r.classList.remove("focused"));
  rs[focusIdx].classList.add("focused");
  // Rows run 250-360px tall, so scrolling the replacement row into view after a MOUSE click
  // yanks the page a few hundred px out from under the pointer. Keyboard nav still scrolls —
  // there the cursor is the only thing telling you where you are.
  if (scroll) rs[focusIdx].scrollIntoView({ block: "nearest" });
}

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select")) return;
  const tr = rows()[focusIdx];
  const acts = { a: "applied", s: "skip", x: "rejected", i: "interviewing", n: "new" };
  if (e.key === "j") { e.preventDefault(); focusStep(focusIdx < 0 ? 0 : 1); }
  else if (e.key === "k") { e.preventDefault(); focusStep(-1); }
  else if (e.key === "Enter" && tr) { e.preventDefault(); toggleJD(tr); }
  else if (e.key === "t" && tr) { e.preventDefault(); queueTailoring(tr); }
  else if (acts[e.key] && tr) { e.preventDefault(); setStatus(tr, acts[e.key]); }
});

/* ---------- delegated clicks / changes ---------- */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (btn) {
    // Mouse and keyboard share ONE cursor: clicking a row's button moves the cursor there,
    // so the re-anchor after the row disappears lands on its replacement — already under the
    // pointer, so scrollIntoView({block:"nearest"}) has nothing to scroll. Without this the
    // cursor would still be wherever j/k last left it and the page would jump back to it.
    if (btn.dataset.act === "queue") queueTailoring(rowOf(btn));
    else {
      focusIdx = rows().indexOf(rowOf(btn));
      setStatus(rowOf(btn), btn.dataset.act, false);   // false → don't scroll; the pointer is here
    }
    return;
  }
  if (e.target.closest("#ingest-btn")) {
    const b = e.target.closest("#ingest-btn");
    b.disabled = true; b.textContent = "ingesting…";
    api("/api/ingest", "POST").then((r) => {
      b.disabled = false; b.textContent = "ingest résumés";
      alert(r.ingested ? `Rendered ${r.ingested} résumé PDF(s). Reloading.` : "Nothing new in output/resumes/.");
      if (r.ingested) location.reload();
    }).catch(() => { b.disabled = false; b.textContent = "ingest résumés"; });
    return;
  }
  // ↗ opens the application page. Record WHICH role first: for LinkedIn-sourced roles the
  // apply_url is the LinkedIn page, and clicking Apply there hands you off to the employer's
  // own ATS whose URL carries no id we store — so the extension's URL lookup can never join
  // back to li:<id>. The pin is that join, and it expires (store.PIN_TTL_MIN) so it can't
  // attach a stale résumé later. Fire-and-forget: the link must open even if JobScan is down.
  const apply = e.target.closest("a.apply");
  if (apply) {
    const tr = rowOf(apply);
    // raw fetch, not api(): api() alerts on failure, and a pin that didn't stick must never
    // interrupt the click — the worst case is the extension falls back to what it does today.
    if (tr) {
      fetch("/api/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: tr.dataset.jobId }),
        keepalive: true,          // the tab may navigate away before this settles
      }).catch(() => {});
    }
    return;                       // let the browser follow the href normally
  }
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
    if (v) {                                   // also a pointer path — same no-scroll rule
      focusIdx = rows().indexOf(rowOf(e.target));
      setStatus(rowOf(e.target), v, false);
    }
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
