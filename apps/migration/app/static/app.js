/**
 * app/static/app.js — NTT-PMO Optimization Tool frontend
 *
 * API URL mapping (matches Flask blueprints):
 *  GET  /api/items              – list work items
 *  GET  /api/schedule/          – list all schedules
 *  GET  /api/schedule/<id>      – get schedule + tasks
 *  POST /api/schedule/run       – run scheduler
 *  POST /api/push/<schedule_id> – push dates to Jira
 */

/* ── Utilities ─────────────────────────────── */

function toast(msg, level = "info", duration = 4000) {
  const c = document.getElementById("toast-container");
  if (!c) return;
  const t = document.createElement("div");
  t.className = `toast toast-${level}`;
  t.innerHTML = `<span>${msg}</span><button class="toast-close" aria-label="Close">✕</button>`;
  t.querySelector(".toast-close").addEventListener("click", () => t.remove());
  c.appendChild(t);
  if (duration > 0) setTimeout(() => t.remove(), duration);
}

async function apiFetch(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.error || data.message || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function setLoading(btn, loading) {
  if (!btn) return;
  if (loading) {
    btn.dataset.origText = btn.innerHTML;
    btn.innerHTML = `<span class="spinner"></span> ${btn.dataset.origText}`;
    btn.disabled = true;
  } else {
    btn.innerHTML = btn.dataset.origText || btn.innerHTML;
    btn.disabled = false;
  }
}

function priorityBadge(p) {
  const n = parseInt(p, 10);
  if (n === 1) return `<span class="badge badge-p1">P1</span>`;
  if (n === 2) return `<span class="badge badge-p2">P2</span>`;
  return `<span class="badge badge-p3">P${p || "—"}</span>`;
}

function statusBadge(s) {
  return `<span class="badge badge-status">${s || "—"}</span>`;
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&")
    .replace(/</g, "<")
    .replace(/>/g, ">");
}

/* ── State ─────────────────────────────────── */
let allItems = [];
let allSchedules = [];
let selectedItemIds = new Set();

/* ── Items ─────────────────────────────────── */

async function loadItems() {
  const btn = document.getElementById("btn-refresh-items");
  setLoading(btn, true);
  try {
    const data = await apiFetch("/api/items");
    allItems = data.items || data || [];
    renderItems(allItems);
  } catch (e) {
    toast(`Failed to load items: ${e.message}`, "error");
    document.getElementById("items-tbody").innerHTML =
      `<tr><td colspan="9" class="empty-row">Error loading items.</td></tr>`;
  } finally {
    setLoading(btn, false);
  }
}

function renderItems(items) {
  const tbody = document.getElementById("items-tbody");
  const count = document.getElementById("items-count");
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-row">No items found.</td></tr>`;
    count.textContent = "0 items";
    return;
  }
  tbody.innerHTML = items
    .map(
      (it) => `
    <tr>
      <td><input type="checkbox" class="item-check" data-id="${esc(it.id)}"
          ${selectedItemIds.has(String(it.id)) ? "checked" : ""}></td>
      <td>${esc(it.jira_key || it.id)}</td>
      <td title="${esc(it.summary)}">${esc((it.summary || "").slice(0, 60))}${it.summary && it.summary.length > 60 ? "…" : ""}</td>
      <td>${esc(it.item_type || "")}</td>
      <td>${esc(it.module || "")}</td>
      <td>${esc(it.stream || "")}</td>
      <td>${priorityBadge(it.priority)}</td>
      <td>${it.effort_days != null ? it.effort_days : "—"}</td>
      <td>${statusBadge(it.status)}</td>
    </tr>`
    )
    .join("");
  count.textContent = `${items.length} item${items.length !== 1 ? "s" : ""}`;

  // Re-bind checkboxes
  tbody.querySelectorAll(".item-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) selectedItemIds.add(cb.dataset.id);
      else selectedItemIds.delete(cb.dataset.id);
    });
  });
}

