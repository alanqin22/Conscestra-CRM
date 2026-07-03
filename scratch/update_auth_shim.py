"""Upgrade the crm-auth-shim in every page (v9): adds an "Admin sign-in
required" notice — a 403 whose detail is the admin gate's ("Admin authorization
required", e.g. the always-admin Email module) gets its own wording instead of
the read-only/write one. v8: RESTORES the in-page view after
an auth block — pages wipe #responseArea with a loading indicator the moment a
form is submitted, so the shim snapshots the area's HTML + field values on every
mousedown/keydown (capture phase, before handlers run; skipped while the notice
overlay is open) and, when the user closes the notice ("Continue browsing"),
puts the pre-submit view back — e.g. the filled Update Lead form survives a
blocked write. Works because pages wire buttons via inline onclick + global fns.
Keeps v7: 401/403 blocks softened to a 200 chat-shaped {output:"🔒 <detail>"}
body; v6: posture-aware notice (locked → "Back to Home"); v5: signed-in chip
with Sign out, "Session expired" handling. PUBLIC pages never gated.
Replaces the existing shim <script> block.
Run: python scratch/update_auth_shim.py
"""
import os, re, glob
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

targets = sorted(glob.glob(os.path.join(ROOT, "*-mgmt.html")))
for extra in ("index.html", "store-home.html"):
    p = os.path.join(ROOT, extra)
    if os.path.exists(p):
        targets.append(p)

