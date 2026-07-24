# CRM Module Homepage Update — Template & Pattern Guide

> **Sources of truth:**
> - `contact-mgmt.html` — simple text search bar pattern (contacts, accounts, leads, opportunities, orders, etc.)
> - `product-mgmt.html` — Amazon hybrid search bar pattern (products, any module needing category+text search)
>
> This document captures every structural, CSS, and JS change needed to update CRM module home pages for consistent mobile/desktop behaviour across all modes (home, list, detail, forms, reports).

---

do not delete <meta name="viewport" content="width=device-width, initial-scale=1">

## Module Variants

Two home page search bar patterns exist. Choose the correct one for each module:

| Pattern | Search bar type | Home panel class | Used by |
|---|---|---|---|
| **Simple** | Single text input (`#home-search-wrapper`) | `#quick-ui` / `#quick-actions` | contact-chat, account-chat, leads, orders, etc. |
| **Amazon hybrid** | Category dropdown + text input + icon button (`.amz-search-bar`) | `.qs-panel` / `.qs-buttons` | product-chat, any module needing category-filtered search |

The CSS architecture, JS pattern, and `on-home-page` logic are **identical** between both variants. Only the search bar HTML/CSS and home panel class names differ.

---

## Mobile Behaviour Contract

| State | Header (search bar) | Input bar (bottom) | Title / Session info |
|---|---|---|---|
| **Home page** | ✅ Visible | ❌ Hidden | ❌ Hidden (`.desktop-only`) |
| **Search results** (from home search) | ✅ Visible (user can refine) | ❌ Hidden | ❌ Hidden |
| **Any other page** (form, detail, list) | ❌ Hidden | ❌ Hidden | ❌ Hidden |

**Desktop**: header always fully visible (title + search + session info). Input bar always visible.

---

## Architecture Overview

Mobile layout is driven by **three CSS paths**, applied in order:

| Path | Trigger | Covers |
|---|---|---|
| `@media (max-width: 640px)` | Narrow viewport | Primary mobile overrides — home page + all content modes |
| `@media (pointer: coarse), (max-width: 768px)` | Touch device OR narrow viewport | Comprehensive fallback — same content rules, wider net |
| `@media (max-width: 380px)` | Very small phones | Single-column button grid, reduced font sizes |

> **No `body.is-mobile` CSS block.** The `@media (pointer: coarse)` path handles JS-detected mobile devices wider than 640px, replacing the old JS-class CSS block entirely.

**Product-chat note:** An additional early `@media (pointer: coarse), (max-width: 768px)` block appears *before* the `@media (max-width: 640px)` block. It contains the product-specific search bar sizing and the base `.qs-panel` / `.qs-buttons` mobile rules. This early block is product-chat–specific; other modules only need the standard three paths.

---

## Summary of Key Changes vs Old Pattern

| Area | Old | New |
|---|---|---|
| Header layout | Single row: h1 + session-info | 3-column flex: title \| search \| session-info |
| Search bar location | Inside home panel (responseArea) | Inside `<header>` — always visible |
| Mobile header height | 96px, `padding: 0.88rem 0.85rem` | 52px, `padding: 0.25rem 0.3rem` |
| Button grid gap | `gap: 0.65rem` (shorthand) | `column-gap: 0.65rem; row-gap: 1.5rem` (separate) |
| Button margin-bottom | `0.65rem` | `1.5rem` |
| Button font size | `0.88rem` | **`1.035rem`** |
| Button height (mobile) | varies | **`62px`** (58px at ≤380px) |
| Voice hint / qs-hint row | Shown on mobile | **Hidden** via `div:last-child { display:none }` |
| Input bar (mobile) | Conditionally hidden | **Always hidden** — unconditional `.input-bar { display:none !important }` |
| Non-home content CSS | Home page only | **All modes**: cards, forms, grids, reports, timelines |
| Mobile CSS architecture | `body.is-mobile` JS-class block | **Removed** — `@media (pointer: coarse), (max-width: 768px)` covers it |
| Header show/hide mechanism | `hideHeader()` / `showHeader()` DOM manipulation | `document.body.classList.remove/add('on-home-page')` |
| `detectDevice()` threshold | `>= 5` or old 5-signal | **`>= 3`**, 8 signals, sessionStorage override |
| Home panel margin-top | `4.5rem` | **`1rem`** |

---

## 1. CSS Changes

### Base styles (outside all media queries)

```css
/* ── Desktop-only columns: visible by default, hidden on mobile via media queries ── */
.desktop-only {
    display: flex;
}

/* ── Home panel: reduced top margin (search is now in header) ── */
/* Simple pattern: */
#quick-ui {
    margin-top: 1rem;
    text-align: center;
    max-width: 900px;
    margin-left: auto;
    margin-right: auto;
}

/* Amazon hybrid pattern: */
.qs-panel {
    max-width: 820px;
    margin: 1rem auto 2rem;
    text-align: center;
}

/* Desktop button grid (Amazon hybrid — 3 columns on desktop) */
.qs-buttons {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.6rem;
    margin-bottom: 1.25rem;
    max-width: 554px;
    margin-left: auto;
    margin-right: auto;
}
.qs-buttons .unified-action-btn { width: 100%; min-width: 0; max-width: none; }
```

---

### A. `@media (max-width: 640px)` — Primary mobile block

