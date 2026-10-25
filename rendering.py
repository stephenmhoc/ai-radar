"""Deterministic, text-only HTML and RSS rendering."""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import re
import xml.etree.ElementTree as ET
from typing import Any
from zoneinfo import ZoneInfo

from radar_common import (
    APPEARANCE_PRESENTATION, Settings, clean_text, escape_public_text,
    edition_closes_at, ordered_appearances, parse_timestamp, parse_utc_timestamp,
    public_http_url, require_public_http_url, site_day, source_display_name,
)

GENERATED_FILES = ("index.html", "archive.html", "feeds.html", "feed.xml", "_headers")
ARCHIVE_PATH = "/archive.html"
RECENT_DAYS = 7
# The homepage roster lists followed people heard from within this many days;
# everyone else collapses into a single "quiet" line.
ROSTER_DAYS = 30
MEDIUM_TAGS = {"podcast": "POD", "youtube": "VID", "newsletter": "TXT"}



def render_html(
    settings: Settings,
    items: list[dict[str, Any]],
    *,
    main_content: str | None = None,
    page_title: str | None = None,
    section_label: str | None = "Latest radar",
    secondary_links: tuple[tuple[str, str], ...] = (("Archive", ARCHIVE_PATH), ("Feeds", "/feeds.html")),
    wide: bool = False,
) -> str:
    body = main_content or render_episode_list(items)
    label = f'<p class="section-label">{html.escape(section_label)}</p>\n    ' if section_label else ""
    document_title = page_title or settings.title
    return _render_document(
        settings,
        document_title=document_title,
        body=label + body,
        secondary_links=secondary_links,
        wide=wide,
    )



def render_episode_list(items: list[dict[str, Any]], *, empty: str = "No items yet.") -> str:
    """Full entries with visible summaries, for the archive, where browser find must reach them."""
    rows: list[str] = []
    for item in items:
        date = date_label(item.get("published_at"))
        summary = escape_public_text(clean_text(str(item.get("short_summary") or "")))
        sources = render_source_details(item)
        rows.append(
            '<li class="episode">'
            '<article class="episode-content">'
            '<p class="episode-meta">'
            f'<time datetime="{html.escape(str(item.get("published_at") or ""))}">{html.escape(date)}</time>'
            "</p>"
            f'<h3>{html.escape(str(item["title"]))}</h3>'
            f'<p class="summary">{summary}</p>'
            f"{sources}"
            "</article>"
            "</li>"
        )
    if not rows:
        return f'<p class="edition-empty">{html.escape(empty)}</p>'
    episode_rows = "\n".join(rows)
    return f'<ul class="episode-list">\n{episode_rows}\n</ul>'



def hour_label(hour: int) -> str:
    return f"{hour % 12 or 12} {'a.m.' if hour < 12 else 'p.m.'}"



def edition_label(day: dt.date) -> str:
    return f"{day:%A}, {day:%B} {day.day}, {day.year}"



def short_day_label(day: dt.date) -> str:
    return f"{day:%a} {day:%b} {day.day}"



def _named(names: tuple[str, ...], text: str) -> bool:
    folded = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(name.casefold())}(?!\w)", folded) for name in names)



def followed_names(settings: Settings, item: dict[str, Any]) -> list[str]:
    """Followed people an item features, by canonical name, for the homepage roster.

    A person counts when a headline or publisher title names them in full (or by an
    alias), or names their surname while the stored editorial reason names them in
    full. The reason alone is not enough: older reasons also mention people who were
    only discussed.
    """
    titles = " ".join(
        [str(item.get("title") or "")]
        + [str(value.get("title") or "") for value in item.get("appearances", []) if isinstance(value, dict)]
    )
    reason = str(item.get("reason") or "")
    return [
        names[0]
        for names in settings.people
        if _named(names, titles) or (_named((names[0].split()[-1],), titles) and _named(names, reason))
    ]



