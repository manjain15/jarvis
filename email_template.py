"""
Jarvis — Email Template
=======================
Renders Jarvis's emails (the morning brief and the weekly review) in the approved
dark design (docs/brief_design.html) as email-safe HTML: tables, inline
styles and bgcolor attributes. Gmail's iOS/Android dark mode fully inverts
those colours, so fills also carry a same-colour linear-gradient (inversion
skips background-image) and text is wrapped for the Gmail-only blend-mode
undo. The one <style> block is scoped with `u + .body` so other clients
ignore it. Also returns a plain-text version for the text/plain part.

render_sections() is the general renderer; render_brief() maps the brief's
data onto it. Both take plain values only; every number is computed by the
caller from Jarvis's data. Sections with nothing in them are left out.

    data = {
      "title":     "Monday 12 October",
      "meta":      "Week 5 · Pull day · 3 classes",
      "note":      "MATH2931 quiz is Thursday ...",          # optional
      "owed":      [{"label": "Gym make-up · by Tue", "value": "55 min", "miss": True}],
      "today":     [{"time": "06:30", "title": "Pull", "sub": "Leave by 06:25"}],
      "due":       [{"label": "MATH2931 Assignment 2", "value": "Wed 11 Nov · 30d"}],
      "yesterday": [{"value": "Done", "label": "Legs", "miss": False}, ...],  # up to 4
      "push":      "Message ... Draft is in Gmail.",           # optional
      "footer":    "jarvis · 07:05",
    }
"""

import html

BG      = "#0a0a0a"
OUTER   = "#000000"
INK     = "#ededed"
SUB     = "#8a8a8a"
LINE    = "#1f1f1f"
MISS    = "#f97066"
SANS    = "-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Helvetica,Arial,sans-serif"
MONO    = "'SF Mono',Menlo,'JetBrains Mono',Consolas,'Courier New',monospace"
WIDTH   = 520


def _e(text):
    return html.escape(str(text or ""))


def _fill(colour):
    """CSS background Gmail dark mode does not invert.

    Inversion rewrites bgcolor and background-color and leaves
    background-image alone, so a same-colour gradient keeps the fill.
    """
    return (f"background-color:{colour};"
            f"background-image:linear-gradient({colour}, {colour});")


def _blend(inner):
    """Wrap text so Gmail dark mode keeps the authored colour.

    Gmail rewrites the body into a div and inserts a <u> before it, so
    `u + .body` matches only there. screen then difference against black
    is a no-op until that black is inverted to white, which flips the
    text a second time and restores it. Other clients never match the
    selector, so the wrappers stay unstyled. Greys come back cleanly; a
    saturated colour such as the miss red can drift a step because
    Gmail's invert is not a pure channel flip.
    """
    return ('<div class="gmail-blend-screen"><div class="gmail-blend-difference">'
            f'{inner}</div></div>')


def _label(text):
    """Small grey uppercase section label with a hairline under it."""
    return (
        f'<tr><td style="padding:36px 0 0 0;">'
        f'<div style="font-family:{SANS};font-size:11px;font-weight:500;letter-spacing:1px;'
        f'text-transform:uppercase;color:{SUB};padding-bottom:10px;border-bottom:1px solid {LINE};">'
        f'{_blend(_e(text))}</div></td></tr>'
    )