```css
/* ── Header: slim single row on mobile ── */
header {
    flex-wrap: nowrap;
    padding: 0.25rem 0.3rem;
    gap: 0;
    min-height: 52px;
}
header h1 {
    font-size: 1.27rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* ── Title + session info hidden — #app-header scope for specificity ── */
#app-header .desktop-only {
    display: none !important;
    flex: 0 !important;
    width: 0 !important;
    overflow: hidden !important;
    padding: 0 !important;
    margin: 0 !important;
}

/* ── Center search column expands to fill full header width ── */
#header-search-center {
    flex: 1 1 100% !important;
    width: 100% !important;
    max-width: 100% !important;
}

/* ── SIMPLE pattern: search wrapper fills center column ── */
#home-[module]-search {
    width: 100% !important;
    max-width: calc(100vw - 0.6rem) !important;
    margin: 0 !important;
}
#home-search-wrapper {
    min-height: 62px !important;
    padding: 0.2rem 1.25rem !important;
    border: 3px solid [MODULE_COLOR] !important;
    border-radius: 25px !important;
    box-shadow: 0 3px 14px rgba([MODULE_COLOR_RGB], 0.22) !important;
    box-sizing: border-box;
}
#home-search-wrapper:focus-within {
    min-height: 66px !important;
    border-color: #7C3AED !important;
    box-shadow: 0 4px 20px rgba(124, 58, 237, 0.28),
                0 0 0 3px rgba(124, 58, 237, 0.10) !important;
    padding: 0.2rem 0.2rem !important;
}
#home-search-input { font-size: 1.1rem !important; }

/* ── AMAZON HYBRID pattern: .amz-search-bar fills center column ── */
#home-search-container {
    width: 100% !important;
    max-width: calc(100vw - 0.6rem) !important;
    margin: 0 !important;
}
.home-search-bar-wrap { width: 100%; max-width: 100%; margin: 0; }
.amz-search-bar {
    height: 62px !important;
    border-radius: 24px !important;
    border: 3px solid #2563EB !important;
    box-shadow: 0 3px 14px rgba(37, 99, 235, 0.22) !important;
}
.amz-search-wrapper:focus-within .amz-search-bar {
    height: 66px !important;
    border-color: #7C3AED !important;
    box-shadow: 0 4px 20px rgba(124, 58, 237, 0.28),
                0 0 0 3px rgba(124, 58, 237, 0.10) !important;
}
.amz-search-input { font-size: 1.1rem !important; }
.amz-cat-select   { font-size: 0.88rem !important; min-width: 105px !important; max-width: 130px !important; }

/* ── Homepage fills viewport ── */
.response { display: flex; flex-direction: column; padding-top: 0 !important; }

/* ── SIMPLE pattern: #quick-ui golden-ratio fill ── */
#quick-ui {
    margin-top: 0 !important;
    flex: 1;
    min-height: calc(100dvh - 160px);
    min-height: calc(100svh - 160px);
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    align-items: stretch;
    padding-top: clamp(0.48rem, calc(14.83dvh - 38.4px), 6rem);
    padding-left: 0.3rem;
    padding-right: 0.3rem;
    padding-bottom: 1rem;
    width: 100%;
    max-width: 100% !important;
    box-sizing: border-box;
}

/* ── AMAZON HYBRID pattern: .qs-panel golden-ratio fill ── */
.qs-panel {
    min-height: calc(100dvh - 160px);
    min-height: calc(100svh - 160px);
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    align-items: stretch;
    margin: 0 auto !important;
    padding-top: clamp(0.48rem, calc(14.83dvh - 38.4px), 6rem);
    padding-left: 0.3rem;
    padding-right: 0.3rem;
    padding-bottom: 1rem;
    gap: 0;
    width: 100%;
    max-width: 100% !important;
    box-sizing: border-box;
    flex: 1;
}

/* ── Input bar: ALWAYS hidden on mobile (unconditional) ── */
.input-bar { display: none !important; }

/* ── Hide header on non-home pages ── */
body:not(.on-home-page) #app-header { display: none !important; }

/* ── SIMPLE pattern: #quick-actions 2-column CSS grid ── */
#quick-actions { width: 100%; max-width: 100% !important; text-align: left !important; }
#quick-actions > div:not(:last-child) {
    display: grid !important;
    grid-template-columns: 1fr 1fr !important;
    column-gap: 0.65rem !important;
    row-gap: 1.5rem !important;
    margin-bottom: 1.5rem !important;
    justify-content: unset !important;
    flex-wrap: unset !important;
}
#quick-actions .unified-action-btn {
    width: 100% !important;
    min-width: unset !important;
    height: 62px !important;
    font-size: 1.035rem !important;
    padding: 0.5rem 0.6rem !important;
    border-radius: 20px !important;
    box-sizing: border-box;
}
/* Hide voice hint row (last child div) */
#quick-actions > div:last-child { display: none !important; }

/* ── AMAZON HYBRID pattern: .qs-buttons 2-column CSS grid ── */
.qs-buttons {
    display: grid !important;
    grid-template-columns: 1fr 1fr !important;
    column-gap: 0.65rem !important;
    row-gap: 1.5rem !important;
    margin-bottom: 1.5rem !important;
    max-width: 100% !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
}
.qs-buttons .unified-action-btn {
    width: 100% !important;
    min-width: unset !important;
    max-width: none !important;
    height: 62px !important;
    font-size: 1.035rem !important;
    padding: 0.5rem 0.6rem !important;
    border-radius: 20px !important;
    box-sizing: border-box;
}

/* ── Card bottom-right buttons (list/detail views) ── */
.card-actions-bottom-right {
    flex-wrap: wrap;
    justify-content: flex-start;
    gap: 1rem !important;
}
.card-actions-bottom-right .unified-action-btn {
    flex: 1 !important;
    min-width: 0 !important;
    height: 44px !important;
    font-size: 0.84rem !important;
    padding: 0.3rem 0.55rem !important;
    border-radius: 20px !important;
}

/* ── Action buttons row (reports / result views) ── */
.action-buttons {
    flex-direction: row !important;
    flex-wrap: nowrap;
    gap: 1.5rem !important;
    justify-content: flex-start;
}
.action-buttons .unified-action-btn {
    flex: 1 !important;
    width: auto !important;
    min-width: 0 !important;
    max-width: none !important;
    border-radius: 20px !important;
    font-size: 0.84rem !important;
}

/* ── Form action row (create/update forms) ── */
/* Simple pattern: */
.form-actions-row { flex-direction: column; align-items: stretch; }
.form-actions-row .btn-group-left { flex-direction: row; flex-wrap: wrap; }
.form-actions-row .unified-action-btn { flex: 1; width: auto !important; min-width: 0 !important; }

/* Amazon hybrid pattern (.pf-actions): */
.pf-actions {
    flex-direction: row !important;
    flex-wrap: nowrap;
    padding: 0.85rem 0.75rem;
    gap: 1.5rem !important;
    align-items: stretch;
    justify-content: flex-start;
}
.pf-actions .pf-btn,
.pf-actions .unified-action-btn {
    flex: 1 !important;
    min-width: 0 !important;
    width: auto !important;
    max-width: none !important;
    justify-content: center;
    border-radius: 20px !important;
    font-size: 0.84rem;
    padding: 0 0.5rem;
}

/* ── Form grids: collapse to 1 column ── */
.form-grid, .pf-grid-2, .pf-grid-3, .pf-grid-4 { grid-template-columns: 1fr !important; }
.pf-card-header { padding: 0.9rem 1rem; }
.pf-card-body   { padding: 1rem 0.9rem; gap: 1.1rem; }

/* ── Form inputs: full width, 1rem prevents iOS auto-zoom ── */
.form-group input, .form-group select,
.pf-input, .pf-select, .pf-textarea,
input[type="text"].pf-input, input[type="number"].pf-input,
select.pf-select, textarea.pf-textarea {
    width: 100% !important;
    font-size: 1rem !important;
    box-sizing: border-box;
}

/* ── Textarea: prevent iOS auto-zoom ── */
textarea { font-size: 0.92rem; }

/* ── Address / form sub-grids ── */
.financial-grid, .addresses-grid, .form-grid { grid-template-columns: 1fr; }
.address-grid              { grid-template-columns: 1fr !important; }
.address-grid-city-row     { grid-template-columns: 1fr !important; }
.address-row-grouped       { grid-template-columns: 1fr !important; grid-column: 1 / -1; }
.addr-street-grid          { grid-template-columns: 1fr !important; }
.addr-city-grid            { grid-template-columns: 1fr 1fr !important; }
.readonly-grid             { grid-template-columns: 1fr !important; }

/* ── Stats / KPI grids ── */
.stats-grid { grid-template-columns: repeat(2, 1fr); }
.kpi-grid   { grid-template-columns: repeat(2, 1fr); }

/* ── Summary & inventory tables: horizontal scroll (product-chat) ── */
.summary-table,
.response .inventory-table {
    display: block;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    white-space: nowrap;
}

/* ── Record detail lines ── */
.response .line .label { width: 90px; font-size: 0.85rem; }

/* ── Activity timestamps: stack vertically ── */
.activity-timestamps { flex-direction: column; gap: 0.2rem; }
.activity-card:hover { transform: none; }

/* ── Footer stats ── */
.footer-stats span { margin-right: 0.75rem; }

/* ── Timeline ── */
.timeline-container         { padding-left: 1.5rem; }
.timeline-container::before { left: 0.5rem; }
.activity-card::before      { left: -1.35rem; }
.activity-card              { padding: 0.65rem 0.75rem; }

/* ── Non-home response padding ── */
body:not(.on-home-page) .response { padding: 0.85rem 0.7rem; }
```