def headline_html(settings: Settings, item: dict[str, Any]) -> str:
    """Escaped headline with the first mention of each followed person in bold."""
    text = html.escape(str(item["title"]))
    for names in settings.people:
        for name in names:
            pattern = re.compile(rf"(?<![\w>]){re.escape(html.escape(name))}(?!\w)", re.IGNORECASE)
            text, count = pattern.subn(lambda match: f"<strong>{match.group(0)}</strong>", text, count=1)
            if count:
                break
    return text



def render_story_rows(
    settings: Settings,
    items: list[dict[str, Any]],
    *,
    new: bool,
    empty: str = "No items yet.",
) -> str:
    """One line per item on the homepage; the summary and sources open in place."""
    rows: list[str] = []
    for item in items:
        appearances = ordered_appearances(item)
        summary = escape_public_text(clean_text(str(item.get("short_summary") or "")))
        tags: list[str] = []
        for value in appearances:
            kind = str(value.get("kind") or "")
            url = public_http_url(value.get("url"))
            if kind not in MEDIUM_TAGS or url is None:
                continue
            label = f"{APPEARANCE_PRESENTATION[kind][0]} · {source_display_name(value)}"
            tags.append(
                f'<a class="medium" href="{html.escape(url, quote=True)}" rel="noopener noreferrer" '
                f'title="{escape_public_text(label)}">{MEDIUM_TAGS[kind]}'
                f'<span class="visually-hidden"> {html.escape(APPEARANCE_PRESENTATION[kind][0])}</span></a>'
            )
        source = escape_public_text(source_display_name(appearances[0])) if appearances else ""
        new_label = '<span class="visually-hidden">New: </span>' if new else ""
        rows.append(
            f'<li class="episode story{" is-new" if new else ""}">'
            "<details><summary>"
            '<span class="mark" aria-hidden="true"></span>'
            f"{new_label}"
            f'<span class="headline">{headline_html(settings, item)}</span>'
            "</summary>"
            f'<div class="story-body"><p class="summary">{summary}</p>{render_source_details(item)}</div>'
            "</details>"
            f'<p class="media"><span class="story-source">{source}</span>{"".join(tags)}</p>'
            "</li>"
        )
    if not rows:
        return f'<p class="day-empty">{html.escape(empty)}</p>'
    return '<ul class="story-list">' + "".join(rows) + "</ul>"



def render_index_day(
    settings: Settings,
    record: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    new: bool,
) -> str:
    day = dt.date.fromisoformat(record["date"])
    members = set(record["item_ids"])
    day_items = [item for item in items if str(item["id"]) in members]
    count = len(day_items)
    anchor = f"day-{record['date']}"
    return (
        f'<section class="day{" day-latest" if new else ""}" aria-labelledby="{anchor}">'
        f'<h2 class="day-label" id="{anchor}"><time datetime="{record["date"]}">{short_day_label(day)}</time>'
        f'<span class="day-count">{count} item{"" if count == 1 else "s"}</span></h2>'
        f"{render_story_rows(settings, day_items, new=new)}"
        "</section>"
    )



