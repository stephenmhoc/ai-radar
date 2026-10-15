"""Deterministic, text-only HTML and RSS rendering."""
from __future__ import annotations

import datetime as dt
import email.utils
import html
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



def render_html(
    settings: Settings,
    items: list[dict[str, Any]],
    *,
    main_content: str | None = None,
    page_title: str | None = None,
    section_label: str | None = "Latest radar",
    secondary_links: tuple[tuple[str, str], ...] = (("Archive", ARCHIVE_PATH), ("Feeds", "/feeds.html")),
) -> str:
    body = main_content or render_episode_list(items)
    label = f'<p class="section-label">{html.escape(section_label)}</p>\n    ' if section_label else ""
    document_title = page_title or settings.title
    return _render_document(
        settings,
        document_title=document_title,
        body=label + body,
        secondary_links=secondary_links,
    )



def render_episode_list(items: list[dict[str, Any]], *, empty: str = "No items yet.") -> str:
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
            f"{sources}"
            f'<p class="summary">{summary}</p>'
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



def render_index_html(
    settings: Settings,
    items: list[dict[str, Any]],
    days: list[dict[str, Any]],
    days_closed_through: str | None,
) -> str:
    """Render today's items (the open calendar day), then the most recent closed days."""
    shown = {item_id for day in days for item_id in day["item_ids"]}
    today_items = [item for item in items if str(item["id"]) not in shown]
    today_note = ""
    since = parse_utc_timestamp(days_closed_through)
    if since is not None:
        edition_close = edition_closes_at(site_day(since, settings), settings)
        local_close = edition_close.astimezone(ZoneInfo(settings.edition_timezone))
        today_note = (
            f'<p class="edition-note">A daily RSS edition goes out at '
            f"{hour_label(local_close.hour)} {local_close:%Z}.</p>"
        )
    sections = [
        '<section class="edition edition-today" aria-labelledby="day-today">'
        '<h2 class="edition-title" id="day-today">Today</h2>'
        f"{today_note}"
        f'{render_episode_list(today_items, empty="Nothing new yet today. Recent days are below.")}'
        "</section>"
    ]
    recent = list(reversed(days))[:RECENT_DAYS]
    if recent:
        sections.append(
            '<section class="recent" aria-labelledby="recent-label">'
            '<p class="section-label" id="recent-label">Recent</p>'
            + "\n".join(render_day_section(day, items) for day in recent)
            + f'<p class="archive-link"><a href="{ARCHIVE_PATH}">All past days&nbsp;→</a></p>'
            + "</section>"
        )
    return render_html(settings, [], main_content="\n".join(sections), section_label=None)



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



