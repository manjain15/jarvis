"""Email shell: stays dark under Gmail's colour inversion, plain text unchanged."""

import re

import email_template

BRIEF = {
    "title": "Monday 12 October",
    "meta": "Week 5 · Pull day",
    "note": "<b>Quiz</b> & test",
    "owed": [{"label": "Gym make-up", "value": "55 min", "miss": True}],
    "today": [{"time": "06:30", "title": "Pull", "sub": "Leave by 06:25"}],
    "due": [{"label": "MATH2931 Assignment 2", "value": "Wed 11 Nov · 30d"}],
    "yesterday": [
        {"value": "Missed", "label": "Legs", "miss": True},
        {"value": "Done", "label": "Steps", "miss": False},
    ],
    "push": "Ship the login flow.",
    "footer": "jarvis · 07:05",
}

PLAIN = """Monday 12 October
Week 5 · Pull day

<b>Quiz</b> & test

OWED
  Gym make-up  55 min

TODAY
  06:30  Pull — Leave by 06:25

DUE SOON
  MATH2931 Assignment 2  Wed 11 Nov · 30d

YESTERDAY
  Legs: Missed
  Steps: Done

ONE PUSH
  Ship the login flow.

jarvis · 07:05
"""


def _page():
    return email_template.render_brief(BRIEF)


def test_color_scheme_stays_dark():
    page, _ = _page()
    assert '<meta name="color-scheme" content="dark">' in page
    assert '<meta name="supported-color-schemes" content="dark">' in page
    assert ":root { color-scheme: dark; }" in page
    assert '<body class="body"' in page


def test_backgrounds_use_inversion_proof_gradients():
    page, _ = _page()
    for colour in (email_template.OUTER, email_template.BG, email_template.INK):
        assert f'bgcolor="{colour}"' in page
        assert f"background-color:{colour}" in page
        assert f"background-image:linear-gradient({colour}, {colour})" in page
    # Every bgcolor lives on a tag that also carries the matching gradient.
    for match in re.finditer(r'bgcolor="(#[0-9a-fA-F]{6})"', page):
        colour = match.group(1)
        tag = page[page.rfind("<", 0, match.start()):page.find(">", match.end())]
        assert f"background-color:{colour}" in tag
        assert f"background-image:linear-gradient({colour}, {colour})" in tag
    assert page.count(f'bgcolor="{email_template.BG}"') >= 3  # card + stat tiles
    assert f'bgcolor="{email_template.INK}"' in page  # note accent bar


def test_gmail_blend_wrappers_are_scoped_to_gmail():
    page, _ = _page()
    style = page.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "u + .body .gmail-blend-screen { background:#000; mix-blend-mode:screen; }" in style
    assert "u + .body .gmail-blend-difference { background:#000; mix-blend-mode:difference; }" in style
    # No bare rule: non-Gmail clients must not apply the blend.
    assert style.count(".gmail-blend-screen") == 1
    assert style.count(".gmail-blend-difference") == 1
    assert "\n.gmail-blend-screen {" not in style
    assert not style.strip().startswith(".gmail-blend")
    screens = page.count('class="gmail-blend-screen"')
    diffs = page.count('class="gmail-blend-difference"')
    assert screens == diffs >= 1
    # Title, meta, note, section labels, row text, stat tiles and footer.
    assert screens >= 12
    body = page.split("<body", 1)[1]
    assert body.index("gmail-blend-screen") < body.index("Monday 12 October")
    assert "Monday 12 October" in body.split("gmail-blend-difference", 1)[1]
    assert email_template.MISS in page
    assert "&lt;b&gt;Quiz&lt;/b&gt; &amp; test" in page


def test_plain_text_part_is_unchanged():
    page, text = _page()
    assert text == PLAIN
    assert "gmail-blend" not in text
    assert "<div" not in text
    assert "<style" not in text
    assert "linear-gradient" not in text
    assert "<style>" in page
