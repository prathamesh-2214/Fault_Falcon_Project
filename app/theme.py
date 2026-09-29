"""Visual layer for the Streamlit app, in the style of the AWS console (Cloudscape, dark): a slim top
bar, a page title with status badges, a key-value details panel, a compact stage progress bar and a
coloured diff viewer. Pure functions returning HTML strings (unit-testable)."""

from __future__ import annotations

import html

from ff.engine import stages as st

# Cloudscape dark palette
BG, PANEL, BORDER, TEXT, MUTED = "#0f1b2a", "#192534", "#414d5c", "#d1d5db", "#8d99a8"
BLUE, ORANGE, RED, GREEN, YELLOW = "#539fe5", "#ff9900", "#ff5d64", "#29ad32", "#fbd332"

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
html, body, [class*="css"], .stMarkdown, button, input, textarea {{ font-family: 'Open Sans', 'Helvetica Neue', Arial, sans-serif; }}
.block-container {{ padding-top: 3.2rem; padding-bottom: 1rem; max-width: 1600px; }}
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"] {{ visibility: hidden; height: 0; }}
header[data-testid="stHeader"] {{ background: transparent; height: 0; }}
[data-testid="stSidebar"] {{ background: #16191f; border-right: 1px solid {BORDER}; }}
[data-testid="stSidebar"] {{ top: 44px; height: calc(100vh - 44px); }}
[data-testid="stSidebarContent"] {{ padding-top: .4rem; }}
[data-testid="stSidebarHeader"] {{ height: 1.2rem; min-height: 0; padding: 0; }}
/* collapsed sidebar: its open button sits below the console bar (not under it), labelled like a nav toggle */
[data-testid="stExpandSidebarButton"] {{ visibility: visible; position: fixed; top: 52px; left: 12px; z-index: 1000101; width: auto;
  height: 30px; display: flex; align-items: center; gap: 6px; padding: 0 10px; border-radius: 6px;
  background: {PANEL}; border: 1px solid {BORDER}; color: {TEXT}; }}
[data-testid="stExpandSidebarButton"]::after {{ content: "Incidents"; font-size: .8rem; font-weight: 600; }}
[data-testid="stExpandSidebarButton"]:hover {{ border-color: {ORANGE}; }}
[data-testid="stAppViewContainer"]:has([data-testid="stSidebar"][aria-expanded="false"]) .block-container {{ padding-top: 5.2rem; }}
h1, h2, h3 {{ letter-spacing: 0; }}
.stTabs [data-baseweb="tab-list"] {{ gap: 2px; border-bottom: 1px solid {BORDER}; }}
.stTabs [data-baseweb="tab"] {{ padding: 6px 14px; font-weight: 600; font-size: .9rem; }}
.stTabs [aria-selected="true"] {{ color: {BLUE} !important; }}
div[data-testid="stExpander"] details {{ border-color: {BORDER}; border-radius: 8px; }}
.stButton > button {{ border-radius: 20px; font-weight: 600; }}

.ff-topbar {{ position: fixed; top: 0; left: 0; right: 0; height: 44px; z-index: 1000100; background: #232f3e;
  display: flex; align-items: center; gap: 14px; padding: 0 20px; border-bottom: 1px solid #0b0f14; }}
.ff-topbar .brand {{ color: #fff; font-weight: 700; font-size: 1.02rem; letter-spacing: .2px; }}
.ff-topbar .brand b {{ color: {ORANGE}; }}
.ff-topbar .sep {{ width: 1px; height: 20px; background: #414d5c; }}
.ff-topbar .crumb {{ color: {MUTED}; font-size: .85rem; }}
.ff-topbar .crumb span {{ color: {TEXT}; }}
.ff-topbar .right {{ margin-left: auto; display: flex; align-items: center; gap: 10px; color: {MUTED}; font-size: .8rem; }}
.ff-topbar .env {{ border: 1px solid #414d5c; border-radius: 4px; padding: 0 7px; color: {TEXT}; font-size: .72rem; }}

.ff-topbar .brand {{ display: flex; align-items: center; gap: 8px; }}
.ff-nav {{ display: flex; flex-direction: column; align-items: center; text-align: center; padding: 4px 4px 14px;
  margin-bottom: 10px; border-bottom: 1px solid {BORDER}; }}
.ff-nav .ff-logo {{ filter: drop-shadow(0 2px 6px rgba(255,153,0,.35)); margin-bottom: 6px; }}
.ff-nav .name {{ color: #fff; font-weight: 700; font-size: 1.6rem; line-height: 1.15; letter-spacing: .3px; }}
.ff-nav .name b {{ color: {ORANGE}; }}
.ff-nav .desc {{ color: {MUTED}; font-size: .8rem; line-height: 1.35; margin-top: 4px; }}

.ff-footer {{ margin-top: 28px; padding: 10px 2px 4px; border-top: 1px solid {BORDER}; color: {MUTED};
  font-size: .75rem; display: flex; flex-wrap: wrap; gap: 6px 18px; justify-content: space-between; }}
.ff-footer b {{ color: {TEXT}; font-weight: 600; }}

.ff-title {{ display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 2px 0 2px; }}
.ff-title h2 {{ font-size: 1.45rem; font-weight: 700; margin: 0; padding: 0; color: #fff; }}
.ff-meta {{ color: {MUTED}; font-size: .85rem; margin-bottom: 10px; }}
.ff-badge {{ display: inline-flex; align-items: center; gap: 5px; padding: 1px 9px; border-radius: 4px;
  font-size: .75rem; font-weight: 700; border: 1px solid; white-space: nowrap; }}
.ff-badge.sev1 {{ color: {RED}; border-color: {RED}; }}
.ff-badge.sev2 {{ color: {ORANGE}; border-color: {ORANGE}; }}
.ff-badge.sev3 {{ color: {YELLOW}; border-color: {YELLOW}; }}
.ff-badge.open {{ color: {RED}; border-color: transparent; background: rgba(255,93,100,.12); }}
.ff-badge.progress {{ color: {BLUE}; border-color: transparent; background: rgba(83,159,229,.14); }}
.ff-badge.done {{ color: {GREEN}; border-color: transparent; background: rgba(41,173,50,.14); }}

.ff-panel {{ border: 1px solid {BORDER}; border-radius: 8px; background: {PANEL}; padding: 10px 16px 12px;
  margin-bottom: 10px; }}
.ff-panel .h {{ font-weight: 700; font-size: .92rem; margin-bottom: 8px; color: #fff; }}
.ff-kv {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 6px 18px; }}
@media (max-width: 1100px) {{ .ff-kv {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
.ff-kv .k {{ color: {MUTED}; font-size: .74rem; }}
.ff-kv .v {{ color: {TEXT}; font-size: .92rem; font-weight: 600; word-break: break-word; }}
.ff-kv .v.warn {{ color: {RED}; }}
.ff-kv .v.ok {{ color: {GREEN}; }}

.ff-steps {{ display: flex; gap: 3px; margin: 0 0 10px; }}
.ff-step {{ flex: 1; font-size: .7rem; color: {MUTED}; border-top: 3px solid {BORDER}; padding-top: 3px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.ff-step.done {{ border-top-color: {GREEN}; color: {TEXT}; }}
.ff-step.cur {{ border-top-color: {BLUE}; color: #fff; font-weight: 700; }}

.ff-diff {{ font-family: 'JetBrains Mono', Consolas, monospace; font-size: .8rem; border-radius: 6px; overflow-x: auto;
  border: 1px solid {BORDER}; margin: 6px 0 4px; background: #0b1422; }}
.ff-diff div {{ padding: 1px 12px; white-space: pre; }}
.ff-diff .add {{ background: rgba(41,173,50,.18); }}
.ff-diff .del {{ background: rgba(255,93,100,.18); }}
.ff-diff .hdr {{ background: rgba(83,159,229,.16); font-weight: 600; }}
.ff-bar {{ height: 6px; border-radius: 3px; background: rgba(141,153,168,.25); overflow: hidden; margin: 4px 0; }}
.ff-bar > span {{ display: block; height: 100%; background: linear-gradient(90deg, {ORANGE}, {RED}); }}
.ff-pill {{ display: inline-block; font-size: .7rem; padding: 0 7px; border-radius: 4px; margin-right: 4px;
  color: {BLUE}; background: rgba(83,159,229,.12); }}
</style>
"""


# the falcon mark: a falcon head in profile (hooked beak, eye, cheek stripe); inline SVG, no external file needed
FALCON_HEAD = ("M12 60C9 38 16 16 33 10.5C42.5 7.5 51 10.5 55.5 17.5C58.6 22.2 59.6 27.6 57.4 32.4"
               "C56.4 29.4 54 28 50.8 28.2L46.6 28.8C44.4 35.5 41 44 39.6 60Z")
FALCON_BEAK = ("M46.4 13.2C52.8 14.2 58 20.4 57.4 32.4C56.4 29.4 54 28 50.8 28.2L46.6 28.8"
               "C48.2 23.4 48.2 18 46.4 13.2Z")
FALCON_STRIPE = "M37.6 20.6C34.2 27 33 33.6 34.2 39.6C37.6 34.2 40 28.2 41.2 22Z"


def logo(size: int = 28) -> str:
    """The FaultFalcon mark as an inline SVG of ``size`` pixels."""
    return (f'<svg class="ff-logo" width="{size}" height="{size}" viewBox="0 0 64 64" role="img" aria-label="FaultFalcon">'
            '<defs><linearGradient id="ffg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffc266"/>'
            f'<stop offset="1" stop-color="{ORANGE}"/></linearGradient></defs>'
            f'<path d="{FALCON_HEAD}" fill="url(#ffg)"/>'
            f'<path d="{FALCON_BEAK}" fill="#232f3e" opacity=".55"/>'
            f'<path d="{FALCON_STRIPE}" fill="#232f3e"/>'
            '<circle cx="41.4" cy="18.6" r="3.4" fill="#232f3e"/><circle cx="42.4" cy="17.6" r="1.1" fill="#fff"/></svg>')


PRODUCT = "FaultFalcon"
TAGLINE = "Root-cause analysis for deployment failures"
ABOUT = ("Correlates alerts, logs, code changes and runbooks to trace an incident to the change and line that "
         "caused it, across long multi-turn investigations.")


def topbar(crumb: str = "") -> str:
    """The fixed console bar: product name, breadcrumb and, on the right, the product area."""
    c = f'<div class="sep"></div><div class="crumb">Incidents › <span>{html.escape(crumb)}</span></div>' if crumb else ""
    right = '<div class="right">Incident Response<span class="env">Internal</span></div>'
    return f'<div class="ff-topbar"><div class="brand">{logo(22)}<span>Fault<b>Falcon</b></span></div>{c}{right}</div>'


def nav_header() -> str:
    """Service name and one-line description at the top of the side navigation."""
    return (f'<div class="ff-nav">{logo(64)}<div class="name">Fault<b>Falcon</b></div>'
            f'<div class="desc">{html.escape(TAGLINE)}</div></div>')


def footer(model: str = "") -> str:
    """Slim page footer: what the tool does, and the model in use."""
    m = f"<span>Model: {html.escape(model)}</span>" if model else ""
    return (f'<div class="ff-footer"><span><b>{PRODUCT}</b> · {html.escape(ABOUT)}</span>'
            f'<span>Internal tool · Site Reliability Engineering</span>{m}</div>')


def hero(model: str = "", mode: str = "", incidents: int = 0) -> str:
    """Kept for compatibility: the top bar."""
    return topbar()


def badge(text: str, kind: str) -> str:
    return f'<span class="ff-badge {kind}">{html.escape(text)}</span>'


def title(text: str, severity: str, status: str, meta: str) -> str:
    """Page title with severity and status badges and one line of metadata."""
    sev = {"SEV-1": "sev1", "SEV-2": "sev2"}.get(severity, "sev3")
    st_kind = {"Open": "open", "Investigating": "progress"}.get(status, "done")
    return (f'<div class="ff-title"><h2>{html.escape(text)}</h2>{badge(severity, sev)}{badge(status, st_kind)}</div>'
            f'<div class="ff-meta">{html.escape(meta)}</div>')


def details(items: list[tuple[str, str, str]], heading: str = "Details") -> str:
    """A key-value panel of (label, value, style) with style '' | 'warn' | 'ok'."""
    cells = "".join(f'<div><div class="k">{html.escape(k)}</div><div class="v {s}">{html.escape(v)}</div></div>'
                    for k, v, s in items)
    return f'<div class="ff-panel"><div class="h">{html.escape(heading)}</div><div class="ff-kv">{cells}</div></div>'


def kpis(items: list[tuple[str, str, str]]) -> str:
    """Compatibility: (label, value, sub-text) cards as a details panel."""
    return details([(k, f"{v} {s}".strip(), "") for k, v, s in items], heading="")


def stepper(current: str) -> str:
    """Compact progress bar over the investigation stages."""
    idx = st.STAGES.index(current) if current in st.STAGES else -1
    parts = ['<div class="ff-steps">']
    for i, s in enumerate(st.STAGES):
        cls = "cur" if i == idx else ("done" if i < idx else "")
        parts.append(f'<div class="ff-step {cls}">{i + 1}. {html.escape(st.LABELS[s])}</div>')
    parts.append("</div>")
    return "".join(parts)


def diff_html(diff: str) -> str:
    """Unified diff with coloured added / removed lines."""
    rows = []
    for ln in (diff or "").splitlines():
        cls = "hdr" if ln.startswith("@@") else "add" if ln.startswith("+") else "del" if ln.startswith("-") else ""
        rows.append(f'<div class="{cls}">{html.escape(ln) or "&nbsp;"}</div>')
    return f'<div class="ff-diff">{"".join(rows)}</div>'


def score_bar(score: float) -> str:
    pct = max(0, min(100, round(score * 100)))
    return f'<div class="ff-bar"><span style="width:{pct}%"></span></div>'
