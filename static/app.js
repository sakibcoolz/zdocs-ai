"use strict";

let currentRepo = null;
let currentSessionId = null;

const $ = (id) => document.getElementById(id);

async function refreshRepoList() {
  const res = await fetch("/api/repos");
  const { repos } = await res.json();

  const list = $("repo-list");
  list.innerHTML = "";
  for (const repo of repos) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = repo;
    btn.className = "repo-pick";
    if (repo === currentRepo) btn.classList.add("active");
    btn.addEventListener("click", () => selectRepo(repo));
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function selectRepo(repo) {
  currentRepo = repo;
  currentSessionId = null;
  $("chat-repo-name").textContent = `— ${repo}`;
  $("chat-log").innerHTML = "";
  $("chat-input").disabled = false;
  $("chat-send").disabled = false;
  refreshRepoList();
}

function setStatus(message, isError = false) {
  const el = $("stage-status");
  el.textContent = message;
  el.classList.toggle("error", isError);
}

async function stageFromUrl(event) {
  event.preventDefault();
  const input = $("url-input");
  const url = input.value.trim();
  if (!url) return;

  setStatus("Downloading and staging...");
  try {
    const res = await fetch("/api/repos/from-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Failed to stage repository");

    setStatus(
      body.status === "already_staged"
        ? `${body.repo} was already staged.`
        : `${body.repo} staged successfully.`
    );
    input.value = "";
    await refreshRepoList();
    selectRepo(body.repo);
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function stageFromUpload(event) {
  event.preventDefault();
  const input = $("file-input");
  const file = input.files[0];
  if (!file) return;

  setStatus("Uploading and staging...");
  try {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/repos/upload", { method: "POST", body: form });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Failed to stage repository");

    setStatus(
      body.status === "already_staged"
        ? `${body.repo} was already staged.`
        : `${body.repo} staged successfully.`
    );
    input.value = "";
    await refreshRepoList();
    selectRepo(body.repo);
  } catch (err) {
    setStatus(err.message, true);
  }
}

function appendMessage(role, text) {
  const log = $("chat-log");
  const div = document.createElement("div");
  div.className = `msg msg-${role}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function sendChat(event) {
  event.preventDefault();
  if (!currentRepo) return;

  const input = $("chat-input");
  const message = input.value.trim();
  if (!message) return;

  appendMessage("user", message);
  input.value = "";

  try {
    const res = await fetch(`/api/repos/${encodeURIComponent(currentRepo)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: currentSessionId }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Chat request failed");

    currentSessionId = body.session_id;
    appendMessage("assistant", body.reply);
  } catch (err) {
    appendMessage("assistant", `Error: ${err.message}`);
  }
}

$("url-form").addEventListener("submit", stageFromUrl);
$("upload-form").addEventListener("submit", stageFromUpload);
$("chat-form").addEventListener("submit", sendChat);

refreshRepoList();
