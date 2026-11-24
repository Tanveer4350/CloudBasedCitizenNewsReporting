const categories = [
  "Community",
  "Education",
  "Environment",
  "Health",
  "Local Government",
  "Public Safety",
  "Transport",
  "Other",
];

const views = {
  feed: document.querySelector("#feed-section"),
  submit: document.querySelector("#submit-section"),
  "my-reports": document.querySelector("#my-reports-section"),
  editor: document.querySelector("#editor-section"),
  auth: document.querySelector("#auth-section"),
};

let currentUser = null;
let nextView = "feed";

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || `Request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function setMessage(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.remove("hidden");
}

function clearMessage(element) {
  element.textContent = "";
  element.classList.add("hidden");
  element.classList.remove("error");
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
  }).format(new Date(value));
}

function updateAccount() {
  const action = document.querySelector("#account-action");
  const label = document.querySelector("#account-label");
  const editorNav = document.querySelector("#editor-nav");
  action.textContent = currentUser ? "Sign out" : "Sign in";
  label.textContent = currentUser ? currentUser.email : "";
  editorNav.classList.toggle("hidden", currentUser?.role !== "editor");
  document.querySelectorAll('[data-auth="citizen"]').forEach((item) => {
    item.classList.toggle("hidden", !currentUser || currentUser.role !== "citizen");
  });
}

async function showView(name) {
  if (name !== "feed" && name !== "auth" && !currentUser) {
    nextView = name;
    name = "auth";
  }
  if (name === "editor" && currentUser?.role !== "editor") {
    name = "feed";
  }

  Object.entries(views).forEach(([key, element]) => {
    element.classList.toggle("hidden", key !== name);
  });
  document.querySelector("#hero").classList.toggle("hidden", name !== "feed");
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.classList.toggle("active", link.dataset.view === name);
  });

  if (name === "feed") await loadFeed();
  if (name === "my-reports") await loadMyReports();
  if (name === "editor") await loadEditorQueue();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function storyCard(report) {
  const media = report.media_url
    ? report.media_type?.startsWith("video/")
      ? `<video class="story-media" controls preload="metadata" src="${escapeHtml(report.media_url)}"></video>`
      : `<img class="story-media" loading="lazy" src="${escapeHtml(report.media_url)}" alt="Photo attached to ${escapeHtml(report.title)}">`
    : "";
  return `
    <article class="story-card">
      <div class="story-meta"><span class="category-tag">${escapeHtml(report.category)}</span><span>${escapeHtml(formatDate(report.created_at))}</span></div>
      ${media}
      <h3>${escapeHtml(report.title)}</h3>
      <p>${escapeHtml(report.body)}</p>
      <div class="story-location"><span aria-hidden="true">⌖</span>${escapeHtml(report.location)}</div>
    </article>`;
}

async function loadFeed() {
  const form = document.querySelector("#filters");
  const params = new URLSearchParams(new FormData(form));
  const feed = document.querySelector("#feed");
  const empty = document.querySelector("#feed-empty");
  feed.innerHTML = '<div class="loading-note">Finding the latest community stories…</div>';
  empty.classList.add("hidden");
  try {
    const result = await api(`/api/news?${params}`);
    feed.innerHTML = result.reports.map(storyCard).join("");
    empty.classList.toggle("hidden", result.reports.length !== 0);
  } catch (error) {
    feed.innerHTML = `<p class="form-message error">${escapeHtml(error.message)}</p>`;
  }
}

function reportRow(report) {
  return `
    <article class="report-row">
      <div class="report-row-top">
        <span class="category-tag">${escapeHtml(report.category)}</span>
        <span class="status-tag status-${escapeHtml(report.status)}">${escapeHtml(report.status)}</span>
      </div>
      <h3>${escapeHtml(report.title)}</h3>
      <p>${escapeHtml(report.body)}</p>
      <div class="report-row-foot"><span>⌖ ${escapeHtml(report.location)}</span><span>Submitted ${escapeHtml(formatDate(report.created_at))}</span></div>
      ${report.editor_note ? `<div class="form-message${report.status === "rejected" ? " error" : ""}">Editor note: ${escapeHtml(report.editor_note)}</div>` : ""}
    </article>`;
}

async function loadMyReports() {
  const list = document.querySelector("#my-reports");
  list.innerHTML = '<p class="loading-note">Loading your stories…</p>';
  try {
    const result = await api("/api/my-reports");
    list.innerHTML = result.reports.length
      ? result.reports.map(reportRow).join("")
      : '<div class="empty-state"><span class="empty-icon">✳</span><h3>Your first story starts here</h3><p>Share something happening in your community.</p><button class="button button-dark" data-go="submit">Write a report</button></div>';
  } catch (error) {
    list.innerHTML = `<p class="form-message error">${escapeHtml(error.message)}</p>`;
  }
}

function editorCard(report) {
  const categoryOptions = categories.map((category) =>
    `<option${category === report.category ? " selected" : ""}>${escapeHtml(category)}</option>`
  ).join("");
  return `
    <article class="report-row editor-row">
      <div class="report-row-top"><span class="category-tag">${escapeHtml(report.category)}</span><span class="report-row-foot">${escapeHtml(formatDate(report.created_at))} · ⌖ ${escapeHtml(report.location)}</span></div>
      <h3>${escapeHtml(report.title)}</h3>
      <p>${escapeHtml(report.body)}</p>
      ${report.media_url ? `<p><a href="${escapeHtml(report.media_url)}" target="_blank" rel="noopener">Open attached ${report.media_type?.startsWith("video/") ? "video" : "photo"}</a></p>` : ""}
      <form class="editor-review" data-review-form="${escapeHtml(report.id)}">
        <label>Headline<input name="title" value="${escapeHtml(report.title)}" maxlength="120" required></label>
        <label>Location<input name="location" value="${escapeHtml(report.location)}" maxlength="120" required></label>
        <label>Category<select name="category">${categoryOptions}</select></label>
        <label>Editor note<input name="editor_note" maxlength="1000" placeholder="Optional note for the reporter"></label>
        <label class="editor-story-label">Story<textarea name="body" rows="4" maxlength="5000" required>${escapeHtml(report.body)}</textarea></label>
        <div class="editor-actions"><button class="button button-reject" name="action" value="reject" type="submit">Reject</button><button class="button button-dark" name="action" value="approve" type="submit">Approve & publish ↗</button></div>
      </form>
    </article>`;
}

async function loadEditorQueue() {
  const list = document.querySelector("#editor-queue");
  const stats = document.querySelector("#editor-stats");
  list.innerHTML = '<p class="loading-note">Loading the review queue…</p>';
  try {
    const [pending, approved, rejected] = await Promise.all([
      api("/api/editor/reports?status=pending"),
      api("/api/editor/reports?status=approved"),
      api("/api/editor/reports?status=rejected"),
    ]);
    stats.innerHTML = [
      ["pending", pending.reports.length],
      ["published", approved.reports.length],
      ["rejected", rejected.reports.length],
    ].map(([label, count]) => `<div class="editor-stat"><strong>${count}</strong><span>${label}</span></div>`).join("");
    list.innerHTML = pending.reports.length
      ? pending.reports.map(editorCard).join("")
      : '<div class="empty-state"><span class="empty-icon">✓</span><h3>You’re all caught up</h3><p>New citizen reports will appear here for review.</p></div>';
  } catch (error) {
    list.innerHTML = `<p class="form-message error">${escapeHtml(error.message)}</p>`;
  }
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.view));
});
document.querySelectorAll("[data-go]").forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.go));
});

document.querySelector("#account-action").addEventListener("click", async () => {
  if (!currentUser) {
    nextView = "feed";
    await showView("auth");
    return;
  }
  await api("/api/auth/logout", { method: "POST" });
  currentUser = null;
  updateAccount();
  await showView("feed");
});

document.querySelector("#filters").addEventListener("submit", (event) => {
  event.preventDefault();
  loadFeed();
});
document.querySelector("#filters").addEventListener("input", (event) => {
  if (event.target.matches('input[name="q"], input[name="location"]')) {
    clearTimeout(event.target.searchTimer);
    event.target.searchTimer = setTimeout(loadFeed, 350);
  }
});
document.querySelector("#filters select").addEventListener("change", loadFeed);

document.querySelectorAll("[data-auth-mode]").forEach((button) => {
  button.addEventListener("click", () => {
    const register = button.dataset.authMode === "register";
    document.querySelectorAll("[data-auth-mode]").forEach((tab) => tab.classList.toggle("active", tab === button));
    document.querySelector("#login-form").classList.toggle("hidden", register);
    document.querySelector("#register-form").classList.toggle("hidden", !register);
  });
});

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = document.querySelector("#login-message");
  clearMessage(message);
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const user = await api("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(form))),
    });
    currentUser = user.user;
    updateAccount();
    await showView(nextView);
  } catch (error) {
    setMessage(message, error.message, true);
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = document.querySelector("#register-message");
  clearMessage(message);
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const result = await api("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(form))),
    });
    currentUser = result.user;
    updateAccount();
    await showView(nextView === "feed" ? "submit" : nextView);
  } catch (error) {
    setMessage(message, error.message, true);
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#media-input").addEventListener("change", (event) => {
  const file = event.target.files[0];
  const name = document.querySelector("#upload-name");
  name.textContent = file ? `${file.name} · ${(file.size / (1024 * 1024)).toFixed(1)} MB` : "";
});

document.querySelector("#report-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = document.querySelector("#report-message");
  clearMessage(message);
  const file = form.elements.media.files[0];
  if (file && file.size > 8 * 1024 * 1024) {
    setMessage(message, "Media files must be 8 MB or smaller.", true);
    return;
  }
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const result = await api("/api/reports", { method: "POST", body: new FormData(form) });
    form.reset();
    document.querySelector("#upload-name").textContent = "";
    setMessage(message, result.message);
  } catch (error) {
    setMessage(message, error.message, true);
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#editor-queue").addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-review-form]");
  if (!form) return;
  event.preventDefault();
  const action = event.submitter?.value;
  if (!["approve", "reject"].includes(action)) return;
  const buttons = form.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  try {
    await api(`/api/editor/reports/${encodeURIComponent(form.dataset.reviewForm)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...Object.fromEntries(new FormData(form)),
        action,
      }),
    });
    await loadEditorQueue();
  } catch (error) {
    buttons.forEach((button) => { button.disabled = false; });
    let message = form.querySelector(".form-message");
    if (!message) {
      message = document.createElement("div");
      message.className = "form-message";
      form.prepend(message);
    }
    setMessage(message, error.message, true);
  }
});

document.querySelector("#refresh-editor").addEventListener("click", loadEditorQueue);

async function start() {
  try {
    const result = await api("/api/me");
    currentUser = result.user;
  } catch (error) {
    if (error.status !== 401) console.error(error);
  }
  updateAccount();
  await showView("feed");
}

start();