NEW_SHIM = """<script>
/* CRM API auth shim (security #1b/#2) — marker: crm-auth-shim
   - Attaches the logged-in session token to API calls.
   - PUBLIC pages (index/store-home) are never gated (signed-in chip still shows).
   - Signed-in chip (bottom-right): who + role + Sign out (POST /auth/signout,
     clear stored session, reload).
   - On a blocked request shows a polished notice (max 1024px):
       * not signed in        -> Sign In / Sign Up
       * session expired      -> 401 while holding a token: clear it, re-sign-in
       * signed in, read-only -> "Request edit access" (emails info@agentorc.ca)
     Triggers on 401 (gate, anon/expired), 403 (gate, viewer), or a 200/500 body
     carrying the in-agent guard's "Read-only access". Inert when nothing blocks.
   - POSTURE-AWARE (GET /auth-health security_mode): when the API is 'locked',
     anonymous visitors may only use index/store-home, so the sign-in notice
     offers "Back to Home" instead of staying on a gated page; under
     'public-read' it stays the closeable "Continue browsing".
   - VIEW RESTORE: snapshots #responseArea (HTML + field values) before each
     interaction; closing the notice restores the pre-submit view, so a filled
     form survives a blocked write. */
(function () {
    var KEY = 'orbit_auth_session';
    var ADMIN_EMAIL = 'info@agentorc.ca';
    var API_BASE = (location.hostname === 'localhost' || location.hostname === '127.0.0.1' ||
                    location.protocol === 'file:')
        ? 'http://localhost:8000' : 'https://orbitcrm-production.up.railway.app';
    var PUBLIC = { '': 1, 'index.html': 1, 'store-home.html': 1 };
    var page = (location.pathname.split('/').pop() || '').toLowerCase();
    var isPublic = !!PUBLIC[page];
    function sess() { try { return JSON.parse(localStorage.getItem(KEY) || 'null') || {}; } catch (e) { return {}; } }
    function tok() { return sess().session_token || ''; }
    function isWriter() { var r = sess().role; return r === 'admin' || r === 'member'; }
    function clearSess() { try { localStorage.removeItem(KEY); } catch (e) {} }
    function esc(x) { return String(x).replace(/[<>&"]/g, function (c) {
        return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]; }); }
    var _fetch = window.fetch ? window.fetch.bind(window) : null;
    var POSTURE = '';
    function withPosture(cb) {
        if (POSTURE || !_fetch) return cb();
        _fetch(API_BASE + '/auth-health')
            .then(function (r) { return r.json(); })
            .then(function (j) { POSTURE = (j && j.security_mode) || 'unknown'; cb(); })
            .catch(function () { POSTURE = 'unknown'; cb(); });
    }
    function removeChip() { var c = document.getElementById('crm-auth-chip'); if (c) try { c.remove(); } catch (e) {} }
    // ── View snapshot/restore ────────────────────────────────────────────────
    // Pages replace #responseArea with a loading indicator the instant a form is
    // submitted, so on an auth block the form is already gone. Snapshot the area
    // (HTML + typed field values) on every interaction BEFORE handlers run;
    // closing the notice restores the pre-submit view, data intact.
    var SNAP = null;
    function snapArea() {
        if (document.getElementById('crm-auth-overlay')) return; // notice open — keep pre-block snap
        var area = document.getElementById('responseArea');
        if (!area || area.querySelector('#loadingIndicator')) return;
        var html = area.innerHTML;
        if (!html || html.length < 40) return;
        var vals = {};
        area.querySelectorAll('input[id],select[id],textarea[id]').forEach(function (el) {
            vals[el.id] = (el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.value;
        });
        SNAP = { html: html, vals: vals };
    }
    document.addEventListener('mousedown', snapArea, true);
    document.addEventListener('keydown', snapArea, true);
    function restoreSnap() {
        var area = document.getElementById('responseArea');
        if (!area || !SNAP) return;
        area.innerHTML = SNAP.html;
        Object.keys(SNAP.vals).forEach(function (id) {
            var el = document.getElementById(id);
            if (!el) return;
            if (el.type === 'checkbox' || el.type === 'radio') el.checked = SNAP.vals[id];
            else el.value = SNAP.vals[id];
        });
    }
    function signOut() {
        var t = tok();
        var done = function () { clearSess(); location.reload(); };
        if (!t || !_fetch) return done();
        _fetch(API_BASE + '/auth/signout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_token: t })
        }).then(done, done);
    }
    function renderChip() {
        var s = sess();
        if (!s.session_token || !document.body || document.getElementById('crm-auth-chip')) return;
        var who = s.first_name || s.email || s.identifier || 'Account';
        var chip = document.createElement('div');
        chip.id = 'crm-auth-chip';
        chip.style.cssText = 'position:fixed;right:14px;bottom:14px;z-index:2147483646;display:flex;' +
            'align-items:center;gap:.55rem;padding:.4rem .45rem .4rem .85rem;border-radius:999px;' +
            'background:rgba(15,23,42,.88);color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,' +
            '\\'Segoe UI\\',Roboto,sans-serif;font-size:.78rem;font-weight:600;' +
            'box-shadow:0 8px 24px rgba(0,0,0,.35);backdrop-filter:blur(6px)';
        chip.innerHTML =
            '<span style="opacity:.92">\\uD83D\\uDC64 ' + esc(who) +
                (s.role ? ' \\u00B7 ' + esc(s.role) : '') + '</span>' +
            '<button type="button" id="crm-auth-signout" style="border:0;border-radius:999px;' +
                'padding:.38rem .8rem;cursor:pointer;background:linear-gradient(135deg,#0d9488,#0f766e);' +
                'color:#fff;font-family:inherit;font-size:.78rem;font-weight:600">Sign out</button>';
        document.body.appendChild(chip);
        document.getElementById('crm-auth-signout').onclick = signOut;
    }
    if (document.body) renderChip();
    else document.addEventListener('DOMContentLoaded', renderChip);
    var shown = false;
    function showNotice(kind) {
        if (shown || !document.body) return; shown = true;
        withPosture(function () { renderNotice(kind); });
    }
    function renderNotice(kind) {
        if (!document.body) { shown = false; return; }
        var s = sess();
        var loggedIn = !!s.session_token;
        var here = encodeURIComponent(page + location.search);
        var title, msg, primary;
        var signinBtn = '<a href="auth.html?redirect=' + here + '" style="padding:.7rem 1.3rem;border-radius:10px;' +
            'background:linear-gradient(135deg,#0d9488,#0f766e);color:#fff;text-decoration:none;' +
            'font-weight:600;white-space:nowrap">Sign In / Sign Up</a>';
        if (kind === 'write' && loggedIn) {
            title = 'Edit access required';
            msg = 'Your account is signed in but does not have edit access \\u2014 editing is ' +
                  'limited to administrators. Request access and an administrator will grant it.';
            var who = s.email || s.identifier || '(your account)';
            var mailto = 'mailto:' + ADMIN_EMAIL +
                '?subject=' + encodeURIComponent('Edit access request \\u2014 Conscestra CRM') +
                '&body=' + encodeURIComponent('Please grant edit (Admin) access to my account: ' + who + '.\\n\\nThank you.');
            primary = '<a href="' + mailto + '" style="padding:.7rem 1.3rem;border-radius:10px;' +
                'background:linear-gradient(135deg,#0d9488,#0f766e);color:#fff;text-decoration:none;' +
                'font-weight:600;white-space:nowrap">Request edit access</a>';
        } else if (kind === 'write') {
            title = 'Read-only access';
            msg = 'You are browsing in read-only mode \\u2014 editing is limited to administrators. ' +
                  'Sign in with an authorized account, or sign up and request access.';
            primary = signinBtn;
        } else if (kind === 'expired') {
            title = 'Session expired';
            msg = 'You were signed out after a period of inactivity. Please sign in again to continue.';
            primary = signinBtn;
        } else if (kind === 'admin') {
            title = 'Admin sign-in required';
            msg = 'This module is restricted to administrators \\u2014 it can read and ' +
                  'send email on behalf of the company. Sign in with an admin account to use it.';
            primary = signinBtn;
        } else {
            title = 'Sign in to continue';
            msg = 'This module needs a Conscestra CRM account. Sign in to use it, or keep browsing the site.';
            primary = signinBtn;
        }
        // Locked API: anonymous visitors may only use index/store-home, so the
        // fallback action leaves the gated page instead of lingering on it.
        var goHome = POSTURE === 'locked' && kind !== 'write';
        var secondary = goHome
            ? '<a href="index.html" style="padding:.7rem 1.1rem;border-radius:10px;background:#f1f5f9;' +
              'color:#475569;font-size:.9rem;text-decoration:none;font-weight:600;white-space:nowrap">' +
              '\\uD83C\\uDFE0 Back to Home</a>'
            : '<button type="button" id="crm-auth-close" style="padding:.7rem 1.1rem;border:0;border-radius:10px;' +
              'background:#f1f5f9;color:#475569;font-size:.9rem;cursor:pointer;white-space:nowrap">' +
              'Continue browsing</button>';
        var o = document.createElement('div');
        o.id = 'crm-auth-overlay';
        o.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;' +
            'justify-content:center;padding:1.25rem;background:rgba(15,23,42,.72);backdrop-filter:blur(4px);' +
            'font-family:-apple-system,BlinkMacSystemFont,\\'Segoe UI\\',Roboto,sans-serif';
        o.innerHTML =
            '<div style="background:#fff;border-radius:18px;width:100%;max-width:1024px;padding:1.6rem 1.9rem;' +
            'box-shadow:0 24px 70px rgba(0,0,0,.42);display:flex;align-items:center;gap:1.4rem;flex-wrap:wrap">' +
              '<div style="font-size:2.6rem;line-height:1">\\uD83D\\uDD12</div>' +
              '<div style="flex:1 1 320px;min-width:240px;text-align:left">' +
                '<h2 style="margin:0 0 .35rem;font-size:1.2rem;color:#0f172a">' + title + '</h2>' +
                '<p style="margin:0;color:#475569;font-size:.92rem;line-height:1.55">' + msg + '</p>' +
              '</div>' +
              '<div style="display:flex;gap:.6rem;flex:0 0 auto;flex-wrap:wrap">' + primary +
                secondary +
              '</div>' +
            '</div>';
        document.body.appendChild(o);
        function close() {
            try { o.remove(); } catch (e) {}
            shown = false;
            restoreSnap();  // bring back the pre-submit view (e.g. the filled form)
        }
        var closeBtn = o.querySelector('#crm-auth-close');
        if (closeBtn) closeBtn.onclick = close;
        o.addEventListener('click', function (e) {
            if (e.target === o) { if (goHome) location.href = 'index.html'; else close(); }
        });
    }
    if (!_fetch) return;
    window.fetch = function (input, init) {
        init = init || {};
        var t = tok();
        if (t) {
            var src = (init && init.headers) ||
                      (typeof input === 'object' && input && input.headers) || {};
            var h = new Headers(src);
            if (!h.has('Authorization')) h.set('Authorization', 'Bearer ' + t);
            init.headers = h;
        }
        return _fetch(input, init).then(function (res) {
            if (!res) return res;
            if (res.status === 401 || res.status === 403) {
                // Read the body first — the detail decides which notice to show.
                return res.clone().json().catch(function () { return {}; }).then(function (j) {
                    var msg = (j && j.detail) || 'Please sign in to continue.';
                    var isAdminGate = res.status === 403 && /admin authorization/i.test(msg);
                    if (res.status === 401 && tok()) {
                        // Held a token but the API refused it — the session
                        // idle-expired or was invalidated. Drop the stale token.
                        clearSess(); removeChip();
                        if (!isPublic) showNotice('expired');
                    } else if (!isPublic) {
                        if (isAdminGate) showNotice('admin');
                        else showNotice(res.status === 401 ? 'signin' : 'write');
                    }
                    // Soften the block for the page: hand it a chat-shaped 200
                    // body so it renders a normal message instead of the raw
                    // {"detail": ...} JSON — the shim's notice is the real UX
                    // and the page stays usable read-only.
                    var soft = { success: true, sessionId: 'auth-shim', mode: 'info',
                                 reportMode: 'generic', output: '\\uD83D\\uDD12 ' + msg };
                    return new Response(JSON.stringify(soft),
                        { status: 200, headers: { 'Content-Type': 'application/json' } });
                });
            }
            if (!isPublic && (res.status === 200 || res.status === 500) && !isWriter()) {
                res.clone().text().then(function (txt) {
                    if (txt && txt.indexOf('Read-only access') !== -1) showNotice('write');
                }).catch(function () {});
            }
            return res;
        });
    };
})();
</script>"""

SHIM_RE = re.compile(r"<script>\s*/\* CRM API auth shim.*?</script>", re.DOTALL)
# Older minimal variant (executives/governance): HTML-comment marker + plain script.
SHIM_RE_V0 = re.compile(
    r"<!-- CRM API auth shim \(marker: crm-auth-shim\).*?-->\s*<script>.*?</script>",
    re.DOTALL)

for path in targets:
    name = os.path.basename(path)
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    if "crm-auth-shim" not in html:
        print(f"skip  {name} (no shim)")
        continue
    new, n = SHIM_RE.subn(lambda _m: NEW_SHIM, html, count=1)
    if not n:
        new, n = SHIM_RE_V0.subn(lambda _m: NEW_SHIM, html, count=1)
    if n:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"upgraded {name}")
    else:
        print(f"WARN  {name} (marker present but block not matched)")
