"""The page the browser shows when it returns to the CLI after Notamify sign-in.

Self-contained: no external requests. The authorization code is removed from the
address bar once the page loads. A strict Content-Security-Policy allows only the
inline styles and the one inline script below.
"""

from __future__ import annotations

import base64
import hashlib
import html

LOGO_SVG = '<svg role="img" aria-label="Notamify" class="logo" viewBox="0 0 117.86096 26.569428" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns="http://www.w3.org/2000/svg" ><defs id="defs1"><linearGradient id="linearGradient4"><stop style="stop-color:#2563eb;stop-opacity:1;" offset="0" id="stop4" /><stop style="stop-color:#1e40af;stop-opacity:1;" offset="1" id="stop5" /></linearGradient><linearGradient xlink:href="#linearGradient4" id="linearGradient5" x1="44.05938" y1="98.595726" x2="271.83984" y2="56.033058" gradientUnits="userSpaceOnUse" /></defs><g id="layer1" style="fill:url(#linearGradient5);fill-opacity:1" transform="translate(-46.228874,-120.96807)"><path style="fill:#ffffff;stroke-width:0.264583" d="m 61.397631,140.88167 h -3.556 l -8.051799,-12.1666 v 12.1666 h -3.556 v -17.7546 h 3.556 l 8.051799,12.192 v -12.192 h 3.556 z m 9.753616,0.2286 q -2.032,0 -3.657599,-0.889 -1.6256,-0.9144 -2.5654,-2.5654 -0.9144,-1.651 -0.9144,-3.81 0,-2.159 0.9398,-3.81 0.9652,-1.651 2.6162,-2.54 1.650999,-0.9144 3.682999,-0.9144 2.032,0 3.683,0.9144 1.651,0.889 2.5908,2.54 0.9652,1.651 0.9652,3.81 0,2.159 -0.9906,3.81 -0.9652,1.651 -2.6416,2.5654 -1.651,0.889 -3.7084,0.889 z m 0,-3.0988 q 0.9652,0 1.8034,-0.4572 0.8636,-0.4826 1.3716,-1.4224 0.508,-0.9398 0.508,-2.286 0,-2.0066 -1.0668,-3.0734 -1.0414,-1.0922 -2.5654,-1.0922 -1.524,0 -2.5654,1.0922 -1.015999,1.0668 -1.015999,3.0734 0,2.0066 0.990599,3.0988 1.016,1.0668 2.54,1.0668 z m 14.097014,-8.2804 v 6.8072 q 0,0.7112 0.3302,1.0414 0.3556,0.3048 1.1684,0.3048 h 1.651 v 2.9972 h -2.2352 q -4.4958,0 -4.4958,-4.3688 v -6.7818 h -1.6764 v -2.921 h 1.6764 v -3.4798 h 3.5814 v 3.4798 h 3.1496 v 2.921 z m 4.800609,4.064 q 0,-2.1336 0.8382,-3.7846 0.8636,-1.651 2.3114,-2.54 1.4732,-0.889 3.2766,-0.889 1.5748,0 2.7432,0.635 1.1938,0.635 1.905,1.6002 v -2.0066 h 3.5814 v 14.0716 h -3.5814 v -2.0574 q -0.6858,0.9906 -1.905,1.651 -1.1938,0.635 -2.7686,0.635 -1.778,0 -3.2512,-0.9144 -1.4478,-0.9144 -2.3114,-2.5654 -0.8382,-1.6764 -0.8382,-3.8354 z m 11.0744,0.0508 q 0,-1.2954 -0.508,-2.2098 -0.508,-0.9398 -1.3716,-1.4224 -0.8636,-0.508 -1.8542,-0.508 -0.9906,0 -1.8288,0.4826 -0.8382,0.4826 -1.3716,1.4224 -0.508,0.9144 -0.508,2.1844 0,1.27 0.508,2.2352 0.5334,0.9398 1.3716,1.4478 0.8636,0.508 1.8288,0.508 0.9906,0 1.8542,-0.4826 0.8636,-0.508 1.3716,-1.4224 0.508,-0.9398 0.508,-2.2352 z m 24.53639,-7.239 q 2.5908,0 4.1656,1.6002 1.6002,1.5748 1.6002,4.4196 v 8.255 h -3.556 v -7.7724 q 0,-1.651 -0.8382,-2.5146 -0.8382,-0.889 -2.286,-0.889 -1.4478,0 -2.3114,0.889 -0.8382,0.8636 -0.8382,2.5146 v 7.7724 h -3.556 v -7.7724 q 0,-1.651 -0.8382,-2.5146 -0.8382,-0.889 -2.286,-0.889 -1.4732,0 -2.3368,0.889 -0.8382,0.8636 -0.8382,2.5146 v 7.7724 h -3.556 v -14.0716 h 3.556 v 1.7018 q 0.6858,-0.889 1.7526,-1.397 1.0922,-0.508 2.3876,-0.508 1.651,0 2.9464,0.7112 1.2954,0.6858 2.0066,1.9812 0.6858,-1.2192 1.9812,-1.9558 1.3208,-0.7366 2.8448,-0.7366 z m 10.9474,-1.4732 q -0.9398,0 -1.5748,-0.5842 -0.6096,-0.6096 -0.6096,-1.4986 0,-0.889 0.6096,-1.4732 0.635,-0.6096 1.5748,-0.6096 0.9398,0 1.5494,0.6096 0.635,0.5842 0.635,1.4732 0,0.889 -0.635,1.4986 -0.6096,0.5842 -1.5494,0.5842 z m 1.7526,1.6764 v 14.0716 h -3.556 v -14.0716 z m 9.95681,2.921 h -2.4638 v 11.1506 h -3.6068 v -11.1506 h -1.6002 v -2.921 h 1.6002 v -0.7112 q 0,-2.5908 1.4732,-3.81 1.4732,-1.2192 4.445,-1.143 v 2.9972 q -1.2954,-0.0254 -1.8034,0.4318 -0.508,0.4572 -0.508,1.651 v 0.5842 h 2.4638 z m 15.77337,-2.921 -8.7122,20.7264 h -3.7846 l 3.048,-7.0104 -5.6388,-13.716 h 3.9878 l 3.6322,9.8298 3.683,-9.8298 z" id="text1" aria-label="Notamify" /><rect style="fill:#ffffff;fill-opacity:1;stroke-width:0.585975" id="rect1" width="108.40235" height="2.9000001" x="46.228874" y="144.6375" ry="0.47815406" /></g></svg>'