def _rows(rows):
    """A list of rows with hairlines between them. Each row is (left_html, right_html)."""
    cells = []
    for i, (left, right) in enumerate(rows):
        border = f"border-bottom:1px solid {LINE};" if i < len(rows) - 1 else ""
        cells.append(
            f'<tr><td style="padding:11px 0;{border}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'{left}{right}</tr></table></td></tr>'
        )
    return (f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0">{"".join(cells)}</table></td></tr>')


def _text_cell(text, sub=None, miss=False):
    colour = MISS if miss else INK
    sub_html = (f'<div style="font-family:{SANS};font-size:13px;line-height:1.45;color:{SUB};'
                f'padding-top:2px;">{_e(sub)}</div>') if sub else ""
    return (f'<td valign="top" style="font-family:{SANS};font-size:15px;line-height:1.45;'
            f'color:{colour};">{_blend(f"{_e(text)}{sub_html}")}</td>')


def _right_cell(text, miss=False):
    colour = MISS if miss else SUB
    return (f'<td valign="top" align="right" style="font-family:{MONO};font-size:13px;'
            f'color:{colour};white-space:nowrap;padding-left:16px;padding-top:1px;">'
            f'{_blend(_e(text))}</td>')


def _time_cell(text):
    return (f'<td valign="top" width="56" style="font-family:{MONO};font-size:13px;color:{SUB};'
            f'width:56px;padding-top:1px;">{_blend(_e(text))}</td>')


def _stat_cell(stat, width):
    """One Yesterday tile: dark fill plus its value and label."""
    value_colour = MISS if stat.get("miss") else INK
    inner = (
        f'<div style="font-family:{MONO};font-size:18px;line-height:1.1;font-weight:500;'
        f'color:{value_colour};">{_e(stat["value"])}</div>'
        f'<div style="font-family:{SANS};font-size:12px;color:{SUB};padding-top:4px;">'
        f'{_e(stat["label"])}</div>'
    )
    return (
        f'<td valign="top" width="{width}%" bgcolor="{BG}" style="{_fill(BG)}padding:14px 12px;'
        f'border:1px solid {LINE};">{_blend(inner)}</td>'
    )


def _stats(stats):
    """The 'Yesterday' grid: up to four cells with hairline borders."""
    width = int(100 / max(1, len(stats)))
    cells = "".join(_stat_cell(s, width) for s in stats)
    return (f'<tr><td style="padding-top:14px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="border-collapse:collapse;"><tr>{cells}</tr></table></td></tr>')


def _page(title, body):
    """Wrap section rows in the outer dark email shell."""
    return f"""<!DOCTYPE html>
<html lang="en" style="{_fill(OUTER)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{_e(title)}</title>
<style>
:root {{ color-scheme: dark; }}
u + .body .gmail-blend-screen {{ background:#000; mix-blend-mode:screen; }}
u + .body .gmail-blend-difference {{ background:#000; mix-blend-mode:difference; }}
</style>
</head>
<body class="body" bgcolor="{OUTER}" style="margin:0;padding:0;{_fill(OUTER)}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{OUTER}" style="{_fill(OUTER)}">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" border="0" bgcolor="{BG}" style="width:100%;max-width:{WIDTH}px;{_fill(BG)}">
<tr><td style="padding:40px 32px 32px 32px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
{body}
</table>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def render_sections(title, meta, note, sections, footer):
    """
    (html, text) for any Jarvis email. sections is a list of
      {"label": "Gym", "rows": [{"text", "sub"?, "right"?, "time"?, "miss"?}]}
      {"label": "Yesterday", "stats": [{"value", "label", "miss"?}]}
    Sections with no rows or stats are left out.
    """
    parts = [
        f'<tr><td style="font-family:{SANS};font-size:26px;line-height:1.2;font-weight:600;'
        f'letter-spacing:-0.5px;color:{INK};">{_blend(_e(title))}</td></tr>',
        f'<tr><td style="font-family:{SANS};font-size:14px;color:{SUB};padding-top:6px;">'
        f'{_blend(_e(meta))}</td></tr>',
    ]
    text = [title, meta or "", ""]

    if note:
        parts.append(
            f'<tr><td style="padding-top:28px;"><table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0"><tr><td width="2" bgcolor="{INK}" style="{_fill(INK)}width:2px;"></td>'
            f'<td style="padding-left:14px;font-family:{SANS};font-size:15px;line-height:1.6;color:{INK};">'
            f'{_blend(_e(note))}</td></tr></table></td></tr>')
        text += [note, ""]

    for section in sections:
        rows, stats = section.get("rows") or [], section.get("stats") or []
        if not rows and not stats:
            continue
        parts.append(_label(section["label"]))
        text.append(section["label"].upper())
        if stats:
            parts.append(_stats(stats))
            text += [f"  {s['label']}: {s['value']}" for s in stats]
        if rows:
            cells = []
            for r in rows:
                left = _time_cell(r["time"]) if r.get("time") else ""
                right = _right_cell(r["right"], miss=r.get("miss")) if r.get("right") else ""
                cells.append((left + _text_cell(r["text"], r.get("sub"), miss=r.get("miss")), right))
                line = "  " + (f"{r['time']:<6} " if r.get("time") else "") + r["text"]
                line += f"  {r['right']}" if r.get("right") else ""
                line += f" — {r['sub']}" if r.get("sub") else ""
                text.append(line)
            parts.append(_rows(cells))
        text.append("")

    parts.append(f'<tr><td style="padding-top:40px;font-family:{SANS};font-size:12px;color:{SUB};">'
                 f'{_blend(_e(footer))}</td></tr>')
    text.append(footer or "")
    return _page(title, "".join(parts)), "\n".join(text).strip() + "\n"


def render_brief(data):
    """(html, text) for the morning brief. See the module docstring for data."""
    sections = [
        {"label": "Owed", "rows": [{"text": o["label"], "right": o["value"], "miss": o.get("miss")}
                                   for o in data.get("owed") or []]},
        {"label": "Today", "rows": [{"time": r["time"], "text": r["title"], "sub": r.get("sub"),
                                     "miss": r.get("miss")} for r in data.get("today") or []]},
        {"label": "Due soon", "rows": [{"text": d["label"], "right": d["value"], "miss": d.get("miss")}
                                       for d in data.get("due") or []]},
        {"label": "Yesterday", "stats": data.get("yesterday") or []},
        {"label": "One push", "rows": [{"text": data["push"]}] if data.get("push") else []},
    ]
    return render_sections(data["title"], data.get("meta"), data.get("note"), sections, data.get("footer"))