---

### B. `@media (max-width: 380px)` — Very small phones

```css
header h1 { font-size: 0.78rem; }

/* Simple pattern: */
#quick-actions > div:not(:last-child) { grid-template-columns: 1fr !important; }
#quick-actions .unified-action-btn    { height: 58px !important; font-size: 0.92rem !important; }

/* Amazon hybrid pattern: */
.qs-buttons { grid-template-columns: 1fr !important; }
.qs-buttons .unified-action-btn { height: 58px !important; font-size: 0.92rem !important; }

.stats-grid { grid-template-columns: 1fr; }
.kpi-grid   { grid-template-columns: 1fr; }
.pf-card-body { padding: 0.75rem 0.65rem; }  /* Amazon hybrid only */
```

---

### C. `@media (min-width: 641px)` — Desktop reset

```css
.response { display: block; }
```

---

### D. `@media (pointer: coarse), (max-width: 768px)` — Touch / wide-mobile fallback

This block fires on **touch devices regardless of viewport width** AND on viewports ≤768px. It replaces the old `body.is-mobile` CSS block entirely. Keep `#app-header { display: flex !important; }` as the first rule to guarantee the header always renders correctly on touch devices.

```css
#app-header { display: flex !important; }  /* ensure header always renders as flex */

body.on-home-page .response   { padding-top: 0.1rem !important; }

/* Simple pattern: */
body.on-home-page #quick-ui   { margin-top: 0 !important; }
/* Amazon hybrid pattern: */
body.on-home-page .qs-panel   { margin-top: 0 !important; }

header {
    flex-wrap: nowrap;
    padding: 0.25rem 0.3rem;
    gap: 0;
    min-height: 52px;
}
header h1 { font-size: 1.27rem; }

.desktop-only { display: none !important; }

#header-search-center {
    flex: 1 1 100% !important;
    width: 100% !important;
}

/* ── SIMPLE pattern ── */
#home-[module]-search {
    width: 99% !important;
    max-width: 99% !important;
    margin: 0 auto !important;
}
#home-search-wrapper {
    min-height: 62px !important;
    padding: 0.3rem 1.25rem !important;
    border: 3px solid [MODULE_COLOR] !important;
    border-radius: 24px !important;
}
#home-search-wrapper:focus-within { min-height: 66px !important; }
#home-search-input { font-size: 1.1rem !important; }

/* ── AMAZON HYBRID pattern ── */
#home-search-container {
    width: 99% !important;
    max-width: 99% !important;
    margin: 0 auto !important;
}
.amz-search-bar {
    height: 62px !important;
    border-radius: 24px !important;
    border: 3px solid #2563EB !important;
    box-shadow: 0 3px 14px rgba(37, 99, 235, 0.22) !important;
}
.amz-search-wrapper:focus-within .amz-search-bar {
    height: 66px !important;
    border-color: #7C3AED !important;
    box-shadow: 0 4px 20px rgba(124, 58, 237, 0.28),
                0 0 0 3px rgba(124, 58, 237, 0.10) !important;
}
.amz-search-input { font-size: 1.1rem !important; }
.amz-cat-select   { font-size: 0.88rem !important; min-width: 105px !important; max-width: 130px !important; }

/* ── Input bar: always hidden ── */
body.on-home-page .input-bar       { display: none !important; }
body:not(.on-home-page) .input-bar { display: none !important; }
body:not(.on-home-page) #app-header { display: none !important; }

/* ── Homepage layout ── */
.response { display: flex; flex-direction: column; padding-top: 0 !important; }

/* Simple pattern: */
#quick-ui {
    margin-top: 0 !important;
    flex: 1;
    min-height: calc(100dvh - 160px);
    min-height: calc(100svh - 160px);
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    align-items: stretch;
    padding-top: clamp(0.48rem, calc(14.83dvh - 38.4px), 6rem) !important;
    padding-left: 0.3rem;
    padding-right: 0.3rem;
    padding-bottom: 1rem;
    width: 100%;
    max-width: 100% !important;
    box-sizing: border-box;
}

/* Amazon hybrid pattern: */
.qs-panel {
    min-height: calc(100dvh - 160px);
    min-height: calc(100svh - 160px);
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    align-items: stretch;
    margin: 0 auto !important;
    padding-top: clamp(0.48rem, calc(14.83dvh - 38.4px), 6rem) !important;
    padding-left: 0.3rem;
    padding-right: 0.3rem;
    padding-bottom: 1rem;
    width: 100%;
    max-width: 100% !important;
    box-sizing: border-box;
    flex: 1;
}

/* ── Simple pattern: #quick-actions 2-column grid ── */
#quick-actions > div:not(:last-child) {
    display: grid !important;
    grid-template-columns: 1fr 1fr !important;
    column-gap: 0.65rem !important;
    row-gap: 1.5rem !important;
    margin-bottom: 1.5rem !important;
    justify-content: unset !important;
    flex-wrap: unset !important;
}
#quick-actions .unified-action-btn {
    width: 100% !important; min-width: unset !important;
    height: 62px !important; font-size: 1.035rem !important;
    padding: 0.5rem 0.6rem !important; border-radius: 20px !important;
    box-sizing: border-box;
}
#quick-actions > div:last-child { display: none !important; }

/* ── Amazon hybrid pattern: .qs-buttons 2-column grid ── */
.qs-buttons {
    display: grid !important;
    grid-template-columns: 1fr 1fr !important;
    column-gap: 0.65rem !important;
    row-gap: 1.5rem !important;
    margin-bottom: 1.5rem !important;
    max-width: 100% !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
}
.qs-buttons .unified-action-btn {
    width: 100% !important; min-width: unset !important; max-width: none !important;
    height: 62px !important; font-size: 1.035rem !important;
    padding: 0.5rem 0.6rem !important; border-radius: 20px !important;
    box-sizing: border-box;
}

/* ── Non-home content rules (apply to both patterns) ── */
.card-actions-bottom-right {
    flex-wrap: wrap;
    justify-content: center !important;
    gap: 1rem !important;
}
.card-actions-bottom-right .unified-action-btn {
    flex: none !important;
    width: auto !important;
    min-width: 110px !important;
    height: 44px !important;
    font-size: 0.84rem !important;
    padding: 0.3rem 0.55rem !important;
    border-radius: 20px !important;
}
.action-buttons { flex-direction: row !important; flex-wrap: nowrap; gap: 1.5rem !important; }
.action-buttons .unified-action-btn {
    flex: 1 !important; width: auto !important;
    min-width: 0 !important; max-width: none !important; border-radius: 20px !important;
}

/* Simple pattern form row: */
.form-actions-row              { flex-direction: column; align-items: stretch; }
.form-actions-row .btn-group-left { flex-direction: row; flex-wrap: wrap; }
.form-actions-row .unified-action-btn { flex: 1; width: auto !important; min-width: 0 !important; }

/* Amazon hybrid pattern form row (.pf-actions): */
.pf-actions { flex-direction: row !important; flex-wrap: nowrap; gap: 1.5rem !important; align-items: stretch; }
.pf-actions .pf-btn,
.pf-actions .unified-action-btn { flex: 1 !important; min-width: 0 !important; width: auto !important; max-width: none !important; border-radius: 20px !important; }

.form-grid, .pf-grid-2, .pf-grid-3, .pf-grid-4 { grid-template-columns: 1fr !important; }
.pf-card-body   { padding: 1rem 0.9rem; }
.pf-card-header { padding: 0.9rem 1rem; }

.form-group input, .form-group select,
.pf-input, .pf-select, .pf-textarea {
    width: 100% !important; font-size: 1rem !important; box-sizing: border-box;
}
textarea { font-size: 0.92rem; }

.financial-grid, .addresses-grid, .address-grid { grid-template-columns: 1fr; }
.addr-street-grid { grid-template-columns: 1fr !important; }
.addr-city-grid   { grid-template-columns: 1fr 1fr !important; }
.readonly-grid    { grid-template-columns: 1fr !important; }
.stats-grid { grid-template-columns: repeat(2, 1fr); }
.kpi-grid   { grid-template-columns: repeat(2, 1fr); }

.summary-table,
.response .inventory-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; white-space: nowrap; }

.response .line .label { width: 90px; font-size: 0.85rem; }
.activity-timestamps { flex-direction: column; gap: 0.2rem; }
.activity-card:hover { transform: none; }
.footer-stats span { margin-right: 0.75rem; }
```