function applyFilters() {
  const stream = document.getElementById("filter-stream").value.toLowerCase();
  const priority = document.getElementById("filter-priority").value;
  const status = document.getElementById("filter-status").value.toLowerCase();
  const search = document.getElementById("filter-search").value.toLowerCase();
  const filtered = allItems.filter((it) => {
    if (stream && !(it.stream || "").toLowerCase().includes(stream)) return false;
    if (priority && String(it.priority) !== priority) return false;
    if (status && !(it.status || "").toLowerCase().includes(status)) return false;
    if (search) {
      const haystack = `${it.jira_key || ""} ${it.summary || ""}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
  renderItems(filtered);
}

/* ── Schedules ─────────────────────────────── */

async function loadSchedules() {
  const btn = document.getElementById("btn-refresh-schedules");
  setLoading(btn, true);
  try {
    // Backend: GET /api/schedule/  → returns array of schedule objects
    const data = await apiFetch("/api/schedule/");
    allSchedules = Array.isArray(data) ? data : data.schedules || [];
    renderSchedules(allSchedules);
  } catch (e) {
    toast(`Failed to load schedules: ${e.message}`, "error");
  } finally {
    setLoading(btn, false);
  }
}

function renderSchedules(runs) {
  const tbody = document.getElementById("schedules-tbody");
  if (!tbody) return; // not on schedules page – bail silently
  if (!runs.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-row">No runs yet.</td></tr>`;
    return;
  }
  // Backend fields: id, name, workflow_template, project_start, max_parallel, created_at, status
  tbody.innerHTML = runs
    .map(
      (r) => `
    <tr>
      <td>${esc(r.id)}</td>
      <td>${esc(r.name || "—")}</td>
      <td>${esc(r.created_at ? new Date(r.created_at).toLocaleString() : "—")}</td>
      <td>${esc(r.project_start || "—")}</td>
      <td>${r.max_parallel ?? "—"}</td>
      <td>${statusBadge(r.status)}</td>
      <td>
        <button class="btn btn-secondary btn-sm btn-view-run" data-run-id="${esc(r.id)}">View</button>
      </td>
    </tr>`
    )
    .join("");

  tbody.querySelectorAll(".btn-view-run").forEach((btn) => {
    btn.addEventListener("click", () => loadRunTasks(btn.dataset.runId));
  });
}

/* ── Run Tasks ─────────────────────────────── */

async function loadRunTasks(runId) {
  const section = document.getElementById("section-tasks");
  const activeRunSpan = document.getElementById("active-run-id");
  const pushBtn = document.getElementById("btn-push-jira");
  section.classList.remove("hidden");
  activeRunSpan.textContent = runId;
  pushBtn.dataset.runId = runId;

  const tbody = document.getElementById("tasks-tbody");
  tbody.innerHTML = `<tr><td colspan="9" class="empty-row"><span class="spinner"></span> Loading…</td></tr>`;

  // Reset gantt
  document.getElementById("gantt-container").innerHTML = "";

  try {
    // Backend: GET /api/schedule/<id>  → { ...scheduleFields, tasks: [...] }
    const data = await apiFetch(`/api/schedule/${runId}`);
    const tasks = data.tasks || [];
    renderTasksTable(tasks);
    renderGantt(tasks);
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-row">Error: ${esc(e.message)}</td></tr>`;
    toast(`Failed to load tasks: ${e.message}`, "error");
  }
}

function renderTasksTable(tasks) {
  const tbody = document.getElementById("tasks-tbody");
  if (!tasks.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-row">No tasks in this run.</td></tr>`;
    return;
  }
  // Backend task fields: item_id, jira_child_key, phase_name, required_role,
  //                      assigned_consultant_id, planned_start, planned_end,
  //                      effort_days, score, status
  tbody.innerHTML = tasks
    .map(
      (t) => `
    <tr>
      <td>${esc(t.jira_child_key || t.item_id || "")}</td>
      <td>${esc(t.phase_name || "")}</td>
      <td>${esc(t.required_role || "")}</td>
      <td>${esc(t.assigned_consultant_id || "—")}</td>
      <td>${esc(t.planned_start || "")}</td>
      <td>${esc(t.planned_end || "")}</td>
      <td>${t.effort_days != null ? t.effort_days : "—"}</td>
      <td>${t.score != null ? Number(t.score).toFixed(2) : "—"}</td>
      <td>${statusBadge(t.status)}</td>
    </tr>`
    )
    .join("");
}

/* ── Gantt ─────────────────────────────────── */

function renderGantt(tasks) {
  const container = document.getElementById("gantt-container");
  if (!tasks.length) {
    container.innerHTML = "<p style='color:var(--text-muted)'>No tasks to display.</p>";
    return;
  }

  // Backend uses planned_start / planned_end
  const dates = tasks.flatMap((t) => [t.planned_start, t.planned_end].filter(Boolean));
  if (!dates.length) {
    container.innerHTML = "<p style='color:var(--text-muted)'>Tasks have no dates.</p>";
    return;
  }

  const minDate = new Date(dates.reduce((a, b) => (a < b ? a : b)));
  const maxDate = new Date(dates.reduce((a, b) => (a > b ? a : b)));
  const totalDays = Math.max(1, (maxDate - minDate) / 86400000 + 1);

  const grid = document.createElement("div");
  grid.className = "gantt-timeline";

  // Header row
  grid.append(
    el("div", "gantt-cell header", "Item / Phase"),
    el("div", "gantt-cell header", "Consultant"),
    el("div", "gantt-cell header gantt-bar-cell", "Timeline")
  );

  tasks.forEach((t) => {
    const label = `${esc(t.jira_child_key || t.item_id || "")} — ${esc(t.phase_name || "")}`;
    const cons = esc(t.assigned_consultant_id || "—");
    const barCell = document.createElement("div");
    barCell.className = "gantt-cell gantt-bar-cell";
    barCell.style.height = "28px";

    if (t.planned_start && t.planned_end) {
      const start = new Date(t.planned_start);
      const end = new Date(t.planned_end);
      const left = ((start - minDate) / 86400000 / totalDays) * 100;
      const width = Math.max(0.5, (((end - start) / 86400000 + 1) / totalDays) * 100);

      const bar = document.createElement("div");
      bar.className = `gantt-bar ${phaseBarClass(t.phase_name)}`;
      bar.style.left = `${left}%`;
      bar.style.width = `${width}%`;
      bar.title = `${t.jira_child_key || t.item_id || ""} ${t.phase_name || ""} | ${t.planned_start} → ${t.planned_end}`;
      bar.textContent = t.phase_name || "";
      barCell.appendChild(bar);
    }

    grid.append(el("div", "gantt-cell", label), el("div", "gantt-cell", cons), barCell);
  });

  container.innerHTML = "";
  container.appendChild(grid);
}

function phaseBarClass(phase) {
  const p = (phase || "").toLowerCase();
  if (p.includes("design")) return "bar-design";
  if (p.includes("dev")) return "bar-development";
  if (p.includes("bbp")) return "bar-bbp";
  if (p.includes("test")) return "bar-test";
  if (p.includes("sign")) return "bar-sign-off";
  return "bar-default";
}

function el(tag, cls, html) {
  const d = document.createElement(tag);
  d.className = cls;
  d.innerHTML = html;
  return d;
}

/* ── Run Schedule Modal ────────────────────── */

function openModal() {
  document.getElementById("modal-schedule").classList.remove("hidden");
  document.getElementById("modal-backdrop").classList.remove("hidden");
  const sd = document.getElementById("sched-start-date");
  if (!sd.value) sd.value = new Date().toISOString().slice(0, 10);
}

function closeModal() {
  document.getElementById("modal-schedule").classList.add("hidden");
  document.getElementById("modal-backdrop").classList.add("hidden");
}

async function runSchedule() {
  const confirmBtn = document.getElementById("modal-confirm");
  const startDate = document.getElementById("sched-start-date").value;
  const workflow = document.getElementById("sched-workflow")?.value || null;
  const mode = document.getElementById("sched-mode")?.value || "dry-run";
  const selectedOnly = document.getElementById("sched-selected-only")?.checked;

  // Backend expects: name, project_start, max_parallel, workflow_template, item_ids (optional)
  const body = {
    name: `Schedule ${startDate}`,
    project_start: startDate,
    max_parallel: 3,
    mode,
  };
  if (workflow) body.workflow_template = workflow;
  if (selectedOnly && selectedItemIds.size > 0) {
    body.item_ids = Array.from(selectedItemIds);
  }

  setLoading(confirmBtn, true);
  try {
    // Backend: POST /api/schedule/run → { schedule_id, tasks_created, failed_items }
    const result = await apiFetch("/api/schedule/run", {
      method: "POST",
      body: JSON.stringify(body),
    });
    closeModal();
    const failedCount = Array.isArray(result.failed_items) ? result.failed_items.length : (result.failed_items ?? 0);
    toast(
      `Schedule #${result.schedule_id} complete — ${result.tasks_created} scheduled, ${failedCount} failed.`,
      "success",
      6000
    );
    await loadSchedules();
    if (result.schedule_id) loadRunTasks(result.schedule_id);
  } catch (e) {
    toast(`Scheduler error: ${e.message}`, "error", 8000);
  } finally {
    setLoading(confirmBtn, false);
  }
}

/* ── Push to Jira ──────────────────────────── */

async function pushToJira(runId) {
  if (!runId) {
    toast("No run selected.", "warn");
    return;
  }
  if (!confirm(`Push schedule #${runId} dates to Jira? This will update Jira issue dates.`)) return;

  const btn = document.getElementById("btn-push-jira");
  setLoading(btn, true);
  try {
    const result = await apiFetch(`/api/push/${runId}`, { method: "POST" });
    toast(`Push complete — ${result.pushed} updated, ${result.failed} failed.`, "success", 6000);
    loadRunTasks(runId);
  } catch (e) {
    toast(`Push error: ${e.message}`, "error", 8000);
  } finally {
    setLoading(btn, false);
  }
}

/* ── Import File ───────────────────────────── */

async function importFile(file, sheetName, replace) {
  const fd = new FormData();
  fd.append("file", file);
  if (sheetName) fd.append("sheet_name", sheetName);
  fd.append("replace", replace ? "true" : "false");

  const res = await fetch("/api/items/import/file", { method: "POST", body: fd });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function openImportModal() {
  const modal = document.getElementById("modal-import");
  if (modal) modal.classList.remove("hidden");
  document.getElementById("modal-backdrop")?.classList.remove("hidden");
}

function closeImportModal() {
  const modal = document.getElementById("modal-import");
  if (modal) modal.classList.add("hidden");
  // Only hide backdrop if schedule modal also hidden
  if (document.getElementById("modal-schedule")?.classList.contains("hidden")) {
    document.getElementById("modal-backdrop")?.classList.add("hidden");
  }
}

async function confirmImport() {
  const fileInput = document.getElementById("import-file-input");
  const sheetInput = document.getElementById("import-sheet-name");
  const replaceChk = document.getElementById("import-replace");
  const confirmBtn = document.getElementById("import-confirm");

  if (!fileInput?.files?.length) {
    toast("Select a file first.", "warn");
    return;
  }
  const file = fileInput.files[0];
  const sheetName = sheetInput?.value.trim() || "";
  const replace = replaceChk?.checked ?? false;

  setLoading(confirmBtn, true);
  try {
    const result = await importFile(file, sheetName, replace);
    toast(`Imported ${result.imported} item(s).`, "success", 5000);
    closeImportModal();
    await loadItems();
  } catch (e) {
    toast(`Import failed: ${e.message}`, "error", 8000);
  } finally {
    setLoading(confirmBtn, false);
  }
}

function exportItems() {
  // Build query string from active filters
  const params = new URLSearchParams();
  const stream = document.getElementById("filter-stream")?.value;
  const status = document.getElementById("filter-status")?.value;
  if (stream) params.set("stream", stream);
  if (status) params.set("status", status);
  const url = `${window.APP_PREFIX || ""}/api/items/export${params.toString() ? "?" + params.toString() : ""}`;
  window.location.assign(url);
}

/* ── Select-all checkbox ───────────────────── */

function initSelectAll() {
  const selectAll = document.getElementById("select-all-items");
  if (!selectAll) return;
  selectAll.addEventListener("change", () => {
    document.querySelectorAll(".item-check").forEach((cb) => {
      cb.checked = selectAll.checked;
      if (selectAll.checked) selectedItemIds.add(cb.dataset.id);
      else selectedItemIds.delete(cb.dataset.id);
    });
  });
}

/* ── View toggle (Table / Gantt) ───────────── */

function initViewToggle() {
  document.querySelectorAll(".toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".toggle-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const view = btn.dataset.view;
      document.getElementById("tasks-table-view").classList.toggle("hidden", view !== "table");
      document.getElementById("tasks-gantt-view").classList.toggle("hidden", view !== "gantt");
    });
  });
}

