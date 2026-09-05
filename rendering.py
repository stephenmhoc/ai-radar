"""Deterministic, text-only HTML and RSS rendering."""
from __future__ import annotations

import email.utils
import html
import xml.etree.ElementTree as ET
from typing import Any

from radar_common import (
    APPEARANCE_PRESENTATION, Settings, clean_text, escape_public_text,
    ordered_appearances, parse_timestamp, public_http_url, require_public_http_url,
    source_display_name,
)

GENERATED_FILES = ("index.html", "feeds.html", "feed.xml", "_headers")



def render_html(
    settings: Settings,
    items: list[dict[str, Any]],
    *,
    main_content: str | None = None,
    page_title: str | None = None,
    section_label: str = "Latest radar",
    secondary_label: str = "Feeds",
    secondary_href: str = "/feeds.html",
) -> str:
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
            f'<h2>{html.escape(str(item["title"]))}</h2>'
            f"{sources}"
            f'<p class="summary">{summary}</p>'
            "</article>"
            "</li>"
        )
    episode_rows = "\n".join(rows) or '<li class="episode empty">No items yet.</li>'
    body = main_content or f'<ul class="episode-list">\n{episode_rows}\n</ul>'
    document_title = page_title or settings.title
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

    h2 {{
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
    <p class="eyebrow">Podcasts, videos &amp; newsletters</p>
    <h1>{html.escape(settings.title)}</h1>
    <p class="dek">{html.escape(settings.description)}</p>
    <nav class="header-links" aria-label="AI Radar links">
      <a class="rss-link" href="/feed.xml">Follow via RSS&nbsp; ↗</a>
      <a class="secondary-link" href="{html.escape(secondary_href, quote=True)}">{html.escape(secondary_label)}</a>
    </nav>
  </header>
  <main>
    <p class="section-label">{html.escape(section_label)}</p>
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
        secondary_label="Radar",
        secondary_href="/",
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
    return "<br><br><strong>Sources</strong><br><br>" + "<br><br>".join(rows)



def render_rss(settings: Settings, items: list[dict[str, Any]]) -> str:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = settings.title
    ET.SubElement(channel, "link").text = settings.base_url + "/"
    ET.SubElement(channel, "description").text = settings.description
    ET.SubElement(channel, "language").text = "en-us"
    for item in items:
        node = ET.SubElement(channel, "item")
        appearances = ordered_appearances(item)
        primary_appearance = appearances[0] if appearances else None
        source_name = source_display_name(primary_appearance) if primary_appearance else ""
        item_title = str(item["title"])
        if source_name:
            item_title = f"{item_title} — {source_name}"
        ET.SubElement(node, "title").text = item_title
        links = item.get("links", {})
        primary_link = (
            public_http_url(links.get("podcast"))
            or public_http_url(links.get("youtube"))
            or public_http_url(links.get("newsletter"))
            or settings.base_url + "/"
        )
        ET.SubElement(node, "link").text = primary_link
        guid = ET.SubElement(node, "guid", {"isPermaLink": "false"})
        guid.text = "ai-radar:" + str(item["id"])
        published_at = parse_timestamp(item.get("published_at"))
        if published_at is not None:
            ET.SubElement(node, "pubDate").text = email.utils.format_datetime(published_at)
        if primary_appearance is not None:
            configured_source = next(
                (
                    source
                    for source in settings.sources
                    if source.kind == primary_appearance.get("kind")
                    and source.name == primary_appearance.get("source")
                ),
                None,
            )
            if configured_source is not None:
                source_feed_url = public_http_url(configured_source.feed_url)
                if source_feed_url:
                    source_node = ET.SubElement(node, "source", {"url": source_feed_url})
                    source_node.text = source_name
        description_parts = [html.escape(clean_text(str(item.get("long_summary") or "")))]
        source_html = rss_source_details(item)
        if source_html:
            description_parts.append(source_html)
        ET.SubElement(node, "description").text = "".join(description_parts)
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