_SCRIPT = 'history.replaceState(null, "", location.pathname);'
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode("ascii")

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    f"script-src 'sha256-{_SCRIPT_HASH}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

_CHECK = '<path d="M7 12.5l3.2 3.2L17 9" />'
_CROSS = '<path d="M8.5 8.5l7 7M15.5 8.5l-7 7" />'
_ALERT = '<path d="M12 7.5v5.5" /><path d="M12 16.5h.01" />'

PAGES = {
    "success": (
        "ok",
        _CHECK,
        "You're signed in",
        "The Notamify CLI is connected to your account. Return to your terminal; you can close this tab.",
        True,
    ),
    "denied": (
        "warn",
        _CROSS,
        "Sign-in cancelled",
        "Nothing was connected. To try again, run <code>notamify auth login</code>.",
        False,
    ),
    "error": (
        "warn",
        _ALERT,
        "Sign-in didn't complete",
        "Return to your terminal for details.",
        False,
    ),
    "mismatch": (
        "warn",
        _ALERT,
        "This sign-in link isn't for this request",
        "It may have expired or come from another terminal. Run <code>notamify auth login</code> again.",
        False,
    ),
}

_STYLE = """
:root{color-scheme:dark;--bg:#020817;--card:#0b1324;--border:#1e293b;--text:#e2e8f0;--muted:#94a3b8;--ok:#3b82f6;--warn:#f59e0b}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:radial-gradient(1200px 600px at 50% -10%,#0f1e3d 0%,var(--bg) 60%);color:var(--text);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
display:flex;align-items:center;justify-content:center;padding:24px}
main{width:100%;max-width:440px;background:var(--card);border:1px solid var(--border);border-radius:16px;
padding:36px 32px 28px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
.logo{display:block;width:150px;height:auto;margin:0 0 28px}
.icon{width:52px;height:52px;border-radius:50%;display:grid;place-items:center;margin-bottom:18px}
.icon svg{width:30px;height:30px;fill:none;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
.ok .icon{background:rgba(59,130,246,.14)}.ok .icon svg{stroke:var(--ok)}
.warn .icon{background:rgba(245,158,11,.14)}.warn .icon svg{stroke:var(--warn)}
h1{font-size:22px;line-height:1.3;font-weight:600;margin:0 0 8px;letter-spacing:-.01em}
p{margin:0;color:var(--muted)}
code{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--text);white-space:nowrap}
.try{margin-top:22px;padding:12px 14px;border:1px solid var(--border);border-radius:10px;background:var(--bg)}
.try span{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
footer{margin-top:26px;padding-top:16px;border-top:1px solid var(--border);font-size:13px;color:var(--muted)}
footer a{color:var(--text);text-decoration:underline;text-underline-offset:3px}
"""


def render(kind: str) -> str:
    """HTML for one of: success, denied, error, mismatch."""
    tone, icon, title, message, show_next = PAGES.get(kind, PAGES["error"])
    next_step = (
        '<div class="try"><span>Try it</span><code>notamify notams active EPWA</code></div>' if show_next else ""
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title, quote=False)} \u00b7 Notamify</title><style>{_STYLE}</style></head>"
        f"<body><main class=\"{tone}\">{LOGO_SVG}"
        f"<div class=\"icon\"><svg viewBox=\"0 0 24 24\" aria-hidden=\"true\">{icon}</svg></div>"
        f"<h1>{html.escape(title, quote=False)}</h1><p>{message}</p>{next_step}"
        "<footer>Manage connected apps in your "
        "<a href=\"https://notamify.com/account#connections\">Notamify account</a>.</footer>"
        f"</main><script>{_SCRIPT}</script></body></html>"
    )