> **No `body.is-mobile` CSS block needed.** The `@media (pointer: coarse)` condition fires on all real touch devices regardless of viewport width.

---

## 2. HTML Changes

### A. Header — 3-column layout

**CRITICAL**: Do NOT put `display:flex` inside the inline `style` of `.desktop-only` divs.

```html
<header id="app-header">
    <!-- Left: title (desktop only — NO display:flex in inline style) -->
    <div class="desktop-only" style="flex:1; justify-content:flex-start; align-items:center;">
        <h1>[Icon] [Short Name]</h1>
    </div>

    <!-- Center: search bar (always in DOM; fills full width on mobile) -->
    <div id="header-search-center" style="flex:2; display:flex; justify-content:center; align-items:center; width:100%;">

        <!-- ══ SIMPLE pattern — single text input ══ -->
        <div id="home-[module]-search" style="margin:0 auto; width:750px; max-width:825px;">
            <div id="home-search-wrapper"
                style="position:relative; display:flex; align-items:center;
                       background:#fff; border:2px solid [MODULE_COLOR]; border-radius:24px;
                       padding:0.35rem 1.1rem; min-height:40px;
                       box-shadow:0 2px 10px rgba([MODULE_COLOR_RGB],0.18);
                       transition:box-shadow 0.22s ease, border-color 0.22s ease;"
                onfocusin="this.style.borderColor='#7C3AED'; this.style.boxShadow='0 4px 20px rgba(124,58,237,0.28), 0 0 0 3px rgba(124,58,237,0.10)';"
                onfocusout="this.style.borderColor='[MODULE_COLOR]'; this.style.boxShadow='0 2px 10px rgba([MODULE_COLOR_RGB],0.18)';">
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none"
                    stroke="#9aa0a6" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"
                    style="flex-shrink:0; margin-right:0.7rem;">
                    <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                </svg>
                <input type="text" id="home-search-input"
                    placeholder="Search [items] by name, email…" autocomplete="off"
                    spellcheck="false" maxlength="120"
                    oninput="homeSearchDebounce(this.value)" onkeydown="homeSearchKeyNav(event)"
                    style="flex:1; border:none; outline:none; font-size:1rem;
                           font-family:'Segoe UI',sans-serif; color:#202124; background:transparent;">
                <span id="home-search-spinner" style="display:none; margin-left:0.5rem;"><!-- spinner svg --></span>
                <button id="home-search-clear" onclick="clearHomeSearch()"
                    style="display:none; background:none; border:none; cursor:pointer;
                           color:#9aa0a6; font-size:1.1rem; padding:0 0.3rem; margin-left:0.3rem;"
                    title="Clear search">&#10005;</button>
            </div>
            <div id="home-search-dropdown"
                style="display:none; position:absolute; z-index:9999;
                       background:#fff; border:1px solid #dfe1e5; border-radius:12px;
                       box-shadow:0 8px 28px rgba(0,0,0,0.18); margin-top:4px;
                       max-height:360px; overflow-y:auto; text-align:left;
                       width:min(600px, calc(100vw - 2rem));">
            </div>
        </div>

        <!-- ══ AMAZON HYBRID pattern — category dropdown + text + button ══ -->
        <div id="home-search-container" style="margin:0 auto; width:750px; max-width:825px;">
            <div class="home-search-bar-wrap" style="max-width:100%; margin:0;">
                <div class="amz-search-wrapper">
                    <div class="amz-search-bar" id="amzSearchBar">
                        <!-- LEFT: category dropdown -->
                        <select class="amz-cat-select amz-cat-loading" id="amzCatSelect" title="Search by category">
                            <option value="">All Categories</option>
                        </select>
                        <!-- CENTER: text input -->
                        <input type="text" class="amz-search-input" id="homeProductSearchInput"
                               placeholder="Search [items]…" autocomplete="off" aria-label="Search">
                        <!-- RIGHT: search button -->
                        <button class="amz-search-btn" id="amzSearchBtn" title="Search" aria-label="Search">
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
                                 stroke="currentColor" stroke-width="2.4"
                                 stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="11" cy="11" r="8"/>
                                <line x1="21" y1="21" x2="16.65" y2="16.65"/>
                            </svg>
                        </button>
                    </div>
                    <div class="amz-suggestions" id="amzSuggestions"></div>
                </div>
            </div>
        </div>

    </div>

    <!-- Right: session info (desktop only — NO display:flex in inline style) -->
    <div class="desktop-only" style="flex:1; justify-content:flex-end; align-items:center;">
        <div class="session-info">
            Session: <span id="sessionId">...</span>
            <button onclick="resetSession()">Reset</button>
        </div>
    </div>
</header>
```

