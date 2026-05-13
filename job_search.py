"""
Jarvis — Job Links
===================
A curated list of internship pages for Manav's target companies.
No API calls, no cost. Surfaced in the morning brief and weekly review.

UPDATE THIS FILE when you find new companies worth watching.
"""

# ── Target company intern pages ───────────────────────────────────────────────
# Update these if URLs change

TARGET_LINKS = [
    {
        "company":  "Google",
        "role":     "STEP / SWE Intern",
        "url":      "https://careers.google.com/jobs/results/?category=ENGINEERING&employment_type=INTERN&location=Australia",
        "notes":    "Check for STEP Intern (Year 1-2) and SWE Intern (penultimate). Opens ~March for summer.",
        "eligible": True,
    },
    {
        "company":  "Amazon",
        "role":     "SDE Intern",
        "url":      "https://www.amazon.jobs/en/search?base_query=intern&loc_query=Australia&job_type=intern",
        "notes":    "Amazon internships open year-round. Apply early — rolling admissions.",
        "eligible": True,
    },
    {
        "company":  "Canva",
        "role":     "Software Engineer Intern",
        "url":      "https://www.canva.com/careers/internships/",
        "notes":    "Sydney-based. Summer intake opens ~April. Strong culture fit for AI/automation skills.",
        "eligible": True,
    },
    {
        "company":  "Anthropic",
        "role":     "Research/Engineering Intern",
        "url":      "https://www.anthropic.com/careers",
        "notes":    "US-based but worth applying. Strong fit given AI background. Very competitive.",
        "eligible": True,
    },
    {
        "company":  "Optiver",
        "role":     "Software Developer Intern",
        "url":      "https://optiver.com/working-at-optiver/career-opportunities/?search=intern",
        "notes":    "Requires penultimate year — eligible next year. Opens ~Feb for summer intake.",
        "eligible": False,  # Year 2 — not yet eligible
        "eligible_from": "2027",
    },
]

# ── Discovery companies — expand your radar ───────────────────────────────────
# Companies worth watching that aren't on your main list yet

DISCOVERY_LINKS = [
    {
        "company":  "Atlassian",
        "role":     "Software Engineer Intern",
        "url":      "https://www.atlassian.com/company/careers/students",
        "notes":    "Sydney HQ. Strong graduate program. Check for early talent/intern programs.",
        "eligible": True,
    },
    {
        "company":  "Atlassian",
        "role":     "Intern / Student Programs",
        "url":      "https://www.atlassian.com/company/careers/students",
        "notes":    "Sydney HQ. Excellent for SWE and product roles.",
        "eligible": True,
    },
    {
        "company":  "Prospa",
        "role":     "Tech Intern",
        "url":      "https://www.prospa.com/careers",
        "notes":    "Sydney fintech. Good for automation/AI skills.",
        "eligible": True,
    },
    {
        "company":  "Afterpay / Block",
        "role":     "Engineering Intern",
        "url":      "https://www.block.xyz/careers",
        "notes":    "Sydney office. Fintech + engineering. Competitive but worth watching.",
        "eligible": True,
    },
    {
        "company":  "Macquarie Group",
        "role":     "Technology Intern",
        "url":      "https://www.macquarie.com/au/en/careers/students-and-graduates.html",
        "notes":    "Sydney. Strong tech division. Opens ~Feb. Competitive.",
        "eligible": True,
    },
    {
        "company":  "WiseTech Global",
        "role":     "Software Engineer Intern",
        "url":      "https://www.wisetechglobal.com/join-us/current-opportunities/",
        "notes":    "Sydney logistics tech. Less competitive than FAANG. Good experience.",
        "eligible": True,
    },
    {
        "company":  "Rokt",
        "role":     "Engineering Intern",
        "url":      "https://rokt.com/careers/",
        "notes":    "Sydney AI/ML company. Strong Python/ML culture. Worth watching.",
        "eligible": True,
    },
    {
        "company":  "Culture Amp",
        "role":     "Engineering Intern",
        "url":      "https://www.cultureamp.com/about/careers",
        "notes":    "Melbourne/Sydney. HR tech with strong engineering culture.",
        "eligible": True,
    },
]

# ── Also check these job boards ───────────────────────────────────────────────

JOB_BOARDS = [
    {
        "name":  "Prosple (AU tech internships)",
        "url":   "https://au.prosple.com/search-jobs?opportunity_types=156&study_fields=2000000&locations=6252001",
        "notes": "Best aggregator for Australian tech internships. Check weekly.",
    },
    {
        "name":  "GradConnection",
        "url":   "https://au.gradconnection.com/internships/information-technology/",
        "notes": "Good for Australian internships. Filter by IT/Software.",
    },
    {
        "name":  "LinkedIn internships",
        "url":   "https://www.linkedin.com/jobs/search/?keywords=software+engineer+intern&location=Sydney&f_E=1",
        "notes": "Set up a job alert here for daily emails.",
    },
]


def get_links_for_brief():
    """
    Returns a formatted string for the morning brief.
    Shows eligible target + discovery companies.
    No API calls — just a curated reminder.
    """
    eligible_targets    = [c for c in TARGET_LINKS if c.get("eligible")]
    not_yet_eligible    = [c for c in TARGET_LINKS if not c.get("eligible")]
    eligible_discovery  = [c for c in DISCOVERY_LINKS if c.get("eligible")]

    lines = ["INTERNSHIP TARGETS — open these and check for new roles:"]

    lines.append("\n  Your target companies:")
    for c in eligible_targets:
        lines.append(f"  • {c['company']} — {c['role']}")
        lines.append(f"    {c['url']}")
        lines.append(f"    Note: {c['notes']}")

    lines.append("\n  Worth adding to your radar:")
    for c in eligible_discovery[:4]:
        lines.append(f"  • {c['company']} — {c['role']}")
        lines.append(f"    {c['url']}")

    lines.append("\n  Not yet eligible (next year):")
    for c in not_yet_eligible:
        lines.append(f"  • {c['company']} — eligible from {c.get('eligible_from', 'penultimate year')}")

    return "\n".join(lines)


def get_links_for_weekly_review():
    """
    Returns a richer summary for the weekly review.
    Includes job boards and a weekly action prompt.
    """
    lines = get_links_for_brief()
    lines += "\n\n  Job boards to check this week:"
    for b in JOB_BOARDS:
        lines += f"\n  • {b['name']}: {b['url']}"
    return lines


if __name__ == "__main__":
    print("\n📋  Jarvis job links\n")
    print(get_links_for_brief())