def age_label(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"



def render_roster(
    settings: Settings,
    items: list[dict[str, Any]],
    days: list[dict[str, Any]],
    today: dt.date,
    new_date: dt.date | None,
) -> str:
    """Followed people by most recent appearance, measured from the archive's own calendar."""
    day_of = {item_id: dt.date.fromisoformat(day["date"]) for day in days for item_id in day["item_ids"]}
    latest: dict[str, tuple[dt.date, dict[str, Any]]] = {}
    for item in items:
        when = day_of.get(str(item["id"]), today)
        for name in followed_names(settings, item):
            if name not in latest or when > latest[name][0]:
                latest[name] = (when, item)
    order = {names[0]: index for index, names in enumerate(settings.people)}
    heard = sorted(
        (name for name, (when, _) in latest.items() if (today - when).days <= ROSTER_DAYS),
        key=lambda name: (-latest[name][0].toordinal(), order[name]),
    )
    quiet = [names[0] for names in settings.people if names[0] not in heard]
    rows: list[str] = []
    for name in heard:
        when, item = latest[name]
        href = "#day-today" if str(item["id"]) not in day_of else f"{ARCHIVE_PATH}#day-{when.isoformat()}"
        rows.append(
            f'<li class="person{" is-new" if when == new_date else ""}">'
            f'<span class="person-name">{html.escape(name)}</span>'
            f'<span class="person-age">{age_label((today - when).days)}</span>'
            f'<a class="person-latest" href="{href}">{html.escape(str(item["title"]))}</a>'
            "</li>"
        )
    body = '<ul class="roster">' + "".join(rows) + "</ul>" if rows else ""
    if quiet:
        body += (
            f'<p class="quiet"><strong>Quiet for {ROSTER_DAYS}+ days:</strong> '
            + html.escape(", ".join(quiet))
            + "</p>"
        )
    return (
        '<aside class="people" aria-labelledby="people-label">'
        '<h2 class="panel-label" id="people-label">People <span>last heard</span></h2>'
        f"{body}</aside>"
    )



def render_index_html(
    settings: Settings,
    items: list[dict[str, Any]],
    days: list[dict[str, Any]],
    days_closed_through: str | None,
) -> str:
    """Render a one-screen front page: today's items, the most recent closed days, and the roster."""
    shown = {item_id for day in days for item_id in day["item_ids"]}
    today_items = [item for item in items if str(item["id"]) not in shown]
    recent = list(reversed(days))[:RECENT_DAYS]
    since = parse_utc_timestamp(days_closed_through)
    # Ages count from the open site day, which only moves when a day closes, so
    # rebuilding unchanged archive state renders identical output.
    today = site_day(since, settings) if since is not None else None
    latest = recent[0] if recent else None
    new_date: dt.date | None = None
    if today_items and today is not None:
        new_date = today
    elif latest is not None:
        new_date = dt.date.fromisoformat(latest["date"])

    status: list[str] = []
    count = len(today_items)
    if today_items:
        status.append(f'<span class="now">{count} new item{"" if count == 1 else "s"} today</span>')
    elif latest is not None:
        latest_count = len(latest["item_ids"])
        status.append(
            f'<span>Latest: <strong>{short_day_label(dt.date.fromisoformat(latest["date"]))}</strong> '
            f'({latest_count} item{"" if latest_count == 1 else "s"})</span>'
        )
    if today is not None:
        local_close = edition_closes_at(today, settings).astimezone(ZoneInfo(settings.edition_timezone))
        status.append(f"<span>Daily RSS edition at {hour_label(local_close.hour)} {local_close:%Z}</span>")

    sections = [
        '<section class="day day-today" aria-labelledby="day-today">'
        '<h2 class="day-label" id="day-today">Today</h2>'
        f'{render_story_rows(settings, today_items, new=True, empty="Nothing new yet today. Recent days are below.")}'
        "</section>"
    ]
    if recent:
        sections.append(
            '<section class="recent" aria-labelledby="recent-label">'
            '<h2 class="visually-hidden" id="recent-label">Recent</h2>'
            + "".join(
                render_index_day(settings, day, items, new=not today_items and day is latest)
                for day in recent
            )
            + "</section>"
        )
    sections.append(f'<p class="archive-link"><a href="{ARCHIVE_PATH}">All past days&nbsp;→</a></p>')
    roster = render_roster(settings, items, days, today, new_date) if today is not None else ""
    body = (
        f'<p class="status">{"".join(status)}</p>'
        f'<div class="front">{roster}<div class="stories">{"".join(sections)}</div></div>'
    )
    return render_html(settings, [], main_content=body, section_label=None, wide=True)



def render_day_section(record: dict[str, Any], items: list[dict[str, Any]]) -> str:
    day = dt.date.fromisoformat(record["date"])
    members = set(record["item_ids"])
    day_items = [item for item in items if str(item["id"]) in members]
    anchor = f"day-{record['date']}"
    return (
        f'<section class="edition" aria-labelledby="{anchor}">'
        f'<h2 class="edition-title" id="{anchor}">{html.escape(edition_label(day))}</h2>'
        f"{render_episode_list(day_items)}"
        "</section>"
    )



def render_archive_html(
    settings: Settings,
    items: list[dict[str, Any]],
    days: list[dict[str, Any]],
) -> str:
    """Render every closed calendar day, newest first, grouped by month."""
    months: dict[str, list[dict[str, Any]]] = {}
    for day in reversed(days):
        months.setdefault(day["date"][:7], []).append(day)
    month_links: list[str] = []
    month_sections: list[str] = []
    for month, month_days in months.items():
        first_day = dt.date.fromisoformat(month + "-01")
        name = f"{first_day:%B} {first_day.year}"
        month_links.append(f'<a href="#month-{month}">{html.escape(name)}</a>')
        month_sections.append(
            f'<section class="archive-month" aria-labelledby="month-{month}">'
            f'<p class="section-label" id="month-{month}">{html.escape(name)}</p>'
            + "\n".join(render_day_section(day, items) for day in month_days)
            + "</section>"
        )
    intro = (
        '<p class="section-label">Archive</p>'
        '<p class="edition-note">Every past day, newest first. Use your browser&rsquo;s '
        "find to search headlines, sources, and summaries. Today&rsquo;s items are on the "
        '<a href="/">front page</a>.</p>'
    )
    if month_links:
        intro += '<nav class="archive-months" aria-label="Months">' + "".join(month_links) + "</nav>"
    body = intro + ("\n".join(month_sections) or '<p class="edition-empty">No past days yet.</p>')
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"Archive — {settings.title}",
        section_label=None,
        secondary_links=(("Radar", "/"), ("Feeds", "/feeds.html")),
    )