---

### B. Home panel HTML — `getHomeHTML()` / `buildHomeHTML()`

**Simple pattern** — row divs inside `#quick-actions` each become a 2-column grid row on mobile. Voice hint div is last child (hidden on mobile by CSS):

```html
<div class="response" id="responseArea">
    <div id="quick-ui" style="margin-top:1rem; text-align:center; max-width:900px; margin-left:auto; margin-right:auto;">
        <div id="quick-actions" style="margin:0.5rem auto; max-width:850px; text-align:center;">
            <!-- ROW 1 — each div = one 2-button row on mobile -->
            <div style="display:flex; justify-content:center; gap:0.75rem; margin-bottom:1.125rem; flex-wrap:wrap;">
                <button class="unified-action-btn" data-action="[action1]">[Label 1]</button>
                <button class="unified-action-btn" data-action="[action2]">[Label 2]</button>
                <button class="unified-action-btn" data-action="[action3]">[Label 3]</button>
                <button class="unified-action-btn" data-action="[action4]">[Label 4]</button>
            </div>
            <!-- ROW 2 -->
            <div style="display:flex; justify-content:center; gap:0.75rem; flex-wrap:wrap;">
                <button class="unified-action-btn" data-action="[action5]">[Label 5]</button>
                <button class="unified-action-btn" data-action="[action6]">[Label 6]</button>
                <button class="unified-action-btn" data-action="[action7]">[Label 7]</button>
                <button class="unified-action-btn" data-action="[action8]">[Label 8]</button>
            </div>
            <!-- Voice hint — hidden on mobile (last-child rule), visible on desktop -->
            <div style="margin-top:2rem; padding:0.6rem 1rem; background:#f0f8ff; border-radius:8px; font-size:0.84rem; color:#555; display:inline-flex; align-items:center; gap:0.5rem;">
                🎙️ <strong>Voice Input:</strong> Click the microphone and speak!
            </div>
        </div>
    </div>
</div>
```

