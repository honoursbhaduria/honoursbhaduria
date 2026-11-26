#!/usr/bin/env python3
"""
github-pet: turn your GitHub activity into an animated profile README pet.

It asks the GitHub GraphQL API for your contribution stats, works out a level,
mood and evolution stage, then renders a self-contained animated SVG and
optionally updates a marked block in your profile README.

Standard library only. Python 3.9+.

    python3 scripts/pet.py --demo
    python3 scripts/pet.py --login octocat --token "$GH_TOKEN" --readme README.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import escape

API_URL = "https://api.github.com/graphql"
PUBLIC_CALENDAR = "https://github.com/users/{login}/contributions"

USER_QUERY = """
query($login: String!) {
  user(login: $login) {
    login
    name
    createdAt
  }
}
"""

CONTRIB_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalRepositoriesWithContributedCommits
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
  }
}
"""

START_MARK = "<!-- PET:START -->"
END_MARK = "<!-- PET:END -->"

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace"

STAGE_NAMES = ["Egg", "Hatchling", "Sprout", "Scout", "Knight", "Wizard", "Legend"]
STAGE_COLORS = ["#f2cc8f", "#7ee787", "#56d364", "#3fb950", "#58a6ff", "#bc8cff", "#ffd33d"]
STAGE_MIN_LEVEL = [1, 3, 6, 10, 15, 20, 30]
LEVEL_DIVISOR = 40

QUIPS = {
    "ecstatic": ["I feel unstoppable!", "So many green squares!", "Let's ship everything!"],
    "happy": ["Another green square!", "Commits taste great.", "This is a good streak."],
    "content": ["Nice and steady.", "One commit at a time.", "I like green squares."],
    "bored": ["Feed me a commit?", "It's quiet today...", "Push something, please."],
    "sleepy": ["Zzz... commits...", "Five more minutes.", "Wake me with a push."],
    "lonely": ["It's been days...", "Did you forget me?", "A tiny commit, please?"],
}

STYLE = """
.pet { animation: float 4.4s ease-in-out infinite; transform-box: fill-box; transform-origin: center bottom; }
.shadow { animation: shadow 4.4s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }
.eyes { animation: blink 5.6s infinite; transform-box: fill-box; transform-origin: center; }
.tail { animation: wag 2.8s ease-in-out infinite; transform-box: fill-box; transform-origin: 0% 85%; }
.sparkle { animation: twinkle 2.4s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }
.bob { animation: bob 3.2s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }
@keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-7px); } }
@keyframes shadow { 0%, 100% { transform: scaleX(1); opacity: .35; } 50% { transform: scaleX(.9); opacity: .25; } }
@keyframes blink { 0%, 92%, 100% { transform: scaleY(1); } 95.5% { transform: scaleY(.08); } }
@keyframes wag { 0%, 100% { transform: rotate(-7deg); } 50% { transform: rotate(11deg); } }
@keyframes twinkle { 0%, 100% { opacity: .15; transform: scale(.8); } 50% { opacity: 1; transform: scale(1.15); } }
@keyframes bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-3px); } }
@media (prefers-reduced-motion: reduce) {
  .pet, .shadow, .eyes, .tail, .sparkle, .bob { animation: none !important; }
}
"""


# --------------------------------------------------------------------------- #
# GitHub GraphQL
# --------------------------------------------------------------------------- #


