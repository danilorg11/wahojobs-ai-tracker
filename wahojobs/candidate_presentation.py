"""Shared candidate navigation and accessible visual tokens; no application state."""
from html import escape
from urllib.parse import urlencode


def candidate_navigation(*, current='', run=None):
    query = '?' + urlencode({'run': run}) if run else ''
    links = [('matches', '/find-matches' + query, 'Matches'),
             ('tracker', '/tracker' + query, 'My Jobs'),
             ('profile', '/account/profile' + query, 'My profile')]
    return ("<header class='candidate-header'><a class='candidate-brand' href='/find-matches'>Wahojobs</a>"
            "<nav class='candidate-nav' aria-label='Product navigation'>"
            + ''.join(f"<a href='{escape(url, quote=True)}'" + (" aria-current='page'" if key == current else '') + f'>{label}</a>' for key,url,label in links)
            + "<a class='candidate-signout' href='/logout'>Sign out</a></nav></header>")


def candidate_style():
    return """
:root {--bg:#f5f7f6;--surface:#fff;--ink:#17211c;--muted:#5b6861;--line:#d9e0dc;--accent:#176b52;--focus:#2563eb;}
body {font:16px/1.5 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--ink);background:var(--bg);}
.candidate-header {display:flex;align-items:center;justify-content:space-between;gap:24px;margin:0 0 28px;padding:16px 0;border-bottom:1px solid var(--line);}
.candidate-brand {color:var(--ink);font-size:24px;font-weight:800;letter-spacing:-.7px;text-decoration:none;}
.candidate-nav {display:flex;align-items:center;flex-wrap:wrap;gap:8px 20px;}
.candidate-nav a {color:var(--muted);text-decoration:none;font-weight:650;min-height:44px;display:inline-flex;align-items:center;}
.candidate-nav a[aria-current=page] {color:var(--accent);box-shadow:inset 0 -2px var(--accent);}
.candidate-nav a:hover {color:var(--accent);text-decoration:underline;}
.candidate-nav .candidate-signout {font-size:14px;font-weight:500;}
.candidate-header a:focus-visible,button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible {outline:3px solid var(--focus);outline-offset:3px;}
.profile-header,.empty,.profile-group,.profile-review-form {border-color:var(--line);border-radius:14px;}
.profile-actions {align-items:center;}
.profile-actions .primary-link,.profile-entry-primary {display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:10px 16px;border-radius:8px;background:var(--accent);color:white;text-decoration:none;}
.profile-actions .secondary-link {display:inline-flex;align-items:center;min-height:44px;}
.profile-group li {overflow-wrap:anywhere;margin:6px 0;}
.profile-group {min-width:0;}
[role=alert] {border:1px solid #b65242;background:#fff6f2;border-radius:8px;padding:14px;}
button:disabled {opacity:.65;cursor:wait;}
.candidate-section .pay-expectation {border-top:1px solid var(--line);padding:8px 0;}
.candidate-section .pay-expectation p {font-size:14px;color:var(--muted);}
@media(max-width:640px){.candidate-header{align-items:flex-start;flex-direction:column;gap:4px;margin-bottom:20px;padding-top:0;}.candidate-nav{gap:0 18px;width:100%;}.candidate-nav a{font-size:14px;}.profile-grid{grid-template-columns:minmax(0,1fr);}.profile-header,.empty{padding:20px;}.profile-header h1,.empty h1{font-size:27px;}}
"""


def candidate_entry_style():
    return candidate_style() + """
* {box-sizing:border-box;} body {margin:0;}
main {width:min(620px,calc(100% - 32px));margin:0 auto;padding:48px 0;}
main > section {background:white;border:1px solid var(--line);border-radius:14px;padding:28px;}
h1,p {margin-top:0;} h1 {font-size:30px;line-height:1.2;}
.eyebrow {color:var(--accent);font-weight:700;}
button {appearance:none;border:0;border-radius:8px;padding:12px 18px;min-height:44px;background:var(--accent);color:white;font:inherit;font-weight:700;cursor:pointer;}
a {color:var(--accent);font-weight:650;} label {display:block;margin:12px 0;}
input:not([type=hidden]):not([type=checkbox]) {font:inherit;padding:10px;width:100%;border:1px solid #aebbb5;border-radius:6px;min-height:44px;}
@media(max-width:640px){main{padding:24px 0;}main>section{padding:22px;}h1{font-size:27px;}}
"""