**Amazon hybrid pattern** — all buttons are siblings inside `.qs-buttons`; CSS grid handles the 2-column layout on mobile automatically:

```html
<div id="response" class="response">
    <!-- Home panel injected on load by getHomeHTML() -->
</div>

<!-- getHomeHTML() returns: -->
<div class="qs-panel fade-in">
    <div class="qs-buttons">
        <!-- All buttons are flat siblings — no row divs needed -->
        <button class="unified-action-btn" onclick="[action1]">[Label 1]</button>
        <button class="unified-action-btn" onclick="[action2]">[Label 2]</button>
        <button class="unified-action-btn" onclick="[action3]">[Label 3]</button>
        <button class="unified-action-btn" onclick="[action4]">[Label 4]</button>
        <button class="unified-action-btn" onclick="[action5]">[Label 5]</button>
        <button class="unified-action-btn" onclick="[action6]">[Label 6]</button>
    </div>
</div>
```

> **Key difference:** Simple pattern uses row `<div>` wrappers so `div:last-child { display:none }` hides the voice hint. Amazon hybrid pattern uses flat button siblings inside `.qs-buttons`; the CSS grid directly controls layout — no row divs or voice hint needed.

---

## 3. JS Changes

### A. `hideQuickUI()` / `showQuickUI()` (simple pattern)

```js
function hideQuickUI() {
    const quickUI = document.getElementById('quick-ui');
    if (quickUI) quickUI.style.display = 'none';
    document.body.classList.remove('on-home-page');
}
function showQuickUI() {
    const quickUI = document.getElementById('quick-ui');
    if (quickUI) quickUI.style.display = 'block';
    document.body.classList.add('on-home-page');
}
```

### A. `hideHeader()` / `showHeader()` (Amazon hybrid pattern — kept as compatibility wrappers)

```js
// Kept for backward compatibility — delegates to on-home-page class
function hideHeader() { document.body.classList.remove('on-home-page'); }
function showHeader()  { document.body.classList.add('on-home-page');    }
```

> Existing callsites using `hideHeader()` / `showHeader()` continue to work without changes. New code should use `document.body.classList.remove/add('on-home-page')` directly.

---

### B. `goHome()` / `goBackHome()` — restore home state + clear search

**Simple pattern (`goHome()`):**
```js
function goHome() {
    const responseArea = document.getElementById('responseArea');
    responseArea.innerHTML = buildHomeHTML();
    attachQuickActionHandlers();
    document.getElementById('message').value = '';
    document.body.classList.add('on-home-page');
    // Clear persistent header search
    const inp = document.getElementById('home-search-input');
    if (inp) inp.value = '';
    const clr = document.getElementById('home-search-clear');
    if (clr) clr.style.display = 'none';
    const drop = document.getElementById('home-search-dropdown');
    if (drop) drop.style.display = 'none';
}
```

**Amazon hybrid pattern (`goBackHome()`):**
```js
function goBackHome() {
    document.body.classList.add('on-home-page');
    responseBox.innerHTML = getHomeHTML();
    _initHomeSearch();   // re-wires the persistent header search bar elements
    // Clear persistent header search
    const inp = document.getElementById('homeProductSearchInput');
    if (inp) inp.value = '';
    const drop = document.getElementById('amzSuggestions');
    if (drop) { drop.classList.remove('open'); drop.innerHTML = ''; }
}
```