/* ── Init ──────────────────────────────────── */

document.addEventListener("DOMContentLoaded", () => {
  // Filter listeners
  ["filter-stream", "filter-priority", "filter-status"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", applyFilters);
  });
  document.getElementById("filter-search")?.addEventListener("input", applyFilters);

  // Import / Export buttons
  document.getElementById("btn-import-file")?.addEventListener("click", openImportModal);
  document.getElementById("btn-export-items")?.addEventListener("click", exportItems);
  document.getElementById("import-cancel")?.addEventListener("click", closeImportModal);
  document.getElementById("import-confirm")?.addEventListener("click", confirmImport);

  // Buttons
  document.getElementById("btn-refresh-items")?.addEventListener("click", loadItems);
  document.getElementById("btn-refresh-schedules")?.addEventListener("click", loadSchedules);
  document.getElementById("btn-run-schedule")?.addEventListener("click", openModal);
  document.getElementById("modal-cancel")?.addEventListener("click", closeModal);
  document.getElementById("modal-backdrop")?.addEventListener("click", closeModal);
  document.getElementById("modal-confirm")?.addEventListener("click", runSchedule);
  document.getElementById("btn-close-tasks")?.addEventListener("click", () => {
    document.getElementById("section-tasks").classList.add("hidden");
  });
  document.getElementById("btn-push-jira")?.addEventListener("click", (e) => {
    pushToJira(e.currentTarget.dataset.runId);
  });

  // Keyboard: Escape closes modal
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  initSelectAll();
  initViewToggle();

  // Initial data load – only run on pages that have the relevant DOM elements
  if (document.getElementById("select-all-items")) loadItems();
  if (document.getElementById("schedules-tbody")) loadSchedules();
});
