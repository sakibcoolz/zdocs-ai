"use strict";

/**
 * Analysis panel: drives the Repository Operations Agent's HTTP API.
 *
 * Deliberately separate from app.js (staging + chat) — the two panels share
 * only the "repo-selected" event, so neither can break the other.
 *
 * Mermaid is loaded from a CDN and rendered progressively. If it does not load
 * (offline, blocked, no network), the diagram source is shown instead: the
 * .mmd files are already written to generated-docs/ either way, so a missing
 * renderer costs presentation, never results.
 */

(() => {
  const el = (id) => document.getElementById(id);
  let repo = null;
  let mermaidReady = null;

  // -- small DOM helpers ----------------------------------------------------

  function node(tag, className, text) {
    const created = document.createElement(tag);
    if (className) created.className = className;
    if (text !== undefined) created.textContent = text;
    return created;
  }

  function table(headers, rows) {
    const wrapper = node("div", "table-wrap");
    const created = node("table");
    const head = node("thead");
    const headRow = node("tr");
    for (const header of headers) headRow.appendChild(node("th", null, header));
    head.appendChild(headRow);
    created.appendChild(head);

    const body = node("tbody");
    for (const row of rows) {
      const tr = node("tr");
      for (const cell of row) {
        const td = node("td");
        if (cell instanceof Node) td.appendChild(cell);
        else td.textContent = cell === null || cell === undefined ? "—" : String(cell);
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
    created.appendChild(body);
    wrapper.appendChild(created);
    return wrapper;
  }

  function badge(text, kind) {
    return node("span", `badge badge-${kind || "neutral"}`, text);
  }

  function confidenceBadge(level) {
    return badge(level, { high: "good", medium: "warn", low: "bad" }[level] || "neutral");
  }

  function card(title) {
    const section = node("section", "card");
    section.appendChild(node("h3", null, title));
    return section;
  }

  function setStatus(message, isError = false) {
    const status = el("analysis-status");
    status.textContent = message;
    status.classList.toggle("error", Boolean(isError));
  }

  function clearOutput() {
    el("analysis-output").innerHTML = "";
  }

  async function getJson(path, options) {
    const response = await fetch(path, options);
    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
    return body;
  }

  function busy(isBusy) {
    for (const id of ["btn-inventory", "btn-oop", "btn-diagrams", "btn-tools"]) {
      el(id).disabled = isBusy || (id !== "btn-tools" && !repo);
    }
  }

  // -- renderers ------------------------------------------------------------

  function renderInventory(data) {
    clearOutput();
    const output = el("analysis-output");

    const counts = card("Files and directories");
    counts.appendChild(
      table(
        ["Files", "Directories", "Total bytes"],
        [[data.counts.file_count, data.counts.directory_count, data.counts.total_bytes.toLocaleString()]]
      )
    );
    output.appendChild(counts);

    const languages = card("Languages");
    const rows = (data.languages.languages || []).map((entry) => [
      entry.language,
      entry.files,
      `${entry.percent_of_classified_files}%`,
      entry.supported ? badge("analyzed", "good") : badge("counted only", "neutral"),
    ]);
    languages.appendChild(table(["Language", "Files", "Share", "Support"], rows));
    if (data.languages.unclassified_files) {
      languages.appendChild(
        node("p", "hint", `${data.languages.unclassified_files} file(s) not classified.`)
      );
    }
    output.appendChild(languages);

    const git = card("Git");
    if (data.git.is_git_repository) {
      git.appendChild(
        table(
          ["Branch", "HEAD", "Commits", "Tracked files"],
          [[data.git.branch, (data.git.head_commit || "").slice(0, 10), data.git.commit_count, data.git.tracked_file_count]]
        )
      );
    } else {
      git.appendChild(node("p", "hint", data.git.reason || "Not a git repository."));
    }
    output.appendChild(git);
  }

  function renderOop(result) {
    clearOutput();
    const output = el("analysis-output");
    const summary = result.data.summary || {};

    const overview = card("Overview");
    overview.appendChild(
      table(
        ["Files analyzed", "Symbols", "Relationships", "Languages"],
        [[summary.files_analyzed, summary.symbol_count, summary.relationship_count, (result.data.languages || []).join(", ") || "—"]]
      )
    );
    output.appendChild(overview);

    const kinds = card("Symbols by kind");
    kinds.appendChild(
      table(["Kind", "Count"], Object.entries(summary.symbols_by_kind || {}))
    );
    output.appendChild(kinds);

    const relations = card("Relationships by type");
    relations.appendChild(
      table(["Relationship", "Count"], Object.entries(summary.relationships_by_type || {}))
    );
    output.appendChild(relations);

    const polymorphism = Object.entries(result.data.polymorphism || {});
    const poly = card("Polymorphic abstractions");
    if (polymorphism.length) {
      poly.appendChild(
        table(
          ["Abstraction", "Implementations"],
          polymorphism.map(([name, implementers]) => [name, implementers.join(", ")])
        )
      );
    } else {
      poly.appendChild(node("p", "hint", "No interface or abstract-base implementations detected."));
    }
    output.appendChild(poly);

    const encapsulation = result.data.encapsulation || {};
    const enc = card("Encapsulation");
    enc.appendChild(
      table(["Visibility", "Members"], Object.entries(encapsulation.members_by_visibility || {}))
    );
    if (encapsulation.public_field_count) {
      enc.appendChild(
        node("p", "hint", `${encapsulation.public_field_count} public field(s), e.g. ${(encapsulation.public_field_examples || []).slice(0, 5).join(", ")}`)
      );
    }
    output.appendChild(enc);

    const findings = card("Findings");
    const rows = (result.matches || []).slice(0, 60).map((match) => [
      match.symbol,
      match.relationship ? `${match.relationship} → ${match.target_symbol}` : match.symbol_type,
      `${match.file_path}:${match.line ?? "?"}`,
      match.detection_method,
      confidenceBadge(match.confidence),
    ]);
    findings.appendChild(
      table(["Symbol", "What", "Evidence", "Detected by", "Confidence"], rows)
    );
    if ((result.matches || []).length > 60) {
      findings.appendChild(
        node("p", "hint", `Showing 60 of ${result.matches.length} findings. Use the API for the full set.`)
      );
    }
    output.appendChild(findings);

    renderWarnings(output, result.warnings, result.truncated);
  }

  function renderWarnings(output, warnings, truncated) {
    const notes = (warnings || []).slice();
    if (truncated) notes.push("Results were truncated by a policy limit; this is a partial answer.");
    if (!notes.length) return;
    const section = card("Limitations of this run");
    const list = node("ul", "notes");
    for (const note of notes) list.appendChild(node("li", null, note));
    section.appendChild(list);
    output.appendChild(section);
  }

  // -- diagrams -------------------------------------------------------------

  function loadMermaid() {
    if (mermaidReady) return mermaidReady;
    mermaidReady = import("https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs")
      .then((module) => {
        module.default.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
        return module.default;
      })
      .catch(() => null);
    return mermaidReady;
  }

  async function renderDiagrams(result) {
    clearOutput();
    const output = el("analysis-output");
    const mermaid = await loadMermaid();

    if (!mermaid) {
      output.appendChild(
        node(
          "p",
          "hint",
          "The Mermaid renderer could not be loaded (offline or blocked). " +
            "Diagram source is shown below and has been written to generated-docs/."
        )
      );
    }

    for (const [index, diagram] of (result.diagrams || []).entries()) {
      const section = card(diagram.title);
      section.appendChild(
        node("p", "hint", `${diagram.node_count} nodes, ${diagram.edge_count} edges — ${diagram.filename}`)
      );

      if (mermaid) {
        const host = node("div", "mermaid-host");
        section.appendChild(host);
        try {
          const { svg } = await mermaid.render(`diagram-${index}-${Date.now()}`, diagram.mermaid);
          host.innerHTML = svg;
        } catch (error) {
          host.appendChild(node("p", "hint error", `Could not render: ${error.message}`));
          host.appendChild(node("pre", "mermaid-source", diagram.mermaid));
        }
      } else {
        section.appendChild(node("pre", "mermaid-source", diagram.mermaid));
      }

      const copy = node("button", "secondary", "Copy Mermaid source");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(diagram.mermaid);
          copy.textContent = "Copied";
          setTimeout(() => (copy.textContent = "Copy Mermaid source"), 1500);
        } catch {
          copy.textContent = "Copy failed";
        }
      });
      section.appendChild(copy);

      if (diagram.omitted_node_count || diagram.omitted_edge_count) {
        section.appendChild(
          node(
            "p",
            "hint",
            `${diagram.omitted_node_count} node(s) and ${diagram.omitted_edge_count} edge(s) omitted to keep the diagram readable.`
          )
        );
      }
      for (const warning of diagram.warnings || []) {
        section.appendChild(node("p", "hint", warning));
      }
      output.appendChild(section);
    }

    if (result.written_files && result.written_files.length) {
      const written = card("Written to disk");
      const list = node("ul", "notes");
      for (const path of result.written_files) list.appendChild(node("li", null, path));
      written.appendChild(list);
      output.appendChild(written);
    }
    renderWarnings(output, result.warnings, false);
  }

  function renderTools(data) {
    clearOutput();
    const output = el("analysis-output");
    const tools = card("Analysis tools");
    tools.appendChild(
      table(
        ["Tool", "Level", "Status", "Found at"],
        (data.tools || []).map((tool) => [
          tool.name,
          tool.level,
          tool.status === "installed"
            ? badge("installed", "good")
            : badge(tool.status.replace(/_/g, " "), tool.fallback ? "warn" : "bad"),
          tool.path,
        ])
      )
    );
    tools.appendChild(
      node("p", "hint", "Missing tools fall back to built-in Python analyzers, or return a structured error when there is no fallback.")
    );
    output.appendChild(tools);

    const languages = card("Supported languages");
    languages.appendChild(
      node("p", "hint", (data.supported_languages || []).join(", "))
    );
    output.appendChild(languages);
  }

  // -- actions --------------------------------------------------------------

  async function run(label, action) {
    busy(true);
    setStatus(`${label}...`);
    try {
      await action();
      setStatus(`${label} complete.`);
    } catch (error) {
      setStatus(error.message, true);
      clearOutput();
    } finally {
      busy(false);
    }
  }

  function selectedKinds() {
    return Array.from(el("diagram-kinds").querySelectorAll("input:checked")).map(
      (input) => input.value
    );
  }

  // -- wiring ---------------------------------------------------------------

  function showTab(name) {
    const isChat = name === "chat";
    el("chat-panel").hidden = !isChat;
    el("analysis-panel").hidden = isChat;
    el("tab-chat").classList.toggle("active", isChat);
    el("tab-analysis").classList.toggle("active", !isChat);
    el("tab-chat").setAttribute("aria-selected", String(isChat));
    el("tab-analysis").setAttribute("aria-selected", String(!isChat));
  }

  document.addEventListener("repo-selected", (event) => {
    repo = event.detail.repo;
    busy(false);
    setStatus("");
    clearOutput();
  });

  el("tab-chat").addEventListener("click", () => showTab("chat"));
  el("tab-analysis").addEventListener("click", () => showTab("analysis"));

  el("btn-inventory").addEventListener("click", () =>
    run("Reading inventory", async () =>
      renderInventory(await getJson(`/api/repos/${encodeURIComponent(repo)}/inventory`))
    )
  );

  el("btn-oop").addEventListener("click", () =>
    run("Analyzing OOP structure", async () =>
      renderOop(await getJson(`/api/repos/${encodeURIComponent(repo)}/oop`))
    )
  );

  el("btn-diagrams").addEventListener("click", () => {
    const kinds = selectedKinds();
    if (!kinds.length) {
      setStatus("Select at least one diagram kind.", true);
      return;
    }
    return run("Generating diagrams", async () =>
      renderDiagrams(
        await getJson(`/api/repos/${encodeURIComponent(repo)}/diagrams`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kinds, write: true }),
        })
      )
    );
  });

  el("btn-tools").addEventListener("click", () =>
    run("Checking tools", async () => renderTools(await getJson("/api/operations/tools")))
  );
})();