> `_initHomeSearch()` must still be called on `goBackHome()` for the Amazon hybrid pattern. The search bar elements live in the permanent header DOM but their event listeners are set up by `_wireAmzBar()` — calling `_initHomeSearch()` re-wires them after a navigation cycle.

---

### C. Home search function — keep header visible during results

**Simple pattern (`runHomeSearch()`):**
```js
async function runHomeSearch(query) {
    hideQuickUI();
    document.body.classList.add('on-home-page');  // keep header visible
    try {
        // ... fetch and render results ...
        document.body.classList.add('on-home-page');  // re-add in success branch too
    } catch (err) {
        document.body.classList.add('on-home-page');  // and in error branch
    }
}
```

**Amazon hybrid pattern (`_homeSearchDirect()`):**
```js
async function _homeSearchDirect(query) {
    document.body.classList.remove('on-home-page');
    responseBox.innerHTML = `<div class="loading-message">...</div>`;
    try {
        // ... fetch and render results ...
        document.body.classList.add('on-home-page');  // keep header/search visible
    } catch (err) {
        document.body.classList.add('on-home-page');  // and in error branch
    }
}
```

---

### D. `detectDevice()` IIFE

```js
(function detectDevice() {
    const ua     = navigator.userAgent || navigator.vendor || window.opera || '';
    const sw     = window.screen?.width  || window.innerWidth  || 1280;
    const sh     = window.screen?.height || window.innerHeight || 800;
    const minDim = Math.min(sw, sh);

    const uaScore     = /android|webos|iphone|ipad|ipod|blackberry|iemobile|opera mini|mobile|tablet|touch|kindle|silk|fennec/i.test(ua) ? 3 : 0;
    const osScore     = /iphone|ipad|ipod|android|windows phone|iemobile/i.test(ua) ? 2 : 0;
    const screenScore = minDim < 640 ? 3 : minDim < 768 ? 2 : minDim < 1024 ? 1 : 0;
    const touchScore  = (('ontouchstart' in window) || navigator.maxTouchPoints > 1) ? 1 : 0;
    // matchMedia signals — reliable in browser dev-tools responsive mode
    const mqTouch  = window.matchMedia && window.matchMedia('(pointer: coarse)').matches ? 2 : 0;
    const mqHover  = window.matchMedia && window.matchMedia('(hover: none)').matches     ? 1 : 0;
    const mqNarrow = window.matchMedia && window.matchMedia('(max-width: 768px)').matches ? 2 : 0;
    let netScore = 0;
    try {
        const c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
        if (c && (c.type === 'cellular' || /^[23]g$/.test(c.effectiveType || ''))) netScore = 1;
    } catch (_) { }

    const total = uaScore + osScore + screenScore + touchScore + mqTouch + mqHover + mqNarrow + netScore;

    // URL param OR sessionStorage override — useful for testing
    const params   = new URLSearchParams(window.location.search);
    const urlMob   = params.get('isMobile');
    const sessMob  = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('crm_is_mobile') : null;
    const forceMob = urlMob !== null ? urlMob : sessMob;

    const isMobile = forceMob !== null ? (forceMob === 'true') : (total >= 3);

    document.body.classList.add(isMobile ? 'is-mobile' : 'is-desktop');
    document.body.classList.add('on-home-page');   // start on home page
    window.__device = { isMobile, isDesktop: !isMobile, minDim };

    let _resizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(_resizeTimer);
        _resizeTimer = setTimeout(() => {
            if (!window.__device?.isMobile) return;
            document.body.classList.toggle('narrow-mobile',
                Math.min(window.innerWidth, window.innerHeight) < 380);
        }, 150);
    });
})();
```

---

### E. `resetSession()` event handler

```js
resetBtn.addEventListener('click', () => {
    sessionId = generateSessionId();
    // ... persist sessionId ...
    document.body.classList.add('on-home-page');
    responseBox.innerHTML = getHomeHTML();   // or buildHomeHTML()
    _initHomeSearch();                       // Amazon hybrid only — omit for simple pattern
    messageBox.value = '';
});
```

---

### F. Form submit + `_directQuery` / `fillAndSend`

Every function that navigates away from home must remove `on-home-page`:

```js
// Form submit
document.body.classList.remove('on-home-page');

// _directQuery / _directOperation
document.body.classList.remove('on-home-page');

// fillAndSend / createNewProductForm / createNewForm
document.body.classList.remove('on-home-page');
```

---

## 4. Step-by-Step Checklist for Each Module

**HTML:**
- [ ] Add `id="app-header"` to `<header>`
- [ ] Replace header content with 3-column flex layout
- [ ] Left `.desktop-only` div: **no `display:flex` in inline style**
- [ ] Center `#header-search-center` div: contains the correct search bar for this module
- [ ] Right `.desktop-only` div: **no `display:flex` in inline style**
- [ ] Shorten h1 to icon + module name only
- [ ] **Simple:** move `div#home-[module]-search` from `#quick-ui` into header; update `buildHomeHTML()` to omit it
- [ ] **Amazon hybrid:** move `.amz-search-wrapper` from `getHomeHTML()` into header; update `getHomeHTML()` to only return `.qs-panel` with `.qs-buttons`
- [ ] Set home panel `margin-top` to `1rem`
- [ ] **Simple:** keep row `<div>` structure inside `#quick-actions`; voice hint div is last child
- [ ] **Amazon hybrid:** flat button siblings inside `.qs-buttons`; no row divs needed

**CSS — Base styles:**
- [ ] Add `.desktop-only { display: flex; }` base rule
- [ ] Update home panel margin: `1rem auto 2rem` (not `4.5rem`)

