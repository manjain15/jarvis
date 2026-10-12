"""
Jarvis — Email Template
=======================
Renders the morning brief (and, later, the weekly review) in the approved
dark design (docs/brief_design.html) as email-safe HTML: tables, inline
styles and bgcolor attributes, because Gmail strips <style> blocks and most
layout CSS. Also returns a plain-text version for the text/plain part.

render_brief(data) takes plain values only; every number is computed by the
caller from Jarvis's data. Sections with nothing in them are left out.

    data = {
      "title":     "Monday 12 October",
      "meta":      "Week 5 · Pull day · 3 classes",
      "note":      "MATH2931 quiz is Thursday ...",          # optional
      "owed":      [{"label": "Gym make-up · by Tue", "value": "70 min", "miss": True}],
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


def _label(text):
    """Small grey uppercase section label with a hairline under it."""
    return (
        f'<tr><td style="padding:36px 0 0 0;">'
        f'<div style="font-family:{SANS};font-size:11px;font-weight:500;letter-spacing:1px;'
        f'text-transform:uppercase;color:{SUB};padding-bottom:10px;border-bottom:1px solid {LINE};">'
        f'{_e(text)}</div></td></tr>'
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
            f'color:{colour};">{_e(text)}{sub_html}</td>')


def _right_cell(text, miss=False):
    colour = MISS if miss else SUB
    return (f'<td valign="top" align="right" style="font-family:{MONO};font-size:13px;'
            f'color:{colour};white-space:nowrap;padding-left:16px;padding-top:1px;">{_e(text)}</td>')


def _time_cell(text):
    return (f'<td valign="top" width="56" style="font-family:{MONO};font-size:13px;color:{SUB};'
            f'width:56px;padding-top:1px;">{_e(text)}</td>')


def _stats(stats):
    """The 'Yesterday' grid: up to four cells with hairline borders."""
    width = int(100 / max(1, len(stats)))
    cells = "".join(
        f'<td valign="top" width="{width}%" bgcolor="{BG}" style="background:{BG};padding:14px 12px;'
        f'border:1px solid {LINE};">'
        f'<div style="font-family:{MONO};font-size:18px;line-height:1.1;font-weight:500;'
        f'color:{MISS if s.get("miss") else INK};">{_e(s["value"])}</div>'
        f'<div style="font-family:{SANS};font-size:12px;color:{SUB};padding-top:4px;">{_e(s["label"])}</div>'
        f'</td>'
        for s in stats
    )
    return (f'<tr><td style="padding-top:14px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="border-collapse:collapse;"><tr>{cells}</tr></table></td></tr>')


def render_brief(data):
    """(html, text) for the morning brief."""
    parts = [
        f'<tr><td style="font-family:{SANS};font-size:26px;line-height:1.2;font-weight:600;'
        f'letter-spacing:-0.5px;color:{INK};">{_e(data["title"])}</td></tr>',
        f'<tr><td style="font-family:{SANS};font-size:14px;color:{SUB};padding-top:6px;">'
        f'{_e(data.get("meta"))}</td></tr>',
    ]
    text = [data["title"], data.get("meta", ""), ""]

    if data.get("note"):
        parts.append(
            f'<tr><td style="padding-top:28px;"><table role="presentation" cellpadding="0" cellspacing="0" '
            f'border="0"><tr><td width="2" bgcolor="{INK}" style="background:{INK};width:2px;"></td>'
            f'<td style="padding-left:14px;font-family:{SANS};font-size:15px;line-height:1.6;color:{INK};">'
            f'{_e(data["note"])}</td></tr></table></td></tr>')
        text += [data["note"], ""]

    if data.get("owed"):
        parts.append(_label("Owed"))
        parts.append(_rows([(_text_cell(o["label"], miss=o.get("miss")), _right_cell(o["value"], miss=o.get("miss")))
                            for o in data["owed"]]))
        text += ["OWED"] + [f"  {o['label']}  {o['value']}" for o in data["owed"]] + [""]

    if data.get("today"):
        parts.append(_label("Today"))
        parts.append(_rows([(_time_cell(r["time"]), _text_cell(r["title"], r.get("sub"), miss=r.get("miss")))
                            for r in data["today"]]))
        text += ["TODAY"] + [f"  {r['time']:<6} {r['title']}" + (f" — {r['sub']}" if r.get("sub") else "")
                             for r in data["today"]] + [""]

    if data.get("due"):
        parts.append(_label("Due soon"))
        parts.append(_rows([(_text_cell(d["label"], miss=d.get("miss")), _right_cell(d["value"], miss=d.get("miss")))
                            for d in data["due"]]))
        text += ["DUE SOON"] + [f"  {d['label']}  {d['value']}" for d in data["due"]] + [""]

    if data.get("yesterday"):
        parts.append(_label("Yesterday"))
        parts.append(_stats(data["yesterday"]))
        text += ["YESTERDAY"] + [f"  {s['label']}: {s['value']}" for s in data["yesterday"]] + [""]

    if data.get("push"):
        parts.append(_label("One push"))
        parts.append(_rows([(_text_cell(data["push"]), "")]))
        text += ["ONE PUSH", f"  {data['push']}", ""]

    parts.append(f'<tr><td style="padding-top:40px;font-family:{SANS};font-size:12px;color:{SUB};">'
                 f'{_e(data.get("footer"))}</td></tr>')
    text.append(data.get("footer", ""))

    body = "".join(parts)
    page = f"""<!DOCTYPE html>
<html lang="en" style="background:{OUTER};">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{_e(data["title"])}</title>
</head>
<body bgcolor="{OUTER}" style="margin:0;padding:0;background:{OUTER};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{OUTER}" style="background:{OUTER};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" border="0" bgcolor="{BG}" style="width:100%;max-width:{WIDTH}px;background:{BG};">
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
    return page, "\n".join(text).strip() + "\n"