def _render_document(
    settings: Settings,
    *,
    document_title: str,
    body: str,
    secondary_links: tuple[tuple[str, str], ...],
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
  <style>
    :root {{
      color-scheme: light;
      --paper: #f4f1e8;
      --ink: #20231f;
      --muted: #6b7068;
      --forest: #1c2b23;
      --sage: #cdd9c4;
      --rule: #d5d1c6;
    }}

    * {{ box-sizing: border-box; }}

    html {{
      background: var(--paper);
      font-size: 16px;
      text-rendering: optimizeLegibility;
    }}

    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}

    a {{
      color: inherit;
      text-decoration-thickness: 1px;
      text-underline-offset: 0.2em;
    }}

    a:hover {{ text-decoration-thickness: 2px; }}

    a:focus-visible {{
      border-radius: 0.15rem;
      outline: 3px solid #9caf88;
      outline-offset: 4px;
    }}

    header {{
      background: var(--forest);
      color: #f6f3e9;
      padding: clamp(3.5rem, 9vw, 7rem) max(1.5rem, calc((100vw - 52rem) / 2));
    }}

    .eyebrow {{
      margin: 0 0 1rem;
      color: var(--sage);
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }}

    h1 {{
      max-width: 12ch;
      margin: 0;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: clamp(3.25rem, 9vw, 6.75rem);
      font-weight: 500;
      letter-spacing: -0.055em;
      line-height: 0.88;
    }}

    .dek {{
      max-width: 38rem;
      margin: 1.75rem 0 0;
      color: #d9ddd5;
      font-size: clamp(1rem, 2vw, 1.2rem);
      line-height: 1.65;
    }}

    .rss-link {{
      display: inline-block;
      border: 1px solid #637267;
      border-radius: 999px;
      padding: 0.65rem 1rem;
      color: #f6f3e9;
      font-size: 0.82rem;
      font-weight: 700;
      letter-spacing: 0.035em;
      text-decoration: none;
    }}

    .rss-link:hover {{
      border-color: var(--sage);
      background: #26392e;
    }}

    .header-links {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 1rem;
      margin-top: 1.75rem;
    }}

    .secondary-link {{
      color: #b8c0b8;
      font-size: 0.78rem;
      font-weight: 650;
      letter-spacing: 0.035em;
      text-decoration-color: #637267;
    }}

    .secondary-link:hover {{ color: #f6f3e9; }}

    main {{
      width: min(52rem, calc(100% - 3rem));
      margin: 0 auto;
      padding: 3.75rem 0 6rem;
    }}

    .section-label {{
      margin: 0 0 1.5rem;
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 750;
      letter-spacing: 0.15em;
      text-transform: uppercase;
    }}

    .episode-list {{
      margin: 0;
      padding: 0;
      list-style: none;
    }}

    .episode {{
      padding: 0 0 2.25rem;
      border-bottom: 1px solid var(--rule);
      margin-bottom: 2.25rem;
    }}

    .episode-content {{ max-width: 46rem; }}

    .episode-meta {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem 0.65rem;
      margin: 0 0 0.6rem;
      color: var(--muted);
      font-size: 0.75rem;
      font-weight: 650;
      letter-spacing: 0.035em;
      text-transform: uppercase;
    }}

    .source-list {{
      display: grid;
      gap: 0.8rem;
      margin: 1rem 0 0;
      padding: 0 0 0 1rem;
      border-left: 3px solid var(--sage);
      list-style: none;
    }}

    .source-name {{
      margin: 0;
      color: #363c35;
      font-size: 0.82rem;
      font-weight: 750;
    }}

    .source-kind {{
      color: #52644a;
      font-size: 0.68rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .source-title {{
      margin: 0.15rem 0 0;
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.45;
    }}

    .source-title-label {{
      margin-right: 0.35rem;
      font-weight: 700;
    }}

    .source-title a {{ color: #465b3c; }}

    h2, h3 {{
      margin: 0;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: clamp(1.3rem, 3vw, 1.62rem);
      font-weight: 600;
      letter-spacing: -0.018em;
      line-height: 1.22;
    }}

    .summary {{
      max-width: 68ch;
      margin: 0.8rem 0 0;
      color: #444941;
      font-size: 0.98rem;
      line-height: 1.72;
    }}

    .episode:last-child {{
      margin-bottom: 0;
      border-bottom: 0;
    }}

    .feed-group + .feed-group {{ margin-top: 3rem; }}

    .edition + .edition,
    .edition-today + .recent {{ margin-top: 3.5rem; }}

    .edition-title {{
      margin: 0 0 1.5rem;
      padding-bottom: 0.75rem;
      border-bottom: 2px solid var(--forest);
      font-size: 1.35rem;
    }}

    .edition-today .edition-title {{ margin-bottom: 0.5rem; }}

    .edition-note {{
      margin: 0 0 1.5rem;
      color: var(--muted);
      font-size: 0.85rem;
    }}

    .archive-link {{
      margin: 2.5rem 0 0;
      font-weight: 650;
    }}

    .archive-months {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem 1.25rem;
      margin: 0 0 3rem;
      font-size: 0.9rem;
      font-weight: 650;
    }}

    .archive-month + .archive-month {{ margin-top: 4rem; }}

    .archive-month .section-label {{ scroll-margin-top: 1rem; }}

    .edition-empty {{
      margin: 0;
      color: var(--muted);
      font-style: italic;
    }}

    .feed-group-title {{
      margin: 0 0 1rem;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.35rem;
      letter-spacing: -0.015em;
    }}

    .feed-list {{
      margin: 0;
      padding: 0;
      list-style: none;
      border-top: 1px solid var(--rule);
    }}

    .feed-item {{
      display: grid;
      grid-template-columns: minmax(10rem, 1fr) auto;
      gap: 1rem;
      align-items: baseline;
      padding: 0.9rem 0;
      border-bottom: 1px solid var(--rule);
    }}

    .feed-name {{
      margin: 0;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.02rem;
      font-weight: 600;
    }}

    .feed-links {{
      margin: 0;
      color: var(--muted);
      font-size: 0.74rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}

    @media (max-width: 36rem) {{
      header {{ padding-block: 3.5rem 4rem; }}
      h1 {{ font-size: clamp(3.4rem, 18vw, 5rem); }}
      main {{
        width: min(100% - 2.25rem, 52rem);
        padding-top: 2.75rem;
      }}
      .episode {{
        margin-bottom: 1.8rem;
        padding-bottom: 1.8rem;
      }}
      .summary {{ font-size: 0.95rem; }}
      .feed-item {{
        display: block;
        padding-block: 1rem;
      }}
      .feed-links {{ margin-top: 0.35rem; }}
    }}
  </style>
</head>
<body>
  <header>
    <p class="eyebrow">Podcasts &amp; newsletters</p>
    <h1>{html.escape(settings.title)}</h1>
    <p class="dek">{html.escape(settings.description)}</p>
    <nav class="header-links" aria-label="AI Radar links">
      <a class="rss-link" href="/feed.xml">Follow via RSS&nbsp; ↗</a>
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