**CSS — `@media (max-width: 640px)`:**
- [ ] `header { flex-wrap:nowrap; padding:0.25rem 0.3rem; gap:0; min-height:52px; }`
- [ ] `#app-header .desktop-only` collapse rule (6 properties)
- [ ] `#header-search-center { flex:1 1 100% !important; width:100% !important; max-width:100% !important; }`
- [ ] **Simple:** `#home-[module]-search` width + `#home-search-wrapper` 62px/3px/25px/module-colour
- [ ] **Amazon hybrid:** `#home-search-container` width + `.amz-search-bar` 62px/3px/24px/#2563EB + focus-within override
- [ ] **Simple:** `#quick-ui` golden-ratio fill block
- [ ] **Amazon hybrid:** `.qs-panel` golden-ratio fill block
- [ ] `.input-bar { display:none !important; }` — unconditional
- [ ] `body:not(.on-home-page) #app-header { display:none !important; }`
- [ ] Button grid: `column-gap:0.65rem; row-gap:1.5rem; margin-bottom:1.5rem`
- [ ] Button: `height:62px; font-size:1.035rem`
- [ ] **Simple:** `#quick-actions > div:last-child { display:none !important; }` — hides voice hint
- [ ] All non-home content rules: card actions, action-buttons, form-actions-row/.pf-actions, form grids, inputs, textarea, stats/KPI grids, financial grid, address sub-grids, record labels, activity timestamps, tables, footer stats, timeline

**CSS — `@media (max-width: 380px)`:**
- [ ] `header h1 { font-size: 0.78rem; }`
- [ ] Button grid single-column + `height:58px; font-size:0.92rem`
- [ ] `.stats-grid` / `.kpi-grid` single-column
- [ ] `.pf-card-body` tighter padding (Amazon hybrid only)

**CSS — `@media (pointer: coarse), (max-width: 768px)`:**
- [ ] `#app-header { display: flex !important; }` — first rule
- [ ] `body.on-home-page .response` / `body.on-home-page #quick-ui` or `.qs-panel` resets
- [ ] Header slim rules, `.desktop-only` hide, `#header-search-center` expand
- [ ] Correct search bar rules for this module's variant
- [ ] Both `.input-bar` hides (scoped and unscoped)
- [ ] Full `#quick-ui` or `.qs-panel` golden-ratio block with `!important`
- [ ] Full button grid (same values as 640px block)
- [ ] All non-home content rules (same as 640px block)

**CSS — `body.is-mobile` block: NOT needed.**

**JS:**
- [ ] `detectDevice()` — 8 signals, threshold `>= 3`, sessionStorage + URL override, adds `on-home-page`
- [ ] **Simple:** `hideQuickUI()` removes `on-home-page`; `showQuickUI()` adds it
- [ ] **Amazon hybrid:** `hideHeader()` / `showHeader()` kept as wrappers delegating to classList; all direct callsites updated to `document.body.classList.remove/add('on-home-page')`
- [ ] `goHome()` / `goBackHome()` adds `on-home-page` + clears search input + clears dropdown
- [ ] **Amazon hybrid:** `goBackHome()` still calls `_initHomeSearch()` to re-wire event listeners
- [ ] Home search function re-adds `on-home-page` in **both** success and error branches
- [ ] `resetSession()` adds `on-home-page` before rendering home HTML
- [ ] All navigation-away functions (form submit, `_directQuery`, `fillAndSend`, `createNewProductForm`) remove `on-home-page`

---

## 5. Key Design Rationale

| Decision | Why |
|---|---|
| Search bar in header | Always accessible; mirrors Google-style UX — never buried in content area |
| `.desktop-only` wrapper | Hides title + session info on mobile via pure CSS — no JS |
| 52px mobile header | Maximises vertical content space |
| `.input-bar { display:none }` unconditional | Input bar is never used on mobile regardless of page |
| `body:not(.on-home-page) #app-header { display:none }` | Full-screen forms/reports without chrome clutter |
| No `display:flex` in `.desktop-only` inline style | Avoids CSS-vs-inline specificity battle |
| `#app-header .desktop-only` scoped selector | Higher specificity; `flex:0; width:0; overflow:hidden` collapses div to zero space |
| `column-gap: 0.65rem; row-gap: 1.5rem` separately | Asymmetric gaps give tall, well-spaced button rows — shorthand `gap` applies same value both ways |
| `margin-bottom: 1.5rem` on button grid rows | Visible row separation; `0.65rem` was too tight |
| Simple: `#quick-actions > div:last-child { display:none }` | Voice hint hidden on mobile without removing the HTML |
| Amazon hybrid: flat siblings in `.qs-buttons` | CSS grid handles 2-column layout directly — no row div wrappers or voice hint needed |
| `_initHomeSearch()` still called on `goBackHome()` | Amazon search bar elements persist in the header DOM, but their event listeners need re-wiring after navigation |
| `@media (pointer: coarse)` replaces `body.is-mobile` CSS | Fires on real touch devices regardless of viewport width; simpler than a third parallel ruleset |
| Threshold `>= 3` (not `>= 5`) | CSS handles false-positives via `pointer:coarse`; JS threshold can stay lower for broader coverage |
| sessionStorage `crm_is_mobile` override | Persistent test flag — survives page reloads without URL editing |
| `clamp(0.48rem, calc(14.83dvh - 38.4px), 6rem)` padding-top | Golden-ratio positioning: centres the button grid at ~0.618 × viewport height |
| `border-radius: 24-25px` on search bar | Pairs better with 20px button corners than a full pill (999px) |
| `#app-header { display: flex !important }` as first rule in pointer:coarse | Prevents any earlier rule from hiding the header on touch devices when on the home page |