def gql(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "github-pet/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"GitHub API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach the GitHub API: {exc.reason}") from exc

    if body.get("errors"):
        raise SystemExit("GitHub API error: " + json.dumps(body["errors"]))
    return body.get("data") or {}


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def day_bounds(day: date, end: bool = False) -> str:
    clock = "23:59:59Z" if end else "00:00:00Z"
    return f"{day.isoformat()}T{clock}"


def empty_totals() -> dict:
    return {"commits": 0, "issues": 0, "prs": 0, "reviews": 0, "repos": 0, "restricted": 0}


def add_totals(totals: dict, collection: dict) -> None:
    totals["commits"] += collection.get("totalCommitContributions", 0)
    totals["issues"] += collection.get("totalIssueContributions", 0)
    totals["prs"] += collection.get("totalPullRequestContributions", 0)
    totals["reviews"] += collection.get("totalPullRequestReviewContributions", 0)
    totals["repos"] += collection.get("totalRepositoriesWithContributedCommits", 0)
    totals["restricted"] += collection.get("restrictedContributionsCount", 0)


def fetch_raw(token: str, login: str, now: datetime) -> dict:
    user = gql(token, USER_QUERY, {"login": login}).get("user")
    if not user:
        raise SystemExit(f"GitHub user '{login}' was not found.")

    created = parse_dt(user["createdAt"]).date()
    today = now.date()
    window_from = today - timedelta(days=364)
    window_to = today + timedelta(days=1)

    recent = gql(
        token,
        CONTRIB_QUERY,
        {"login": login, "from": day_bounds(window_from), "to": day_bounds(window_to)},
    )["user"]["contributionsCollection"]

    totals = empty_totals()
    add_totals(totals, recent)

    # contributionsCollection only accepts a one-year range, so walk the rest
    # of the account's life a year at a time for lifetime XP.
    cursor = created
    while cursor < window_from:
        end = min(cursor + timedelta(days=364), window_from - timedelta(days=1))
        chunk = gql(
            token,
            CONTRIB_QUERY,
            {"login": login, "from": day_bounds(cursor), "to": day_bounds(end, end=True)},
        )["user"]["contributionsCollection"]
        add_totals(totals, chunk)
        cursor = end + timedelta(days=1)

    days = []
    calendar = recent.get("contributionCalendar") or {}
    for week in calendar.get("weeks", []):
        for day in week.get("contributionDays", []):
            when = date.fromisoformat(day["date"])
            if window_from <= when <= today:
                days.append((when, day.get("contributionCount", 0)))

    return {
        "login": user.get("login") or login,
        "display_name": user.get("name") or login,
        "created": created,
        "days": days,
        **totals,
    }


def fetch_public_calendar(login: str, now: datetime) -> dict:
    """Fallback for local previews: read the public contribution calendar.

    No token required. This only covers the last year and cannot see private
    contributions, so the GraphQL path is still the real thing.
    """
    request = urllib.request.Request(
        PUBLIC_CALENDAR.format(login=login),
        headers={"User-Agent": "github-pet/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not read the public contribution calendar: {exc.reason}") from exc

    pattern = re.compile(
        r'<td[^>]*data-date="([0-9-]+)"[^>]*id="(contribution-day-component-[0-9-]+)"[^>]*>.*?'
        r'<tool-tip[^>]*for="\2"[^>]*>(.*?)</tool-tip>',
        re.S,
    )
    today = now.date()
    days = []
    for date_str, _component, tooltip in pattern.findall(html):
        match = re.match(r"(\d+|No) contribution", tooltip)
        count = 0 if (not match or match.group(1) == "No") else int(match.group(1))
        when = date.fromisoformat(date_str)
        if when <= today:
            days.append((when, count))

    if not days:
        raise SystemExit(
            "Could not parse the public contribution calendar. GitHub may have changed its markup; "
            "use a token with the GraphQL API instead."
        )

    days.sort()
    return {
        "login": login,
        "display_name": login,
        "created": days[0][0],
        "commits": sum(count for _, count in days),
        "prs": 0,
        "issues": 0,
        "reviews": 0,
        "repos": 0,
        "restricted": 0,
        "days": days,
        "scope": "calendar",
    }


# --------------------------------------------------------------------------- #
# Pet maths
# --------------------------------------------------------------------------- #


def current_streak(counts: dict, today: date) -> int:
    cursor = today
    if counts.get(cursor, 0) == 0:
        cursor -= timedelta(days=1)
    streak = 0
    while counts.get(cursor, 0) > 0:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def longest_streak(days: list) -> int:
    best = run = 0
    previous = None
    for when, count in sorted(days):
        if count > 0:
            run = run + 1 if previous is not None and (when - previous).days == 1 else 1
            best = max(best, run)
            previous = when
        else:
            run = 0
            previous = None
    return best


def xp_for_level(level: int) -> int:
    return LEVEL_DIVISOR * (level - 1) ** 2


def level_for_xp(xp: int) -> int:
    return max(1, min(99, 1 + int(math.sqrt(max(xp, 0) / LEVEL_DIVISOR))))


def mood_for(commits_7: int, days_since) -> str:
    if days_since is None or days_since > 14:
        return "lonely"
    if commits_7 == 0 and days_since >= 3:
        return "sleepy"
    if commits_7 == 0:
        return "bored"
    if commits_7 < 5:
        return "content"
    if commits_7 < 15:
        return "happy"
    return "ecstatic"


def build_pet(raw: dict, now: datetime, pet_name: str) -> dict:
    days = sorted(raw["days"])
    counts = dict(days)
    today = now.date()

    streak = current_streak(counts, today)
    longest = longest_streak(days)
    week_cutoff = today - timedelta(days=6)
    month_cutoff = today - timedelta(days=29)
    commits_7 = sum(count for when, count in days if when >= week_cutoff)
    active_30 = sum(1 for when, count in days if when >= month_cutoff and count > 0)

    contributed = [when for when, count in days if count > 0]
    last_day = max(contributed) if contributed else None
    days_since = (today - last_day).days if last_day else None

    xp = (
        raw["commits"] * 3
        + raw["prs"] * 10
        + raw["issues"] * 6
        + raw["reviews"] * 8
        + raw["repos"] * 15
        + raw["restricted"] * 3
        + streak * 5
        + longest * 2
        + active_30 * 2
    )

    level = level_for_xp(xp)
    previous_xp = xp_for_level(level)
    next_xp = xp_for_level(level + 1)
    progress = 1.0 if next_xp <= previous_xp else (xp - previous_xp) / (next_xp - previous_xp)
    progress = max(0.0, min(1.0, progress))

    stage = 0
    for index, minimum in enumerate(STAGE_MIN_LEVEL):
        if level >= minimum:
            stage = index
    mood = mood_for(commits_7, days_since)
    quips = QUIPS[mood]
    quip = quips[(now.toordinal() + level * 7) % len(quips)]

    return {
        "pet_name": pet_name,
        "login": raw["login"],
        "level": level,
        "xp": xp,
        "xp_next": next_xp,
        "progress": progress,
        "stage": stage,
        "stage_name": STAGE_NAMES[stage],
        "mood": mood,
        "mood_label": mood.capitalize(),
        "quip": quip,
        "streak": streak,
        "longest": longest,
        "commits_7": commits_7,
        "commits_all": raw["commits"],
        "active_30": active_30,
        "prs": raw["prs"],
        "days_since": days_since,
        "commits_label": "contributions this year" if raw.get("scope") == "calendar" else "commits all-time",
        "week_label": "CONTRIB 7D" if raw.get("scope") == "calendar" else "COMMITS 7D",
        "week_summary": "contributions this week" if raw.get("scope") == "calendar" else "commits this week",
        "third_value": raw["commits"] if raw.get("scope") == "calendar" else raw["prs"],
        "third_label": "CONTRIBUTIONS" if raw.get("scope") == "calendar" else "PULL REQUESTS",
        "source_label": "public contribution calendar" if raw.get("scope") == "calendar" else "powered by the GitHub GraphQL API",
        "updated": today.isoformat(),
    }


def demo_raw(now: datetime) -> dict:
    today = now.date()
    days = []
    for offset in range(365):
        when = today - timedelta(days=offset)
        count = 0 if (offset > 9 and offset % 11 == 0) else (offset % 6) + 1
        if offset < 9:
            count = 2 + (offset % 4)
        days.append((when, count))
    return {
        "login": "octocat",
        "display_name": "The Octocat",
        "created": date(2018, 4, 1),
        "commits": 9840,
        "prs": 420,
        "issues": 210,
        "reviews": 360,
        "repos": 48,
        "restricted": 300,
        "days": days,
    }


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def mix(color: str, other: str, amount: float) -> str:
    a = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(other[i : i + 2], 16) for i in (1, 3, 5))
    blended = tuple(round(x + (y - x) * amount) for x, y in zip(a, b))
    return "#%02x%02x%02x" % blended


def fmt(number: int) -> str:
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M".replace(".0M", "M")
    if number >= 1000:
        return f"{number / 1000:.1f}k".replace(".0k", "k")
    return str(number)


def wrap(text: str, width: int) -> list:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if len(trial) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fed_text(days_since) -> str:
    if days_since is None:
        return "never"
    if days_since == 0:
        return "today"
    if days_since == 1:
        return "yesterday"
    return f"{days_since} days ago"


def sparkles() -> str:
    points = [(104, 152, 0), (268, 130, 0.7), (94, 238, 1.4), (280, 236, 2.1), (185, 84, 1.1)]
    return "".join(
        f'<path class="sparkle" style="animation-delay:{delay}s" '
        f'd="M{x} {y} l3 7 7 3 -7 3 -3 7 -3 -7 -7 -3 7 -3 Z" fill="#ffd33d" opacity=".9"/>'
        for x, y, delay in points
    )


def mouth_for(mood: str) -> str:
    if mood == "ecstatic":
        return '<path d="M169 219 Q185 236 201 219 Q185 228 169 219 Z" fill="#0b0f14"/>'
    if mood == "happy":
        return '<path d="M171 219 Q185 232 199 219" fill="none" stroke="#0b0f14" stroke-width="4" stroke-linecap="round"/>'
    if mood == "content":
        return '<path d="M176 221 Q185 227 194 221" fill="none" stroke="#0b0f14" stroke-width="4" stroke-linecap="round"/>'
    if mood == "bored":
        return '<path d="M177 223 h16" stroke="#0b0f14" stroke-width="4" stroke-linecap="round"/>'
    if mood == "sleepy":
        return '<path d="M179 222 q6 4 12 0" fill="none" stroke="#0b0f14" stroke-width="4" stroke-linecap="round"/>'
    return '<path d="M175 226 Q185 217 195 226" fill="none" stroke="#0b0f14" stroke-width="4" stroke-linecap="round"/>'


def accessories(stage: int) -> str:
    out = ""
    if stage in (2, 3, 4):
        out += """
<g class="bob">
  <path d="M186 150 C 185 132 185 120 187 106" stroke="#2ea043" stroke-width="5" fill="none" stroke-linecap="round"/>
  <path d="M187 114 C 168 104 155 108 148 120 C 163 130 178 126 187 114 Z" fill="#3fb950"/>
  <path d="M187 106 C 204 94 217 98 224 110 C 209 121 195 117 187 106 Z" fill="#56d364"/>
</g>"""
    if stage >= 3:
        out += """
<path d="M126 236 C 150 252 220 252 244 236" stroke="#f85149" stroke-width="12" fill="none" stroke-linecap="round"/>
<path d="M236 240 L254 270 L236 266 Z" fill="#da3633"/>"""
    if stage >= 2:
        out += """
<g class="bob">
  <path d="M256 246 L296 170" stroke="#c9d1d9" stroke-width="4.5" stroke-linecap="round"/>
  <path d="M292 178 L306 162 L302 181 L297 184 Z" fill="#e6edf3"/>
  <path d="M245 242 L267 250" stroke="#8b949e" stroke-width="7" stroke-linecap="round"/>
  <path d="M256 246 L249 260" stroke="#6e7681" stroke-width="5" stroke-linecap="round"/>
</g>"""
    if stage >= 4:
        out += """
<g class="bob">
  <path d="M248 170 l4.6 9.3 10.3 1.5 -7.5 7.3 1.8 10.2 -9.2 -4.8 -9.2 4.8 1.8 -10.2 -7.5 -7.3 10.3 -1.5 Z" fill="#ffd33d" stroke="#d29922" stroke-width="1.5"/>
</g>"""
    if stage == 5:
        out += """
<g>
  <path d="M130 140 L185 64 L240 140 Q 185 158 130 140 Z" fill="#8957e5" stroke="#6e40c9" stroke-width="3" stroke-linejoin="round"/>
  <path d="M138 134 Q 185 120 232 134" stroke="#ffd33d" stroke-width="5" fill="none" stroke-linecap="round"/>
  <path d="M185 82 l3 7 8 1 -6 5 2 8 -7 -4 -7 4 2 -8 -6 -5 8 -1 Z" fill="#ffd33d" opacity=".95"/>
</g>"""
    if stage >= 6:
        out += """
<g class="bob">
  <path d="M144 130 L155 96 L173 117 L185 88 L197 117 L215 96 L226 130 Q 185 144 144 130 Z" fill="#ffd33d" stroke="#d29922" stroke-width="3" stroke-linejoin="round"/>
  <circle cx="185" cy="106" r="4.5" fill="#f85149"/>
</g>"""
    return out


def pet_art(p: dict, body: str, dark: str, light: str) -> str:
    if p["stage"] == 0:
        return f"""<g class="pet">
  <path d="M185 118 C 236 118 258 178 258 214 C 258 250 226 270 185 270 C 144 270 112 250 112 214 C 112 178 134 118 185 118 Z" fill="url(#shell)" stroke="#b98a55" stroke-width="3"/>
  <path d="M150 168 l16 12 l-11 13 l18 9 l-9 12" fill="none" stroke="#b98a55" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  <g class="eyes">
    <ellipse cx="206" cy="206" rx="7" ry="9" fill="#241a10"/>
    <circle cx="208" cy="203" r="2.4" fill="#fff"/>
  </g>
  <ellipse cx="185" cy="248" rx="26" ry="8" fill="#000" opacity="0.08"/>
  {sparkles()}
</g>"""

    mood = p["mood"]
    extra = ""
    if mood == "sleepy":
        extra += f'<text class="bob" x="244" y="150" font-family="{MONO}" font-size="17" font-weight="700" fill="#8b949e">z z</text>'
    if mood == "lonely":
        extra += '<path d="M222 208 q4 9 0 13 q-4 -4 0 -13" fill="#58a6ff" opacity=".9"/>'

    return f"""<g class="pet">
  <path class="tail" d="M246 236 C 282 232 292 202 272 188" fill="none" stroke="{dark}" stroke-width="13" stroke-linecap="round"/>
  <path d="M143 158 L132 106 L178 138 Z" fill="{dark}" stroke="{dark}" stroke-width="6" stroke-linejoin="round"/>
  <path d="M227 158 L238 106 L192 138 Z" fill="{dark}" stroke="{dark}" stroke-width="6" stroke-linejoin="round"/>
  <path d="M143 152 L137 122 L166 140 Z" fill="{light}" opacity=".5"/>
  <path d="M227 152 L233 122 L204 140 Z" fill="{light}" opacity=".5"/>
  <ellipse cx="185" cy="206" rx="70" ry="62" fill="url(#body)" stroke="{dark}" stroke-width="3"/>
  <ellipse cx="185" cy="228" rx="43" ry="33" fill="{light}" opacity=".55"/>
  <ellipse cx="160" cy="264" rx="19" ry="9" fill="{dark}"/>
  <ellipse cx="210" cy="264" rx="19" ry="9" fill="{dark}"/>
  <ellipse cx="118" cy="222" rx="12" ry="18" fill="{dark}" transform="rotate(18 118 222)"/>
  <ellipse cx="252" cy="222" rx="12" ry="18" fill="{dark}" transform="rotate(-18 252 222)"/>
  <ellipse cx="146" cy="214" rx="10" ry="6" fill="#ff8fa3" opacity=".45"/>
  <ellipse cx="224" cy="214" rx="10" ry="6" fill="#ff8fa3" opacity=".45"/>
  <g class="eyes">
    <ellipse cx="163" cy="196" rx="9" ry="11" fill="#0b0f14"/>
    <ellipse cx="207" cy="196" rx="9" ry="11" fill="#0b0f14"/>
    <circle cx="166" cy="192" r="3" fill="#fff"/>
    <circle cx="210" cy="192" r="3" fill="#fff"/>
  </g>
  {mouth_for(mood)}
  {extra}
  {accessories(p["stage"])}
  {sparkles()}
</g>"""


def speech(p: dict) -> str:
    lines = wrap(p["quip"], 30)[:2]
    if len(lines) == 1:
        body = (
            f'<text x="185" y="68" text-anchor="middle" font-family="{SANS}" '
            f'font-size="15" fill="#e6edf3">{escape(lines[0])}</text>'
        )
    else:
        body = (
            f'<text text-anchor="middle" font-family="{SANS}" font-size="15" fill="#e6edf3">'
            f'<tspan x="185" y="58">{escape(lines[0])}</tspan>'
            f'<tspan x="185" y="79">{escape(lines[1])}</tspan></text>'
        )
    return f"""<g>
  <rect x="52" y="28" width="266" height="66" rx="16" fill="#161b22" stroke="#30363d"/>
  <path d="M108 93 L134 93 L120 111 Z" fill="#161b22" stroke="#30363d"/>
  <rect x="109" y="91" width="26" height="4" fill="#161b22"/>
  {body}
</g>"""


def panel(p: dict, body: str) -> str:
    name = escape(p["pet_name"])
    stage = escape(p["stage_name"])
    level = p["level"]
    xp_text = f'{fmt(p["xp"])} XP'
    next_text = "max level" if level >= 99 else f'{fmt(max(0, p["xp_next"] - p["xp"]))} XP to Lv {level + 1}'
    fill_width = max(0.0, 470 * p["progress"])
    if 0 < fill_width < 8:
        fill_width = 8

    chips = [
        (fmt(p["streak"]), "DAY STREAK"),
        (fmt(p["commits_7"]), p["week_label"]),
        (fmt(p["third_value"]), p["third_label"]),
        (fmt(p["active_30"]), "ACTIVE 30D"),
    ]
    chip_svg = ""
    for index, (number, label) in enumerate(chips):
        x = 370 + index * 120
        chip_svg += (
            f'<rect x="{x}" y="172" width="110" height="60" rx="12" fill="#161b22" stroke="#30363d"/>'
            f'<text x="{x + 55}" y="200" text-anchor="middle" font-family="{MONO}" '
            f'font-size="22" font-weight="700" fill="#e6edf3">{number}</text>'
            f'<text x="{x + 55}" y="219" text-anchor="middle" font-family="{MONO}" '
            f'font-size="9.5" letter-spacing="1" fill="#8b949e">{label}</text>'
        )

    mood = escape(p["mood_label"])
    fed = escape(fed_text(p["days_since"]))

    return f"""<g>
  <text x="370" y="66" font-family="{SANS}" font-size="30" font-weight="700" fill="#e6edf3">{name}</text>
  <text x="370" y="92" font-family="{SANS}" font-size="13" fill="#8b949e">Stage {stage} · {fmt(p["commits_all"])} {p["commits_label"]}</text>
  <circle cx="836" cy="64" r="31" fill="#161b22" stroke="{body}" stroke-width="2.5"/>
  <text x="836" y="58" text-anchor="middle" font-family="{MONO}" font-size="9" letter-spacing="1.5" fill="#8b949e">LEVEL</text>
  <text x="836" y="80" text-anchor="middle" font-family="{SANS}" font-size="24" font-weight="700" fill="#e6edf3">{level}</text>
  <text x="370" y="124" font-family="{MONO}" font-size="12" fill="#8b949e">{xp_text}</text>
  <text x="840" y="124" text-anchor="end" font-family="{MONO}" font-size="12" fill="#8b949e">{next_text}</text>
  <rect x="370" y="132" width="470" height="16" rx="8" fill="#21262d" stroke="#30363d"/>
  <rect x="370" y="132" width="{fill_width:.1f}" height="16" rx="8" fill="url(#xp)"/>
  {chip_svg}
  <text x="370" y="260" font-family="{SANS}" font-size="13" fill="#8b949e">Mood <tspan fill="#e6edf3" font-weight="600">{mood}</tspan></text>
  <text x="520" y="260" font-family="{SANS}" font-size="13" fill="#8b949e">Fed <tspan fill="#e6edf3" font-weight="600">{fed}</tspan></text>
  <text x="840" y="260" text-anchor="end" font-family="{MONO}" font-size="11" fill="#8b949e">updated {p["updated"]}</text>
  <text x="370" y="290" font-family="{SANS}" font-size="11" fill="#6e7681">@{escape(p["login"])} · {escape(p["source_label"])}</text>
  <text x="840" y="290" text-anchor="end" font-family="{SANS}" font-size="11" fill="#6e7681">longest streak {p["longest"]}d</text>
</g>"""


def build_svg(p: dict) -> str:
    body = STAGE_COLORS[p["stage"]]
    dark = mix(body, "#000000", 0.38)
    light = mix(body, "#ffffff", 0.5)

    stars = "".join(
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="#e6edf3" opacity="{o}"/>'
        for x, y, r, o in [
            (30, 34, 1.3, 0.35),
            (72, 96, 1.0, 0.22),
            (118, 40, 1.6, 0.30),
            (330, 60, 1.2, 0.25),
            (352, 250, 1.4, 0.20),
            (60, 286, 1.1, 0.25),
            (300, 150, 1.0, 0.18),
            (24, 190, 1.2, 0.20),
            (862, 300, 1.3, 0.18),
        ]
    )

    title = f'{p["pet_name"]} - GitHub pet'
    desc = (
        f'Level {p["level"]} {p["stage_name"]} with a {p["streak"]}-day streak. '
        f'Mood: {p["mood"]}. {p["xp"]} XP from GitHub contributions.'
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="320" viewBox="0 0 900 320" role="img" aria-labelledby="petTitle petDesc">
<title id="petTitle">{escape(title)}</title>
<desc id="petDesc">{escape(desc)}</desc>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#0d1117"/>
    <stop offset="1" stop-color="#161b22"/>
  </linearGradient>
  <linearGradient id="body" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="{light}"/>
    <stop offset="0.55" stop-color="{body}"/>
    <stop offset="1" stop-color="{dark}"/>
  </linearGradient>
  <linearGradient id="shell" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#fff3d6"/>
    <stop offset="1" stop-color="#e8c78f"/>
  </linearGradient>
  <linearGradient id="xp" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#2ea043"/>
    <stop offset="1" stop-color="#7ee787"/>
  </linearGradient>
</defs>
<style>{STYLE}</style>
<rect x="0.5" y="0.5" width="899" height="319" rx="18" fill="url(#bg)" stroke="#30363d"/>
{stars}
<line x1="350" y1="42" x2="350" y2="282" stroke="#21262d" stroke-width="1"/>
{speech(p)}
<ellipse class="shadow" cx="185" cy="276" rx="72" ry="12" fill="#000" opacity="0.35"/>
{pet_art(p, body, dark, light)}
{panel(p, body)}
</svg>
"""


# --------------------------------------------------------------------------- #
# README + CLI
# --------------------------------------------------------------------------- #


def readme_block(p: dict, svg_path: str) -> str:
    rel = os.path.basename(svg_path)
    version = p["updated"].replace("-", "")
    alt = escape(f'{p["pet_name"]}, a GitHub pet')
    summary = (
        f'<b>{escape(p["pet_name"])}</b> · Level {p["level"]} {escape(p["stage_name"])} · '
        f'{p["streak"]}-day streak · {p["commits_7"]} {p["week_summary"]} · mood: {escape(p["mood"])}'
    )
    return (
        f"{START_MARK}\n"
        '<p align="center">\n'
        f'  <img src="./{rel}?v={version}" alt="{alt}" width="900" />\n'
        "</p>\n\n"
        f'<p align="center"><sub>{summary}</sub></p>\n'
        f"{END_MARK}"
    )


def update_readme(path: str, svg_path: str, p: dict) -> None:
    block = readme_block(p, svg_path)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = f'# {p["pet_name"]}\n'

    if START_MARK in text and END_MARK in text:
        before = text.split(START_MARK, 1)[0]
        after = text.split(END_MARK, 1)[1]
        updated = before + block + after
    else:
        updated = text.rstrip() + "\n\n" + block + "\n"

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(updated)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Render an animated GitHub pet from your contribution stats."
    )
    parser.add_argument("--login", default=os.environ.get("PET_USERNAME") or os.environ.get("GITHUB_REPOSITORY_OWNER"))
    parser.add_argument("--token", default=os.environ.get("PET_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--name", default=os.environ.get("PET_NAME", "Commitling"))
    parser.add_argument("--out", default="pet.svg", help="where to write the SVG")
    parser.add_argument("--readme", default=None, help="README to update between PET markers")
    parser.add_argument("--demo", action="store_true", help="use fake stats, no token needed")
    parser.add_argument(
        "--public-calendar",
        action="store_true",
        help="read the public contribution calendar instead of GraphQL (last year only, no token)",
    )
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    pet_name = (args.name or "Commitling").strip()[:22] or "Commitling"

    if args.demo:
        raw = demo_raw(now)
    elif args.public_calendar:
        if not args.login:
            parser.error("no username: pass --login or set PET_USERNAME")
        raw = fetch_public_calendar(args.login, now)
    else:
        if not args.login:
            parser.error("no username: pass --login or set PET_USERNAME")
        if not args.token:
            parser.error("no token: pass --token or set PET_TOKEN (a PAT with read:user)")
        raw = fetch_raw(args.token, args.login, now)

    pet = build_pet(raw, now, pet_name)
    if not args.demo and pet["commits_all"] == 0 and pet["commits_7"] == 0 and not raw["days"]:
        print(
            "warning: GitHub returned no contributions. If that is unexpected, set PET_TOKEN "
            "to a personal access token with the read:user scope.",
            file=sys.stderr,
        )
    svg = build_svg(pet)

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(svg)
    print(f"wrote {args.out}: {pet['pet_name']} is level {pet['level']} ({pet['stage_name']}), mood {pet['mood']}")

    if args.readme:
        update_readme(args.readme, args.out, pet)
        print(f"updated {args.readme}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