STYLE = """
    :root {
      color-scheme: light;
      --bg: #f4f5f7;
      --surface: #ffffff;
      --ink: #14171c;
      --muted: #5d6672;
      --rule: #dde1e7;
      --accent: #1d5bd6;
      --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      --page: 52rem;
    }

    @media (prefers-color-scheme: dark) {
      :root {
        color-scheme: dark;
        --bg: #0e1115;
        --surface: #151a20;
        --ink: #e6e9ee;
        --muted: #949daa;
        --rule: #262c35;
        --accent: #7ea6ff;
      }
    }

    * { box-sizing: border-box; }

    html {
      background: var(--bg);
      font-size: 16px;
      -webkit-text-size-adjust: 100%;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      line-height: 1.45;
    }

    body.wide { --page: 76rem; }

    a {
      color: inherit;
      text-decoration-thickness: 1px;
      text-underline-offset: 0.18em;
    }

    a:focus-visible,
    summary:focus-visible {
      border-radius: 2px;
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }

    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }

    .bar,
    main {
      padding-left: max(1rem, calc((100% - var(--page)) / 2));
      padding-right: max(1rem, calc((100% - var(--page)) / 2));
    }

    .bar {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.25rem 1rem;
      padding-top: 0.9rem;
      padding-bottom: 0.8rem;
      border-bottom: 1px solid var(--rule);
      background: var(--surface);
    }

    h1 {
      margin: 0;
      font-size: 1.1rem;
      font-weight: 780;
      letter-spacing: -0.01em;
    }

    h1 a { text-decoration: none; }

    .dek {
      margin: 0;
      color: var(--muted);
      font-size: 0.82rem;
    }

    .header-links {
      display: flex;
      gap: 1rem;
      margin-left: auto;
      font-size: 0.82rem;
    }

    .rss-link {
      color: var(--accent);
      font-weight: 700;
    }

    .secondary-link { color: var(--muted); }

    .secondary-link:hover { color: var(--ink); }

    main {
      padding-top: 0.25rem;
      padding-bottom: 4rem;
    }

    .status {
      display: flex;
      flex-wrap: wrap;
      gap: 0.2rem 1.25rem;
      margin: 0 0 0.25rem;
      padding: 0.55rem 0;
      color: var(--muted);
      font-size: 0.8rem;
    }

    .status strong { color: var(--ink); }

    .status .now {
      color: var(--accent);
      font-weight: 650;
    }

    .front {
      display: grid;
      gap: 1rem;
      font-size: 0.875rem;
    }

    .day {
      display: grid;
      grid-template-columns: 6.5rem minmax(0, 1fr);
      gap: 0 0.75rem;
      padding: 0.4rem 0;
      border-top: 1px solid var(--rule);
    }

    .day-label {
      margin: 0;
      padding-top: 0.15rem;
      color: var(--muted);
      font: 600 0.75rem/1.4 var(--mono);
    }

    .day-today .day-label,
    .day-latest .day-label { color: var(--accent); }

    .day-count {
      display: block;
      font-weight: 400;
    }

    .day-empty {
      margin: 0;
      padding: 0.1rem 0;
      color: var(--muted);
    }

    .story-list {
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .story {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 0.75rem;
      align-items: start;
    }

    .story summary {
      display: flex;
      gap: 0.45rem;
      align-items: baseline;
      padding: 0.12rem 0;
      cursor: pointer;
      list-style: none;
    }

    .story summary::-webkit-details-marker { display: none; }

    .story summary:hover .headline {
      text-decoration: underline;
      text-decoration-color: var(--muted);
    }

    .mark {
      flex: none;
      width: 0.4rem;
      height: 0.4rem;
      border-radius: 50%;
      transform: translateY(-0.1rem);
    }

    .is-new .mark { background: var(--accent); }

    .headline {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    details[open] .headline { white-space: normal; }

    .story-body {
      max-width: 46rem;
      padding: 0.1rem 0 0.75rem 0.85rem;
    }

    .media {
      display: flex;
      align-items: center;
      gap: 0.3rem;
      margin: 0;
      padding-top: 0.2rem;
    }

    .story-source {
      max-width: 12rem;
      margin-right: 0.2rem;
      overflow: hidden;
      color: var(--muted);
      font-size: 0.78rem;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .medium {
      padding: 0.2rem 0.28rem;
      border: 1px solid var(--rule);
      border-radius: 3px;
      background: var(--surface);
      color: var(--muted);
      font: 600 0.62rem/1 var(--mono);
      letter-spacing: 0.04em;
      text-decoration: none;
    }

    .medium:hover {
      border-color: var(--accent);
      color: var(--accent);
    }

    .archive-link {
      margin: 0;
      padding-top: 0.75rem;
      border-top: 1px solid var(--rule);
      font-weight: 650;
    }

    .archive-link a,
    .edition-note a,
    .source-title a { color: var(--accent); }

    .panel-label,
    .section-label {
      margin: 0 0 0.4rem;
      color: var(--muted);
      font-size: 0.7rem;
      font-weight: 750;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .panel-label span {
      margin-left: 0.4rem;
      font-weight: 500;
      letter-spacing: 0;
      text-transform: none;
    }

    .roster {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr));
      gap: 0 1rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .person {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 0 0.5rem;
      padding: 0.3rem 0;
      border-top: 1px solid var(--rule);
    }

    .person-name {
      overflow: hidden;
      font-weight: 650;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .person-age {
      padding-top: 0.1rem;
      color: var(--muted);
      font: 0.72rem var(--mono);
    }

    .is-new .person-age {
      color: var(--accent);
      font-weight: 700;
    }

    .person-latest {
      display: none;
      grid-column: 1 / -1;
      overflow: hidden;
      color: var(--muted);
      font-size: 0.78rem;
      text-decoration: none;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .person-latest:hover { text-decoration: underline; }

    .quiet {
      margin: 0;
      padding-top: 0.4rem;
      border-top: 1px solid var(--rule);
      color: var(--muted);
      font-size: 0.76rem;
    }

    .quiet strong { color: var(--ink); }

    @media (min-width: 64rem) {
      .front {
        grid-template-columns: minmax(0, 1fr) 16rem;
        gap: 2.5rem;
        align-items: start;
      }
      .stories { grid-row: 1; grid-column: 1; }
      .people { grid-row: 1; grid-column: 2; }
      .roster { display: block; }
      .person-latest { display: block; }
    }

    .summary {
      max-width: 68ch;
      margin: 0;
      line-height: 1.55;
    }

    .source-list {
      display: grid;
      gap: 0.15rem;
      margin: 0.4rem 0 0;
      padding: 0;
      color: var(--muted);
      font-size: 0.8rem;
      list-style: none;
    }

    .source-item {
      display: flex;
      flex-wrap: wrap;
      gap: 0 0.4rem;
    }

    .source-name,
    .source-title { margin: 0; }

    .source-name {
      color: var(--ink);
      font-weight: 650;
    }

    .source-kind {
      color: var(--muted);
      font: 600 0.66rem var(--mono);
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }

    .source-title-label {
      margin-right: 0.3rem;
      font-weight: 650;
    }

    .episode-list {
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .episode-list .episode {
      padding: 0.9rem 0;
      border-bottom: 1px solid var(--rule);
    }

    .episode-list .episode:last-child { border-bottom: 0; }

    .episode-list .summary { font-size: 0.92rem; }

    .episode-meta {
      margin: 0 0 0.2rem;
      color: var(--muted);
      font: 0.72rem var(--mono);
    }

    h2, h3 { text-wrap: balance; }

    h3 {
      margin: 0 0 0.3rem;
      font-size: 1.02rem;
      font-weight: 650;
      line-height: 1.35;
    }

    .edition-title {
      margin: 2rem 0 0;
      padding-bottom: 0.35rem;
      border-bottom: 2px solid var(--ink);
      font-size: 0.95rem;
      font-weight: 700;
    }

    .edition-note {
      margin: 0 0 1rem;
      color: var(--muted);
      font-size: 0.85rem;
    }

    .edition-empty {
      margin: 0;
      color: var(--muted);
    }

    main > .section-label:first-child { margin-top: 1.5rem; }

    .archive-months {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem 1rem;
      margin: 0 0 1rem;
      font-size: 0.85rem;
      font-weight: 650;
    }

    .archive-month { margin-top: 2.5rem; }

    .archive-month .section-label { scroll-margin-top: 1rem; }

    .feed-group + .feed-group { margin-top: 2rem; }

    .feed-group-title {
      margin: 0 0 0.5rem;
      font-size: 1rem;
      font-weight: 700;
    }

    .feed-list {
      margin: 0;
      padding: 0;
      border-top: 1px solid var(--rule);
      list-style: none;
    }

    .feed-item {
      display: grid;
      grid-template-columns: minmax(10rem, 1fr) auto;
      gap: 1rem;
      align-items: baseline;
      padding: 0.6rem 0;
      border-bottom: 1px solid var(--rule);
    }

    .feed-name {
      margin: 0;
      font-weight: 600;
    }

    .feed-links {
      margin: 0;
      color: var(--muted);
      font-size: 0.74rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    @media (max-width: 40rem) {
      .header-links { margin-left: 0; }
      .day { grid-template-columns: minmax(0, 1fr); }
      .day-label { padding: 0 0 0.15rem; }
      .day-count {
        display: inline;
        margin-left: 0.4rem;
      }
      .story-source { display: none; }
      .media { padding-top: 0.25rem; }
      .headline {
        display: -webkit-box;
        white-space: normal;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }
      details[open] .headline { display: block; }
      .feed-item {
        display: block;
        padding-block: 0.8rem;
      }
      .feed-links { margin-top: 0.3rem; }
    }
"""



