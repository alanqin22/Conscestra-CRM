# Executive Report — Template & Pattern Guide

> **Sources of truth:**
> - `notifications-mgmt.html` — first implementation (12 "Executive alerts" chips)
> - `activity-mgmt.html` — second implementation, adapted to different helper-function
>   names (proves the pattern generalizes across modules with different conventions)
> - `app/agents/orchestrator/executive.py` (`format_exec_answer()`) — the **shared**
>   backend function that produces the markdown for `mode: 'executive_question'`,
>   used by **all 10** agent formatters (accounting, accounts, activities, analytics,
>   contacts, leads, notifications, opportunities, orders, products)
>
> This document captures everything needed to render `mode: 'executive_question'`
> responses as a formal, letterhead-style "Executive Report" instead of plain
> markdown, and to replicate that treatment across every agent mgmt page.

---

## 1. Background — the `format_exec_answer()` contract

Every "Executive question" chip (one per agent's home page) ultimately calls
`format_exec_answer(pack, sections, note)` in
`app/agents/orchestrator/executive.py`. Regardless of which agent/module asked
the question, the response always has:

```json
{ "output": "<markdown>", "mode": "executive_question", "report_mode": "executive_question", "success": true }
```

And `output` always follows this **deterministic structure**:

```
### 💼 Executive Answer
**As of:** 2026-06-11T17:55

📌 **Impact now:** <one-sentence so-what summary>

**Confidence:** High|Medium|Low · **Top drivers:** 1) <driver 1> 2) <driver 2> 3) <driver 3>
**Recommended action:** <Owner role> — <one-sentence next step>.
**Drill-down:** [<label>](<href>)

> ℹ️ <optional scope note — explains a CRM data-modeling caveat>

#### <icon> <Section 1 title>
<markdown content — usually a table or numbered list>

#### <icon> <Section 2 title>
<markdown content>

_Source: sp_orchestrator executive pack — live CRM data._
```

Notes:
- The title icon/text (`💼 Executive Answer`) is **always identical** — it comes
  from one shared function, not per-agent.
- `**Recommended action:**` and `**Drill-down:**` and the `> ℹ️` scope note are
  all **optional** — present only when the pack has the relevant data.
- `**Top drivers:**` is either `1) ... 2) ... 3) ...` or the placeholder `—`.
- Section headers are `#### <emoji> <Title>` (h4) — most agent `renderMarkdown()`
  implementations do **not** handle `####`, which is why a raw render looks
  broken (literal `#### 💝 Activity Pulse` text, literal `[label](url)` links,
  oversized/blue `### Executive Answer` heading).

The `renderExecutiveReport()` function below parses this structure directly
(without depending on `renderMarkdown()`'s header handling) and only delegates
**section bodies** (tables/lists/paragraphs — never `####` headers) to the
module's existing `renderMarkdown()`.

---

## 2. Per-Module Reference Table

Each mgmt page has its own naming conventions. Look up the module you're
updating and substitute accordingly when copying the CSS/JS below.

| Module file | Inline-markdown parser | Table renderer | `escapeHTML` | Back Home handler | Back to AI handler | Back Home button class | `executive_question` status |
|---|---|---|---|---|---|---|---|
| `notifications-mgmt.html` | `parseInline(text)` *(calls `escapeHTML` first, then link/bold/italic/code)* | `renderTable(headers, rows)` | ✅ | `goHome()` | `showNotifAIPage()` | `.action-bar-btn.btn-back-home` / `.action-bar-btn.btn-all-read` | ✅ **Done** |
| `activity-mgmt.html` | `parseMarkdownInline(text)` *(no `escapeHTML` — bold/italic/code only; link added for this feature)* | `renderMarkdownTable(headers, rows)` | ✅ (`escapeHTML`, used separately) | `showQuickUI()` | `showActivityAIPage()` | `.back-home-btn` (wrap both buttons in `.back-home-btns` or `.exec-report-actions`) | ✅ **Done** |
| `accounting-mgmt.html` | `parseMarkdownInline(text)` *(new helper: link + bold + italic via both `*..*` and `_.._` + inline code, no `escapeHTML`)* | `execConvertMarkdownTables(text)` / `execConvertTableToHTML()` *(gradient-header inline-styled `<table>`, no class; renamed from `product-mgmt.html`'s `convertMarkdownTablesToHTML`/`convertTableToHTML` to avoid colliding with accounting's existing differently-signatured `convertMarkdownTablesToHTML(text, mode, parsedPagination)`)* + `renderExecSectionBody()` | n/a (none in file) | `location.reload()` | `showAcctAIPage()` | `.unified-action-btn` | ✅ **Done** — content-based detection in `buildAccountingResponseHTML()`; `_applyChrome(effectiveMode)` already hides `#app-header`/`.input-bar` for `executive_question` via existing rules, no `on-exec-report-page` class needed |
| `account-mgmt.html` (accounts) | `parseMarkdownInline(text)` *(new helper: link + bold + italic via both `*..*` and `_.._` + inline code, no `escapeHTML`)* | `execConvertMarkdownTables(text)` / `execConvertTableToHTML()` *(gradient-header inline-styled `<table>`, no class)* + `renderExecSectionBody()` | ✅ (used for `asOf`, `confidence`, `recommendedOwner`, `drilldownHref` only) | `goBackHome()` | `showAccountAIPage()` | `.unified-action-btn` | ✅ **Done** — content-based detection at the top of `buildResponseHTML()`; `'executive_question'` added to `_ACCT_DATA_MODES` so `.input-bar` hides via the existing `on-conversation-page` rule; `#app-header`/`header` hides via the existing `body:not(.on-home-page):not(.on-list-page) header` rule once `goBackHome()`'s `on-home-page` class is removed — no new CSS class needed |
| `analytics-mgmt.html` | `mdInline(text)` *(pre-existing, `escapeHTML`-based; already handled bold/link/underscore-italic — unchanged)* | `renderTable(headers, rows)` via `buildTextResponseHTML(sec.lines.join('\n'))` for section bodies | ✅ (pre-existing) | `goBackToAnalyticsHome()` | `showAnalyticsAIPage()` | `.unified-action-btn` | ✅ **Done** (plus new `body.on-exec-report-page` class hides `#app-header` while keeping `.input-bar` visible — see note below) |
| `contact-mgmt.html` | `parseMarkdownInline(text)` *(new helper: link + bold + italic via both `*..*` and `_.._` + inline code, no `escapeHTML`)* | `execConvertMarkdownTables(text)` / `execConvertTableToHTML()` *(gradient-header inline-styled `<table>`, no class)* + `renderExecSectionBody()` | ✅ (used for `asOf`, `confidence`, `recommendedOwner`, `drilldownHref` only) | `goHome()` (`hideQuickUI()`/`showQuickUI()` exist too) | `showContactAIPage()` | `.unified-action-btn` | ✅ **Done** — content-based detection at the top of `buildResponseHTML()`; `'executive_question'` added to `_CONTACT_DATA_MODES` so `.input-bar` hides via the existing `on-conversation-page` rule; `header` hides via the existing `body:not(.on-home-page):not(.on-list-page) header` rule once `goHome()`'s `on-home-page` class is added back — no new CSS class needed |
| `lead-mgmt.html` | `parseMarkdownInline(text)` *(new helper: link + bold + italic via both `*..*` and `_.._` + inline code, no `escapeHTML`)* | `execConvertMarkdownTables(text)` / `execConvertTableToHTML()` *(gradient-header inline-styled `<table>`, no class)* + `renderExecSectionBody()` | ✅ (used for `asOf`, `confidence`, `recommendedOwner`, `drilldownHref` only) | `goHome()` | `showLeadAIPage()` | `.unified-action-btn` | ✅ **Done** — content-based detection at the top of `buildResponseHTML()`; no data-modes array change needed because `sendMessage()`'s existing `isDataMode = !!resMode && resMode !== 'conversational'` check already removes `on-conversation-page` for `executive_question` (so `.input-bar` hides via the existing rule); `header` hides via the existing `body:not(.on-home-page):not(.on-list-page) header` rule once `goHome()` adds `on-home-page` back — no new CSS class needed |
| `opportunity-mgmt.html` | `parseMarkdownBold(text)` *(bold + `[label](url)` link via inline `style="color:#4f46e5;font-weight:600;"`, no `escapeHTML` — link regex already present, not modified)* | `renderSimpleTable(headers, rows)` *(outputs `.products-table`, called from `parseMarkdownContent()`)* | ✅ (`escapeHTML`, used separately) | `goHome()` | `showOppAIPage()` | `.back-home-btn` | ✅ **Done** |
| `order-mgmt.html` | `parseMarkdownInline(text)` *(extracted from `convertMarkdownToHTML()`'s replace chain: link + bold + italic + code, no `escapeHtml`)* | `convertMarkdownToHTML(sec.lines.join('\n'))` / `convertTableToHTML()` *(gradient-header inline-styled `<table>`, no class)* | ✅ (`escapeHtml`, lowercase h, DOM-based) | `goBackHome()` | `showOrderAIPage()` | `.unified-action-btn` | ✅ **Done** |
| `product-mgmt.html` | `parseMarkdownInline(text)` *(new helper: link + bold + italic via both `*..*` and `_.._` + inline code, no `escapeHTML`)* | `convertMarkdownTablesToHTML(text)` / `convertTableToHTML()` *(gradient-header inline-styled `<table>`, no class)* + `renderExecSectionBody()` | ✅ (`escapeHTML`, proper-cased) | `goBackHome()` | `showProdAIPage()` | `.unified-action-btn` | ✅ **Done** |
| `orchestrator-mgmt.html` | `parseInline(text)` *(pre-existing, `escapeHTML`-based; bold/italic/code — link regex added for this feature)* | `renderTable(rows)` via `renderMarkdown(sec.lines.join('\n'))` for section bodies | ✅ (pre-existing) | `goHome()` | `showOrchAIPage()` | `.action-btn.action-btn-home` / `.action-btn.action-btn-primary` | ✅ **Done** — special case: backend returns `mode: 'executive'` (not `'executive_question'`); content-based detection used, see note below |

For modules with **"none — inline `.replace()` chain"**, `renderMarkdown()`
applies bold/italic/code/link replacements directly inline rather than via a
named helper. For those modules, either:
1. Extract the chain into a small `parseMarkdownInline(text)` function (preferred —
   matches the activity-mgmt.html pattern and keeps `renderExecutiveReport()` clean), or
2. Duplicate the minimal replace chain (link → bold → italic → code) directly
   inside `renderExecutiveReport()`.

Always confirm `header h1 { font-size: ... }` for the module — it is **`1.2rem`**
for every module except `analytics-mgmt.html` (`1.25rem`). `.exec-report-title`
must match it.

---

## 3. CSS — `.exec-report*` block

Insert this whole block once per file, anywhere in the `<style>` section (e.g.
right after the existing "Back Home Button" CSS). Update the two
module-specific bits:

- The `/* match header h1 (...) */` comment + `font-size` in `.exec-report-title`
  (and its `@media (max-width: 640px)` override) — use the module's `header h1`
  font-size (almost always `1.2rem`).
- If `.md-link` already exists in the file (check first — some modules may
  already define a generic markdown-link style), skip that part.

```css
/* Generic markdown link (e.g. Drill-down links) */
.md-link {
    color: #0078d7;
    font-weight: 600;
    text-decoration: none;
    border-bottom: 1px solid rgba(0, 120, 215, 0.35);
    transition: border-color 0.15s ease, color 0.15s ease;
}

.md-link:hover {
    color: #005ea3;
    border-color: #005ea3;
}

/* ═══════════════════════════════════════════════════════════════
   EXECUTIVE REPORT — formal report layout for the [Module] AI
   Agent's "Executive" question chips
   ═══════════════════════════════════════════════════════════════ */
.exec-report {
    max-width: 900px;
    margin: 1.25rem auto 2rem;
    background: #ffffff;
    border-radius: 14px;
    box-shadow: 0 8px 30px rgba(15, 35, 65, 0.10);
    border: 1px solid #e6ebf2;
    overflow: hidden;
}

.exec-report-header {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 1.5rem 2rem 1.15rem;
    background: linear-gradient(135deg, #f4f8ff 0%, #eef3fb 100%);
    border-bottom: 4px solid #0078d7;
    border-image: linear-gradient(90deg, #0078d7 0%, #6f42c1 35%, #e83e8c 70%, #fd7e14 100%) 1;
}

.exec-report-icon {
    font-size: 2.3rem;
    line-height: 1;
    flex-shrink: 0;
}

.exec-report-title {
    margin: 0;
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 1.2rem;            /* match header h1 ("[icon] [Module]") */
    font-weight: 700;
    letter-spacing: 0.2px;
    background: linear-gradient(90deg, #0078d7 0%, #6f42c1 35%, #e83e8c 70%, #fd7e14 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    color: transparent;
}

.exec-report-asof {
    margin-top: 0.3rem;
    font-size: 0.83rem;
    font-style: italic;
    color: #6b7280;
}

.exec-report-headline {
    display: flex;
    gap: 0.85rem;
    align-items: flex-start;
    margin: 1.5rem 2rem 0;
    padding: 1rem 1.25rem;
    background: #fff8ec;
    border-left: 4px solid #fd7e14;
    border-radius: 0 8px 8px 0;
}

.exec-headline-icon {
    font-size: 1.35rem;
    line-height: 1.4;
    flex-shrink: 0;
}

.exec-headline-label {
    display: block;
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #b45309;
    margin-bottom: 0.25rem;
}

.exec-headline-text {
    font-size: 1.02rem;
    line-height: 1.6;
    color: #333;
}

.exec-decision-panel {
    margin: 1.25rem 2rem 0;
    padding: 1.1rem 1.25rem;
    background: #f8f9fc;
    border: 1px solid #e6ebf2;
    border-radius: 10px;
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
}

.exec-decision-row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 0.6rem 1rem;
}

.exec-recommended-row,
.exec-drilldown-row {
    align-items: center;
}

.exec-confidence-badge {
    display: inline-flex;
    align-items: center;
    padding: 0.3rem 0.85rem;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 700;
    white-space: nowrap;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.exec-confidence-high { background: #e6f7ec; color: #1a7f43; }
.exec-confidence-medium { background: #fff4e0; color: #b45309; }
.exec-confidence-low { background: #fde8e8; color: #b3261e; }

.exec-drivers { flex: 1; min-width: 220px; }

.exec-drivers-label,
.exec-recommended-label {
    display: block;
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #6b7280;
}

.exec-recommended-label { display: inline-block; margin-right: 0.4rem; }

.exec-drivers ol {
    margin: 0.25rem 0 0;
    padding-left: 1.2rem;
    font-size: 0.88rem;
    color: #374151;
    line-height: 1.6;
}

.exec-owner-badge {
    background: #e8f0fe;
    color: #0b5cab;
    font-weight: 700;
    font-size: 0.78rem;
    padding: 0.2rem 0.6rem;
    border-radius: 14px;
    white-space: nowrap;
}

.exec-recommended-text { font-size: 0.92rem; color: #333; }

.exec-drilldown-link {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    color: #0078d7;
    font-weight: 600;
    font-size: 0.88rem;
    text-decoration: none;
    border-bottom: 1px solid transparent;
    transition: border-color 0.15s ease;
}

.exec-drilldown-link:hover { border-color: #0078d7; }

.exec-scope-note {
    display: flex;
    gap: 0.6rem;
    align-items: flex-start;
    margin: 1.25rem 2rem 0;
    padding: 0.85rem 1.1rem;
    background: #eef6ff;
    border: 1px solid #d6e7fb;
    border-radius: 8px;
    font-size: 0.85rem;
    color: #355070;
    line-height: 1.55;
}

.exec-scope-icon { font-size: 1.05rem; flex-shrink: 0; }

.exec-report-body {
    padding: 1.5rem 2rem 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

.exec-section {
    border: 1px solid #e6ebf2;
    border-radius: 10px;
    overflow: hidden;
    background: #fff;
}

.exec-section-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.65rem 1.1rem;
    background: linear-gradient(135deg, #f8f9fa 0%, #eef1f5 100%);
    border-bottom: 1px solid #e6ebf2;
}

.exec-section-icon { font-size: 1.1rem; line-height: 1; }

.exec-section-title {
    margin: 0;
    font-size: 0.96rem;
    font-weight: 700;
    background: linear-gradient(90deg, #0078d7 0%, #6f42c1 35%, #e83e8c 70%, #fd7e14 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    color: transparent;
}

.exec-section-body { padding: 0.6rem 1.1rem 0.9rem; }
.exec-section-body .md-table-container { margin: 0.5rem 0 0; box-shadow: none; border: 1px solid #eef0f3; border-radius: 8px; }
.exec-section-body .md-spacer:first-child,
.exec-section-body .md-spacer:last-child { display: none; }
.exec-section-body .md-p { margin: 0.35rem 0; }

.exec-report-footer {
    margin: 0.25rem 2rem 0;
    padding: 1rem 0;
    border-top: 1px solid #eef0f3;
    text-align: center;
    font-size: 0.78rem;
    font-style: italic;
    color: #9aa3b2;
}

.exec-report-actions {
    display: flex;
    justify-content: center;
    gap: 1rem;
    padding: 1.25rem 2rem 1.75rem;
    flex-wrap: wrap;
}

@media (max-width: 640px) {
    .exec-report { margin: 0.75rem auto 1.5rem; border-radius: 10px; }
    .exec-report-header { padding: 1.1rem 1.25rem 0.9rem; gap: 0.75rem; }
    .exec-report-icon { font-size: 1.8rem; }
    .exec-report-title { font-size: 1.2rem; }
    .exec-report-headline,
    .exec-decision-panel,
    .exec-scope-note,
    .exec-report-footer { margin-left: 1.25rem; margin-right: 1.25rem; }
    .exec-report-body { padding: 1.1rem 1.25rem 0.5rem; gap: 1rem; }
    .exec-report-actions { padding: 1rem 1.25rem 1.5rem; }
}
```

`.exec-section-body` relies on the module's existing markdown CSS classes —
confirm these class names match before pasting (all observed modules use the
same names, but verify):
- `.md-table-container` (table wrapper)
- `.md-spacer` (blank-line spacer divs)
- `.md-p` (paragraph)
- `.md-list` (bullet lists), `.md-h2`/`.md-h3` (not used inside exec sections,
  since `####` is stripped before delegating to `renderMarkdown()`)

`opportunity-mgmt.html` is the exception: its generic renderer
(`parseMarkdownContent()`) already strips blank lines (no `.md-spacer`
equivalent) and outputs inline-styled `<p>` and `<table class="products-table">`
rather than `.md-p`/`.md-table-container`. Its `.exec-section-body` block omits
the `.md-spacer`/`.md-p` overrides entirely and instead adds:
```css
.exec-section-body .products-table { margin-top: 0.4rem; }
```

---

## 4. JS — inline-markdown link support

`renderExecutiveReport()` needs `[label](url)` → `<a class="md-link">` support
in whichever function does inline-markdown replacement (bold/italic/code).

**If the module has a named function** (`parseInline`, `parseMarkdownInline`,
etc.), add the link replacement as the **first** regex, before bold/italic/code:

```js
// notifications-mgmt.html style — escapeHTML() runs first, link regex after
function parseInline(text) {
    let result = escapeHTML(text);
    result = result.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" class="md-link">$1</a>');
    result = result.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    result = result.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    result = result.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
    return result;
}

// activity-mgmt.html style — no escapeHTML (pre-existing behavior; do not add it)
function parseMarkdownInline(text) {
    if (!text) return '';
    return text
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" class="md-link">$1</a>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/_([^_]+)_/g, '<em>$1</em>')
        .replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
}
```

> **Do not change whether the function calls `escapeHTML()`.** Some modules
> escape first (then the link regex still matches, since `escapeHTML` doesn't
> touch `[`, `]`, `(`, `)`); others don't escape at all. Match the existing
> behavior — adding/removing escaping is a separate, out-of-scope change.

**If the module has no named inline-parser** (most modules — see table above),
extract the existing inline replace-chain from `renderMarkdown()` into a small
`parseMarkdownInline(text)` function (same shape as the activity-mgmt.html
version above, using whatever bold/italic/code regexes the module already
uses), add the link regex as the first line, and call it from both
`renderMarkdown()` (replacing the inline chain) and `renderExecutiveReport()`.

---

## 5. JS — `parseDrivers()`, `execReportActions()`, `renderExecutiveReport()`

Insert these three functions before the module's
`// ENHANCED MARKDOWN RENDERING` / `// MARKDOWN PARSER` section (so
`renderExecutiveReport()` can call the module's existing `renderMarkdown()`).

Replace every `INLINE(...)` below with the module's inline-parser call
(`parseInline(...)` or `parseMarkdownInline(...)`), and `TABLE_RENDERER` /
`BACK_HOME_FN` / `BACK_TO_AI_FN` / button HTML per the reference table in
section 2.

```js
// ========================================================================
// EXECUTIVE REPORT RENDERING
// Renders the deterministic markdown produced by format_exec_answer() in
// app/agents/orchestrator/executive.py (mode: 'executive_question') —
// used by the [Module] AI Agent's "Executive" question chips — as a
// formal letterhead-style report instead of plain markdown.
// ========================================================================

// Splits a "1) ... 2) ... 3) ..." driver list into an array of strings.
// Returns [] for the placeholder "—" (no drivers available).
function parseDrivers(raw) {
    if (!raw || raw.trim() === '—') return [];
    const drivers = [];
    const re = /(\d+)\)\s*(.*?)(?=\s\d+\)\s|$)/gs;
    let m;
    while ((m = re.exec(raw)) !== null) {
        const text = m[2].trim();
        if (text) drivers.push(text);
        if (m.index === re.lastIndex) re.lastIndex++;
    }
    return drivers;
}

// Back Home / Back to AI button row shown at the bottom of every
// Executive Report.
function execReportActions() {
    return `<div class="exec-report-actions">
        <button class="BACK_HOME_BTN_CLASS" onclick="BACK_HOME_FN()">🏠 Back Home</button>
        <button class="BACK_HOME_BTN_CLASS" onclick="BACK_TO_AI_FN()">🤖 Back to AI</button>
    </div>`;
}

// Parses the deterministic markdown structure emitted by
// format_exec_answer() into a formal "Executive Report" layout.
// Returns null (so the caller can fall back to renderMarkdown) if the
// text doesn't start with the expected "### <icon> Executive Answer"
// heading.
function renderExecutiveReport(text) {
    if (!text) return null;
    const lines = text.split('\n');
    let i = 0;

    const titleMatch = (lines[i] || '').match(/^###\s+(\S+)\s+(Executive Answer)\s*$/);
    if (!titleMatch) return null;
    const titleIcon = titleMatch[1];
    const titleText = titleMatch[2];
    i++;

    let asOf = '';
    const asOfMatch = (lines[i] || '').match(/^\*\*As of:\*\*\s*(.*)$/);
    if (asOfMatch) { asOf = asOfMatch[1]; i++; }

    while (lines[i] !== undefined && lines[i].trim() === '') i++;

    let headlineIcon = '📌', headlineText = '';
    const headlineMatch = (lines[i] || '').match(/^(\S+)\s+\*\*Impact now:\*\*\s*(.*)$/);
    if (headlineMatch) {
        headlineIcon = headlineMatch[1];
        headlineText = headlineMatch[2];
        i++;
    }

    while (lines[i] !== undefined && lines[i].trim() === '') i++;

    let confidence = '', driversRaw = '';
    const confMatch = (lines[i] || '').match(/^\*\*Confidence:\*\*\s*(\w+)\s*·\s*\*\*Top drivers:\*\*\s*(.*)$/);
    if (confMatch) {
        confidence = confMatch[1];
        driversRaw = confMatch[2];
        i++;
    }

    let recommendedOwner = '', recommendedStep = '';
    const recMatch = (lines[i] || '').match(/^\*\*Recommended action:\*\*\s*(.*)$/);
    if (recMatch) {
        const parts = recMatch[1].split(' — ');
        if (parts.length >= 2) {
            recommendedOwner = parts[0];
            recommendedStep = parts.slice(1).join(' — ');
        } else {
            recommendedStep = recMatch[1];
        }
        i++;
    }

    let drilldownLabel = '', drilldownHref = '';
    const drillMatch = (lines[i] || '').match(/^\*\*Drill-down:\*\*\s*\[([^\]]+)\]\(([^)]+)\)/);
    if (drillMatch) {
        drilldownLabel = drillMatch[1];
        drilldownHref = drillMatch[2];
        i++;
    }

    while (lines[i] !== undefined && lines[i].trim() === '') i++;

    let scopeNote = '';
    const noteMatch = (lines[i] || '').match(/^>\s*ℹ️\s*(.*)$/);
    if (noteMatch) {
        scopeNote = noteMatch[1];
        i++;
        while (lines[i] !== undefined && lines[i].trim() === '') i++;
    }

    // Footer (last "_Source: ..._" line) — strip it from the body before
    // splitting into sections.
    let footer = '';
    let bodyLines = lines.slice(i);
    for (let j = bodyLines.length - 1; j >= 0; j--) {
        const fm = bodyLines[j].match(/^_Source:\s*(.+)_$/);
        if (fm) {
            footer = fm[1];
            bodyLines = bodyLines.slice(0, j);
            break;
        }
    }

    // Split remaining lines into "#### <icon> <title>" sections.
    const sections = [];
    let cur = null;
    for (const line of bodyLines) {
        const secMatch = line.match(/^####\s+(\S+)\s+(.*)$/);
        if (secMatch) {
            if (cur) sections.push(cur);
            cur = { icon: secMatch[1], title: secMatch[2], lines: [] };
        } else if (cur) {
            cur.lines.push(line);
        }
    }
    if (cur) sections.push(cur);

    // ── Build the report ────────────────────────────────────────────
    let html = '<div class="exec-report">';

    html += `<div class="exec-report-header">
        <div class="exec-report-icon">${titleIcon}</div>
        <div>
            <h2 class="exec-report-title">${INLINE(titleText)}</h2>
            ${asOf ? `<div class="exec-report-asof">As of ${escapeHTML(asOf)}</div>` : ''}
        </div>
    </div>`;

    if (headlineText) {
        html += `<div class="exec-report-headline">
            <div class="exec-headline-icon">${headlineIcon}</div>
            <div>
                <span class="exec-headline-label">Impact Now</span>
                <div class="exec-headline-text">${INLINE(headlineText)}</div>
            </div>
        </div>`;
    }

    const drivers = parseDrivers(driversRaw);
    const confClass = confidence ? `exec-confidence-${confidence.toLowerCase()}` : '';
    if (confidence || drivers.length || recommendedStep || drilldownHref) {
        html += '<div class="exec-decision-panel">';

        if (confidence || drivers.length) {
            html += '<div class="exec-decision-row">';
            if (confidence) {
                html += `<span class="exec-confidence-badge ${confClass}">${escapeHTML(confidence)} Confidence</span>`;
            }
            if (drivers.length) {
                html += `<div class="exec-drivers"><span class="exec-drivers-label">Top Drivers</span>
                    <ol>${drivers.map(d => `<li>${INLINE(d)}</li>`).join('')}</ol>
                </div>`;
            }
            html += '</div>';
        }

        if (recommendedStep) {
            html += `<div class="exec-decision-row exec-recommended-row">
                <span class="exec-recommended-label">Recommended Action</span>
                ${recommendedOwner ? `<span class="exec-owner-badge">${escapeHTML(recommendedOwner)}</span>` : ''}
                <span class="exec-recommended-text">${INLINE(recommendedStep)}</span>
            </div>`;
        }

        if (drilldownHref) {
            html += `<div class="exec-decision-row exec-drilldown-row">
                <a class="exec-drilldown-link" href="${escapeHTML(drilldownHref)}">🔗 ${INLINE(drilldownLabel)}</a>
            </div>`;
        }

        html += '</div>';
    }

    if (scopeNote) {
        html += `<div class="exec-scope-note">
            <span class="exec-scope-icon">ℹ️</span>
            <div>${INLINE(scopeNote)}</div>
        </div>`;
    }

    if (sections.length) {
        html += '<div class="exec-report-body">';
        for (const sec of sections) {
            html += `<div class="exec-section">
                <div class="exec-section-header">
                    <span class="exec-section-icon">${sec.icon}</span>
                    <h4 class="exec-section-title">${INLINE(sec.title)}</h4>
                </div>
                <div class="exec-section-body">${renderMarkdown(sec.lines.join('\n'))}</div>
            </div>`;
        }
        html += '</div>';
    }

    if (footer) {
        html += `<div class="exec-report-footer">Source: ${INLINE(footer)}</div>`;
    }

    html += execReportActions();
    html += '</div>';
    return html;
}
```

Notes:
- `escapeHTML(...)` is used directly (not via `INLINE`) for plain values that
  must never contain markdown (timestamps, confidence labels, owner names,
  href attributes) — every observed module has an `escapeHTML` function.
- `renderMarkdown(sec.lines.join('\n'))` reuses the module's existing markdown
  renderer for section bodies (tables/lists/paragraphs). Section bodies never
  contain `####` headers (those were already consumed as section delimiters),
  so the module's lack of h4 support is a non-issue.

---

## 6. JS — wire `handleResponse()`

Add a check for `data.mode === 'executive_question'` that calls
`renderExecutiveReport()` and falls back to the existing generic-markdown path
if it returns `null` (defensive — should only happen if the backend output
doesn't match the expected structure).

```js
// Executive Answer (one of the "Executive" chips on the [Module] AI Agent
// home page) — render as a formal, letterhead-style report instead of
// plain markdown.
if (data.mode === 'executive_question') {
    const execHtml = renderExecutiveReport(data.output);
    if (execHtml) {
        responseArea.innerHTML = `<div class="fade-in">${execHtml}</div>`;
        return;
    }
}
```

**Where to insert it** varies by module's `handleResponse()` structure — place
it as early as possible, before any structured-key (`data.summary`,
`data.activities`, etc.) or `effectiveMode`-based routing, since
`exec_markdown` responses never set those other keys and `'executive_question'`
won't match `effectiveMode.includes('list'|'summary'|'timeline'|...)` checks
anyway (so it would otherwise fall all the way through to the generic markdown
fallback — which is exactly the *un-styled* behavior being fixed).

Good insertion points seen so far:
- **notifications-mgmt.html**: after the `on-conversation-page`/`on-ai-response`
  class toggling, just before the final generic `renderMarkdown(data.output)` call.
- **activity-mgmt.html**: immediately after the `data.metadata.status === 'error'`
  check, before "1. Structured JSON keys".

`accounting-mgmt.html` already has `'executive_question'` referenced (a label
map entry and a "known modes" list) — check what those are used for before
adding the render branch, to avoid duplicate/conflicting handling.

**order-mgmt.html** is wired differently: instead of gating on
`textMode === 'executive_question'`, it calls `renderExecutiveReport(rawText)`
unconditionally (right after `textMode` is computed in
`displayOrderResponse()`, before the `textModeMap`/`show_order_form` logic)
and only proceeds if the result is non-null. This is because at least one
Executive chip ("Order concentration") was observed to come back with
`mode: "conversational"` while still emitting the deterministic
`### 💼 Executive Answer` markdown — gating on `textMode` alone would have
let that response fall through to `routeByContent()`, which mis-fires on the
"Invoice" substring inside the exec markdown's table and prepends a spurious
"📋 Order Details" `<h2>`. Content-based detection sidesteps that backend
mode-labeling inconsistency entirely and is the more robust pattern if other
modules hit the same issue.

**product-mgmt.html** follows the same content-based pattern as order-mgmt.html:
`renderExecutiveReport(rawText)` is called unconditionally near the top of
`buildProductResponseHTML()` (right before `switch(mode) {`), and its result
is returned immediately if non-null — `mode === 'executive_question'` is not
checked. Its `parseMarkdownInline()` also handles **both** italic syntaxes:
`*text*` (single-asterisk, seen in table cells) and `_text_` (underscore, seen
in section-body asides like `_Cost basis: current Wholesale price (same
convention as the Accounting margin analytics)._`). If a future module's
fixtures contain underscore-italic asides, add the same
`.replace(/_(.+?)_/g, '<em>$1</em>')` step to its inline parser.

`analytics-mgmt.html` already had a check in `buildResponseHTML()`:
```js
if (data && data.mode === 'executive_question') {
    return buildTextResponseHTML(text);
}
```
The `buildTextResponseHTML(text)` call was replaced with
`renderExecutiveReport(text) || buildTextResponseHTML(text)`. Its existing
`mdInline(text)` inline parser already handled bold/link/underscore-italic
(it's `escapeHTML`-based, unlike product-mgmt.html's original parser), so no
inline-parser changes were needed — only reuse. Section bodies are rendered
via `buildTextResponseHTML(sec.lines.join('\n'))`, analytics' own markdown
renderer, which outputs `.table-wrapper`/`.report-table` (its existing table
styling) instead of `.md-table-container`.

One fixture (`revenue_concentration` chip) originally came back with
`mode: "dashboard"` and `### 💰 Revenue Summary` content instead of an
executive answer. **Fixed**: `app/agents/orchestrator/executive.py`'s
`EXEC_QA` pattern for the concentration question only matched the literal
substring `concentration`, but the chip's question text is "How concentrated
is revenue among top customers?" (word-form `concentrated`, not
`concentration`), so `match_exec_question()` returned `None` and the request
fell through to NL-report detection. Changed the pattern from
`concentration|top\s+10\s+customers|%\s*revenue\s+from` to
`concentrat\w*|top\s+10\s+customers|%\s*revenue\s+from` so it matches both
word forms. This fix benefits both the Analytics AI Agent's "Revenue
concentration" chip and the Orchestrator's "Customer concentration" chip
(same shared `EXEC_QA` list).

**New pattern: hide `#app-header` while keeping `.input-bar` visible.**
`analytics-mgmt.html` was the first module where the Executive Report should
hide the page's header bar (title/REPORT/RANGE dropdowns/search/Connected/
Reset) but still show the "Ask AI Agent..." input bar for follow-up questions.
Implementation:
- New CSS rule: `body.on-exec-report-page #app-header { display: none !important; }`
- In `sendMessage()`, after building `responseHTML`:
  `const isExecReport = responseHTML.includes('class="exec-report"');`
  then `document.body.classList.toggle('on-exec-report-page', isExecReport);`
- `.input-bar` visibility already follows the existing
  `body:not(.on-home-page):not(.on-conversation-page) .input-bar { display: none !important; }`
  rule, so `isExecReport` is also added to the `on-conversation-page`
  condition (alongside `conversational` mode / `fromAIPage`) to keep the
  input bar visible on exec-report pages.
- `on-exec-report-page` is removed alongside `on-conversation-page` in the
  `catch` block, `hideQuickUI()`, `showQuickUI()`, and `showAnalyticsAIPage()`
  (i.e. wherever the page navigates away from the report).
- Other modules that adopt this pattern should reuse the same
  `on-exec-report-page` class name and detection (`responseHTML.includes('class="exec-report"')`)
  for consistency.

**orchestrator-mgmt.html** also follows the content-based pattern, for a
different reason than order/product: the Orchestrator backend
(`app/agents/orchestrator/router.py`, step 1b) returns `mode: 'executive'`
for exec answers — distinct from every other module's
`mode: 'executive_question'` — so `data.mode === 'executive_question'`
would never match. In `sendMessage()`'s try block,
`renderExecutiveReport(output)` is called unconditionally on `d.output`;
if non-null, its result is wrapped in `<div class="fade-in">` and used as
`responseHTML`, otherwise the existing `renderMarkdown(output)` path is used.
`on-exec-report-page` is toggled via
`document.body.classList.toggle('on-exec-report-page', !!execHtml)` and
removed in `goHome()`, `showOrchAIPage()`, `conductSymphony()` (on entry),
and the `catch` block — i.e. everywhere the page navigates away from or
replaces the report. The orchestrator's `#app-header` is hidden the same way
(`body.on-exec-report-page #app-header { display: none !important; }`), and
`.input-bar` stays visible via the pre-existing
`on-conversation-page` class (added alongside `on-exec-report-page` in all
exec-report responses).

**accounting-mgmt.html** follows the content-based pattern, called
unconditionally near the top of `buildAccountingResponseHTML()` (right after
`effectiveMode` is computed, before `formatMarkdown(outputText, effectiveMode)`):
`'executive_question'` was already present in `modeTitles` and
`_ACCT_DATA_MODES` (so the mode was *recognized* before this change), but had
no dedicated renderer and fell through to `formatMarkdown()`, which doesn't
understand `####` h4 sections — producing the broken raw-markdown output this
feature fixes. If `renderExecutiveReport(outputText)` returns non-null,
`_applyChrome(effectiveMode || mode)` is called and the result returned
directly (no wrapper div), matching product-mgmt.html. No new
`on-exec-report-page` class or CSS rule was needed: `_applyChrome()` already
removes `on-home-page` for `executive_question`, and the existing rule
`body:not(.on-home-page) #app-header { display: none !important; }` hides the
header automatically; likewise `executive_question` was already in
`_ACCT_DATA_MODES`, so `on-conversation-page` is removed and `.input-bar` is
hidden, consistent with the page's other report modes (only the inline "Back
Home"/"Back to AI" buttons in `.exec-report-actions` are shown, via
`location.reload()` and `showAcctAIPage()`). accounting-mgmt.html has no
`escapeHTML()` at all, so — per the "Do not add `escapeHTML()` to parsers that
lack it" rule below — all `escapeHTML(...)` calls from the product-mgmt.html
reference implementation were replaced with raw interpolation
(`asOf`, `confidence`, `recommendedOwner`, `drilldownHref`). Its
`execConvertMarkdownTables`/`execConvertTableToHTML` table-converter helpers
were named distinctly from product-mgmt.html's `convertMarkdownTablesToHTML`/
`convertTableToHTML` because accounting-mgmt.html already has an unrelated
`convertMarkdownTablesToHTML(text, mode, parsedPagination)` with a different
signature.

**account-mgmt.html** (the Account AI Agent's "Executive questions" chips)
also follows the content-based pattern: `renderExecutiveReport(text)` is
called near the top of `buildResponseHTML(text, data)` (right after
`stripUserMessages`/whitespace guards, before `mode` is computed), and its
result is returned immediately if non-null. Before this change,
`'executive_question'` wasn't recognized by `buildResponseHTML()`'s
`switch(mode)` at all, so it fell through to `buildFallbackHTML()`'s "true
fallback" branch — a `escapeHTML(text)` dump in a `white-space:pre-wrap` div
with only a single "🏠 Back Home" button, producing the broken raw-markdown
output this feature fixes. `'executive_question'` was added to the
`_ACCT_DATA_MODES` array inside `sendMessage()` (used to decide whether to
remove `on-conversation-page`), so `.input-bar` now hides via the existing
`body:not(.on-ai-page):not(.on-conversation-page) .input-bar` rule — same
as the page's other report modes. No new CSS class was needed for the header
either: `header` already hides via the existing
`body:not(.on-home-page):not(.on-list-page) header { display: none !important; }`
rule once `sendMessage()` removes `on-home-page`. account-mgmt.html already
had `escapeHTML()` (used extensively for account/contact field rendering), so
— per the per-module reference table — `escapeHTML(...)` *was* applied to
`asOf`, `confidence`, `recommendedOwner`, and `drilldownHref` (the plain-text
fields), matching order-mgmt.html's `escapeHtml(...)` convention, while the
new `parseMarkdownInline()` helper itself remains unescaped (mirrors
accounting-mgmt.html's/product-mgmt.html's inline parser).

**contact-mgmt.html** (the Contact AI Agent's "Executive questions" chips)
follows the same content-based pattern: `renderExecutiveReport(text)` is
called right after the initial `!text` guard at the top of
`buildResponseHTML(text, data)`, before the error-flag checks and `mode`
computation, and its result is returned immediately if non-null. Before this
change, `'executive_question'` wasn't recognized by `buildResponseHTML()`'s
`switch(mode)` at all, so it fell through to `buildFallbackHTML()`'s "true
fallback" branch — an `escapeHTML(cleanText)` dump in a
`white-space:pre-wrap` `<pre>` with only a single "🏠 Back Home" button,
producing the broken raw-markdown output this feature fixes.
`'executive_question'` was added to the `_CONTACT_DATA_MODES` array inside
`sendMessage()` (used to decide whether to remove `on-conversation-page`), so
`.input-bar` now hides via the existing
`body:not(.on-ai-page):not(.on-conversation-page) .input-bar` rule — same as
the page's other report modes. No new CSS class was needed for the header
either: `header` already hides via the existing
`body:not(.on-home-page):not(.on-list-page) header { display: none !important; }`
rule, and `on-home-page` is not present while a chat response is showing.
contact-mgmt.html already had `escapeHTML()`, so — per the per-module
reference table — `escapeHTML(...)` *was* applied to `asOf`, `confidence`,
`recommendedOwner`, and `drilldownHref` (the plain-text fields), while the new
`parseMarkdownInline()` helper itself remains unescaped (mirrors
account-mgmt.html's inline parser). `execReportActions()` uses `goHome()` and
`showContactAIPage()` for its Back Home / Back to AI buttons.

**lead-mgmt.html** (the Lead AI Agent's "Executive questions" chips) follows
the same content-based pattern: `renderExecutiveReport(cleanText)` is called
inside `buildResponseHTML(text, data)` right after `cleanText` is computed
(via `stripUserMessages`), before the error-flag check and `mode` computation,
and its result is returned immediately if non-null. Before this change,
`'executive_question'` wasn't recognized by `buildResponseHTML()`'s
`switch(mode)` at all, so it fell through to `buildFallbackHTML()`'s "true
fallback" branch — a `renderMarkdown(text)` dump in an `.audit-record` box
with only a single "🏠 Back Home" button, producing the broken raw-markdown
output (literal `####` headers and pipe-table rows) this feature fixes.
Unlike contact-mgmt.html/account-mgmt.html, **no `_LEAD_DATA_MODES` array
change was needed**: `sendMessage()` already computes
`isDataMode = !!resMode && resMode !== 'conversational'`, and since
`'executive_question' !== 'conversational'` this is already `true`, so
`on-conversation-page` is removed and `.input-bar` hides via the existing
`body:not(.on-ai-page):not(.on-conversation-page) .input-bar` rule with no
code change. `header` hides via the existing
`body:not(.on-home-page):not(.on-list-page) header { display: none !important; }`
rule once `goHome()` adds `on-home-page` back. lead-mgmt.html already had
`escapeHTML()`, so — per the per-module reference table — `escapeHTML(...)`
*was* applied to `asOf`, `confidence`, `recommendedOwner`, and
`drilldownHref` (the plain-text fields), while the new
`parseMarkdownInline()` helper itself remains unescaped (mirrors
contact-mgmt.html's/account-mgmt.html's inline parser). `execReportActions()`
uses `goHome()` and `showLeadAIPage()` for its Back Home / Back to AI buttons.

---

## 7. Step-by-Step Checklist for Each Module

- [ ] Confirm `header h1 { font-size: ... }` (almost always `1.2rem`; `analytics-mgmt.html` is `1.25rem`) and use it for `.exec-report-title` / its mobile override
- [ ] Check whether `.md-link` already exists — add it only if missing
- [ ] Paste the `.exec-report*` CSS block (section 3), update the header comment's icon/module name
- [ ] Identify the module's inline-markdown parser (section 2 table); if none exists, extract one from `renderMarkdown()`'s replace chain
- [ ] Add `[label](url)` → `<a class="md-link">` as the **first** regex in that parser — do not add/remove `escapeHTML()`
- [ ] Add `parseDrivers()`, `execReportActions()`, `renderExecutiveReport()` before the markdown-rendering section, substituting `INLINE(...)`, `BACK_HOME_BTN_CLASS`, `BACK_HOME_FN`, `BACK_TO_AI_FN` per the reference table
- [ ] Wire `handleResponse()` (or `buildResponseHTML()` for analytics) per section 6
- [ ] Start local server (`python main.py`, `localhost:8000`) and fetch each "Executive" chip's `output` via the module's chat endpoint (e.g. `POST /activity-chat`) — save as JSON fixtures
- [ ] Run a Node `vm`-sandbox test: load the page's inline `<script>` blocks, call `renderExecutiveReport(fixture.output)` for every fixture, assert: non-null, contains `exec-report-title`, `exec-report-footer`, `Back Home`, `Back to AI`, and zero remaining literal `[label](url)` substrings
- [ ] Playwright-verify at least one chip end-to-end: submit the chip's question, confirm `.exec-report` renders with gradient title/section headers, confidence badge, drivers list, table(s), footer, and both action buttons

---

## 8. Key Design Rationale

| Decision | Why |
|---|---|
| Single shared CSS/JS pattern across all 10 modules | `format_exec_answer()` output is byte-identical in structure regardless of agent — one parser handles all of them |
| `renderExecutiveReport()` parses `####` sections itself, delegates bodies to `renderMarkdown()` | Sidesteps modules whose `renderMarkdown()` doesn't support h4 headers, without modifying `renderMarkdown()` |
| Returns `null` on structure mismatch | Lets `handleResponse()` fall back to the existing generic markdown path — fail-safe if the backend ever changes the format |
| `.exec-report-title` / `.exec-section-title` use a literal gradient (not a `:first-of-type` rule) | Most modules have no existing rainbow-gradient header rule to reuse; a self-contained class avoids depending on incidental global styles |
| `.exec-report-title` font-size matches `header h1` | Keeps the report's "letterhead" title visually consistent with the page's own branding/title size |
| `parseDrivers()` regex `/(\d+)\)\s*(.*?)(?=\s\d+\)\s|$)/gs` | Splits `"1) ... 2) ... 3) ..."` without requiring drivers to avoid containing the literal pattern `" N) "` |
| `execReportActions()` reuses the module's existing Back Home / Back to AI handlers | No new navigation logic — the report is just another "view" rendered into `responseArea` |
| Link-syntax regex added to inline parser, not a new function | `**Drill-down:** [label](href)` and any other markdown links in `format_exec_answer()` output need rendering; reusing the existing inline parser keeps bold/italic/code/link handling in one place |
| Do not add `escapeHTML()` to parsers that lack it | Out of scope — changing escaping behavior could affect other call sites of the same function |
| Section bodies get `.md-spacer:first-child/:last-child { display:none }` | Removes the leading/trailing blank-line spacer `renderMarkdown()` emits, which would otherwise create extra whitespace inside the bordered section card |
| `.exec-section-body .md-table-container { box-shadow:none; border:1px solid #eef0f3 }` | The outer `.exec-section` already has a border/shadow — nesting the table's own shadow looked doubled |