def _render_document(
    settings: Settings,
    *,
    document_title: str,
    body: str,
    secondary_links: tuple[tuple[str, str], ...],
    wide: bool = False,
) -> str:
    secondary = "\n      ".join(
        f'<a class="secondary-link" href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
        for label, href in secondary_links
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(document_title)}</title>
  <meta name="description" content="{html.escape(settings.description, quote=True)}">
  <link rel="alternate" type="application/rss+xml" title="{html.escape(settings.title, quote=True)}" href="/feed.xml">
  <style>{STYLE}  </style>
</head>
<body{' class="wide"' if wide else ""}>
  <header class="bar">
    <h1><a href="/">{html.escape(settings.title)}</a></h1>
    <p class="dek">{html.escape(settings.description)}</p>
    <nav class="header-links" aria-label="AI Radar links">
      <a class="rss-link" href="/feed.xml">RSS</a>
      {secondary}
    </nav>
  </header>
  <main>
    {body}
  </main>
</body>
</html>
"""



def render_feeds_html(settings: Settings) -> str:
    groups: list[str] = []
    for kind, heading in (
        ("podcast", "Podcast feeds"),
        ("youtube", "YouTube feeds"),
        ("newsletter", "Newsletter feeds"),
    ):
        rows: list[str] = []
        for source in sorted(
            (source for source in settings.sources if source.kind == kind),
            key=lambda source: source.name.casefold(),
        ):
            feed_url = require_public_http_url(source.feed_url, label=f"feed URL for {source.name}")
            homepage_url = require_public_http_url(
                source.homepage_url, label=f"homepage URL for {source.name}"
            )
            rows.append(
                '<li class="feed-item">'
                f'<p class="feed-name">{html.escape(source.name)}</p>'
                '<p class="feed-links">'
                f'<a href="{html.escape(feed_url, quote=True)}" rel="noopener noreferrer">Feed</a>'
                ' <span aria-hidden="true">·</span> '
                f'<a href="{html.escape(homepage_url, quote=True)}" rel="noopener noreferrer">Source</a>'
                "</p>"
                "</li>"
            )
        if not rows:
            continue
        groups.append(
            '<section class="feed-group">'
            f'<h2 class="feed-group-title">{html.escape(heading)}</h2>'
            f'<ul class="feed-list">{"".join(rows)}</ul>'
            "</section>"
        )
    return render_html(
        settings,
        [],
        main_content="".join(groups),
        page_title=f"Feeds — {settings.title}",
        section_label="Monitored sources",
        secondary_links=(("Radar", "/"), ("Archive", ARCHIVE_PATH)),
    )



def render_source_details(item: dict[str, Any]) -> str:
    rows: list[str] = []
    for appearance_value in ordered_appearances(item):
        kind = str(appearance_value.get("kind") or "")
        labels = APPEARANCE_PRESENTATION.get(kind)
        if labels is None:
            continue
        kind_label, title_label = labels
        source_name = source_display_name(appearance_value)
        source_title = clean_text(str(appearance_value.get("title") or ""))
        url = public_http_url(appearance_value.get("url"))
        if not source_name or not source_title or url is None:
            continue
        rows.append(
            '<li class="source-item">'
            '<p class="source-name">'
            f'<span class="source-kind">{html.escape(kind_label)}</span>'
            ' <span aria-hidden="true">·</span> '
            f"{escape_public_text(source_name)}"
            "</p>"
            '<p class="source-title">'
            f'<span class="source-title-label">{html.escape(title_label)}:</span>'
            f'<a href="{html.escape(url, quote=True)}" rel="noopener noreferrer">'
            f"{escape_public_text(source_title)}&nbsp;↗</a>"
            "</p>"
            "</li>"
        )
    if not rows:
        return ""
    return '<ul class="source-list" aria-label="Content sources">' + "".join(rows) + "</ul>"



def rss_source_details(item: dict[str, Any]) -> str:
    rows: list[str] = []
    for appearance_value in ordered_appearances(item):
        kind = str(appearance_value.get("kind") or "")
        labels = APPEARANCE_PRESENTATION.get(kind)
        if labels is None:
            continue
        kind_label, title_label = labels
        source_name = source_display_name(appearance_value)
        source_title = clean_text(str(appearance_value.get("title") or ""))
        url = public_http_url(appearance_value.get("url"))
        if not source_name or not source_title or url is None:
            continue
        rows.append(
            f"<strong>{html.escape(kind_label)} · {html.escape(source_name)}</strong><br>"
            f"<strong>{html.escape(title_label)}:</strong> "
            f'<a href="{html.escape(url, quote=True)}">{html.escape(source_title)}</a>'
        )
    if not rows:
        return ""
    return "<p><strong>Sources</strong></p>" + "".join(f"<p>{row}</p>" for row in rows)



def primary_link(settings: Settings, item: dict[str, Any]) -> str:
    links = item.get("links", {})
    return (
        public_http_url(links.get("podcast"))
        or public_http_url(links.get("youtube"))
        or public_http_url(links.get("newsletter"))
        or settings.base_url + "/"
    )



def rss_item_section(settings: Settings, item: dict[str, Any]) -> str:
    """One item inside a daily RSS edition: titled with its primary source, long summary, sources."""
    appearances = ordered_appearances(item)
    source_name = source_display_name(appearances[0]) if appearances else ""
    title = str(item["title"])
    if source_name:
        title = f"{title} — {source_name}"
    link = primary_link(settings, item)
    return (
        f'<h3><a href="{html.escape(link, quote=True)}">{html.escape(title)}</a></h3>'
        f'<p>{html.escape(clean_text(str(item.get("long_summary") or "")))}</p>'
        f"{rss_source_details(item)}"
    )



def render_rss(
    settings: Settings,
    items: list[dict[str, Any]],
    editions: list[dict[str, Any]],
) -> str:
    """Render one RSS entry per closed daily edition, newest first."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = settings.title
    ET.SubElement(channel, "link").text = settings.base_url + "/"
    ET.SubElement(channel, "description").text = settings.description
    ET.SubElement(channel, "language").text = "en-us"
    for edition in reversed(editions):
        members = set(edition["item_ids"])
        edition_items = [item for item in items if str(item["id"]) in members]
        day = dt.date.fromisoformat(edition["date"])
        count = len(edition_items)
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = (
            f"{settings.title} — {edition_label(day)} ({count} item{'' if count == 1 else 's'})"
        )
        ET.SubElement(node, "link").text = f"{settings.base_url}{ARCHIVE_PATH}#day-{edition['date']}"
        guid = ET.SubElement(node, "guid", {"isPermaLink": "false"})
        guid.text = "ai-radar:edition:" + str(edition["date"])
        closed_at = parse_timestamp(edition["closed_at"])
        if closed_at is not None:
            ET.SubElement(node, "pubDate").text = email.utils.format_datetime(closed_at)
        ET.SubElement(node, "description").text = "<hr>".join(
            rss_item_section(settings, item) for item in edition_items
        )
    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"



def render_headers() -> str:
    return """/*
  Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY

/feed.xml
  Content-Type: application/rss+xml; charset=utf-8
"""



def date_label(value: str | None) -> str:
    if not value:
        return "Unknown date"
    parsed = parse_timestamp(value)
    if parsed is None:
        return value[:10]
    return f"{parsed:%b} {parsed.day}, {parsed.year}"
