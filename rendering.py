"""Deterministic, text-only HTML and RSS rendering."""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any
from zoneinfo import ZoneInfo

from radar_common import (
    APPEARANCE_PRESENTATION, Settings, clean_text, escape_public_text,
    edition_closes_at, ordered_appearances, parse_timestamp, parse_utc_timestamp,
    public_http_url, require_public_http_url, site_day, source_display_name,
)

GENERATED_FILES = (
    "index.html", "archive.html", "feeds.html", "people.html", "about.html", "404.html",
    "feed.xml", "sitemap.xml", "robots.txt", "llms.txt", "_headers",
)
# One generated page per day, item, and qualifying person. Files here that the
# archive no longer produces are pruned, so a retraction removes its page.
GENERATED_DIRS = ("days", "items", "people")
ARCHIVE_PATH = "/archive"
FEEDS_PATH = "/feeds"
PEOPLE_PATH = "/people"
ABOUT_PATH = "/about"
NAV_LINKS = (("People", PEOPLE_PATH), ("Archive", ARCHIVE_PATH), ("Feeds", FEEDS_PATH), ("About", ABOUT_PATH))
REPOSITORY_URL = "https://github.com/stephenmhoc/ai-radar"
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
# A person gets a page once this many items feature them; fewer would be thin.
PERSON_PAGE_MIN_ITEMS = 3
META_DESCRIPTION_CHARS = 158
# Schema.org types for the original work a summary is based on. YouTube stays a
# CreativeWork: VideoObject is held to thumbnail requirements this site cannot meet.
SOURCE_SCHEMA_TYPES = {"podcast": "PodcastEpisode", "youtube": "CreativeWork", "newsletter": "Article"}
RECENT_DAYS = 7
# The homepage roster lists followed people heard from within this many days;
# everyone else collapses into a single "quiet" line.
ROSTER_DAYS = 30
MEDIUM_TAGS = {"podcast": "POD", "youtube": "VID", "newsletter": "TXT"}



def slugify(text: str, *, limit: int = 70) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:limit].rsplit("-", 1)[0] if len(slug) > limit else slug



def item_path(item: dict[str, Any]) -> str:
    """A readable path; the ID prefix keeps it unique across repeated headlines."""
    return f"/items/{slugify(str(item['title'])) or 'item'}-{slugify(str(item['id']))[:8]}"



def person_path(name: str) -> str:
    return f"{PEOPLE_PATH}/{slugify(name)}"



def day_path(date: str) -> str:
    return f"/days/{date}"



def meta_description(text: str) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= META_DESCRIPTION_CHARS:
        return cleaned
    return cleaned[:META_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"



def publisher_schema(settings: Settings) -> dict[str, Any]:
    return {"@type": "Organization", "name": settings.title, "url": settings.base_url + "/"}



def breadcrumb_schema(settings: Settings, trail: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": position, "name": name, "item": settings.base_url + path}
            for position, (name, path) in enumerate(trail, start=1)
        ],
    }



def item_list_schema(settings: Settings, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "@type": "ItemList",
        "numberOfItems": len(items),
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": position,
                "name": str(item["title"]),
                "url": settings.base_url + item_path(item),
            }
            for position, item in enumerate(items, start=1)
        ],
    }



def json_ld_block(value: dict[str, Any]) -> str:
    """A non-executing data block; escaping `<` keeps feed text from closing the tag."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f'<script type="application/ld+json">{payload.replace("<", chr(92) + "u003c")}</script>'



def render_html(
    settings: Settings,
    items: list[dict[str, Any]],
    *,
    main_content: str | None = None,
    page_title: str | None = None,
    section_label: str | None = "Latest radar",
    wide: bool = False,
    path: str | None = None,
    description: str | None = None,
    og_type: str = "website",
    structured: tuple[dict[str, Any], ...] = (),
    noindex: bool = False,
    own_heading: bool = False,
) -> str:
    body = main_content or render_episode_list(items)
    label = f'<p class="section-label">{html.escape(section_label)}</p>\n    ' if section_label else ""
    document_title = page_title or settings.title
    return _render_document(
        settings,
        document_title=document_title,
        body=label + body,
        wide=wide,
        path=path,
        description=description or settings.description,
        og_type=og_type,
        structured=structured,
        noindex=noindex,
        own_heading=own_heading,
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
            f'<h3><a href="{item_path(item)}">{html.escape(str(item["title"]))}</a></h3>'
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
    """One row per item on the homepage; the summary and sources open in place."""
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
                f'<a class="medium" href="{html.escape(url, quote=True)}" rel="noopener" '
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
            f'<div class="story-body"><p class="summary">{summary}</p>{render_source_details(item)}'
            f'<p class="item-link"><a href="{item_path(item)}">Full summary&nbsp;→</a></p></div>'
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
) -> str:
    day = dt.date.fromisoformat(record["date"])
    members = set(record["item_ids"])
    day_items = [item for item in items if str(item["id"]) in members]
    count = len(day_items)
    anchor = f"day-{record['date']}"
    return (
        f'<section class="day" aria-labelledby="{anchor}">'
        f'<h2 class="day-label" id="{anchor}"><time datetime="{record["date"]}">{short_day_label(day)}</time>'
        f'<span class="day-count">{count} item{"" if count == 1 else "s"}</span></h2>'
        f"{render_story_rows(settings, day_items, new=False)}"
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
) -> str:
    """Followed people by most recent appearance, measured from the archive's own calendar."""
    day_of = {item_id: dt.date.fromisoformat(day["date"]) for day in days for item_id in day["item_ids"]}
    latest: dict[str, tuple[dt.date, dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for item in items:
        when = day_of.get(str(item["id"]), today)
        for name in followed_names(settings, item):
            counts[name] = counts.get(name, 0) + 1
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
        label = html.escape(name)
        if counts[name] >= PERSON_PAGE_MIN_ITEMS:
            label = f'<a href="{person_path(name)}">{label}</a>'
        rows.append(
            f'<li class="person{" is-new" if when == today else ""}">'
            f'<span class="person-name">{label}</span>'
            f'<span class="person-age">{age_label((today - when).days)}</span>'
            f'<a class="person-latest" href="{item_path(item)}">{html.escape(str(item["title"]))}</a>'
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
    """Render the front page: today's items, the most recent closed days, and the roster."""
    shown = {item_id for day in days for item_id in day["item_ids"]}
    today_items = [item for item in items if str(item["id"]) not in shown]
    recent = list(reversed(days))[:RECENT_DAYS]
    since = parse_utc_timestamp(days_closed_through)
    # Ages count from the open site day, which only moves when a day closes, so
    # rebuilding unchanged archive state renders identical output.
    today = site_day(since, settings) if since is not None else None
    latest = recent[0] if recent else None

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
            + "".join(render_index_day(settings, day, items) for day in recent)
            + "</section>"
        )
    sections.append(f'<p class="archive-link"><a href="{ARCHIVE_PATH}">All past days&nbsp;→</a></p>')
    roster = render_roster(settings, items, days, today) if today is not None else ""
    body = (
        f'<p class="status">{"".join(status)}</p>'
        f'<div class="front"><div class="stories">{"".join(sections)}</div>{roster}</div>'
    )
    named = [names[0] for names in settings.people[:3]]
    leaders = f", including {', '.join(named[:-1])}, and {named[-1]}" if len(named) == 3 else ""
    description = f"Daily summaries of AI podcast interviews, talks, and writing from the people building AI{leaders}."
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"{settings.title} — AI interviews and writing, summarized daily",
        section_label=None,
        wide=True,
        path="/",
        description=description,
        structured=(
            {
                "@context": "https://schema.org",
                "@type": "WebSite",
                "name": settings.title,
                "url": settings.base_url + "/",
                "description": description,
                "publisher": publisher_schema(settings),
            },
        ),
    )



def render_archive_day(settings: Settings, record: dict[str, Any], items: list[dict[str, Any]]) -> str:
    day = dt.date.fromisoformat(record["date"])
    members = set(record["item_ids"])
    rows: list[str] = []
    for item in items:
        if str(item["id"]) not in members:
            continue
        appearances = ordered_appearances(item)
        source = escape_public_text(source_display_name(appearances[0])) if appearances else ""
        rows.append(
            f'<li><a href="{item_path(item)}">{html.escape(str(item["title"]))}</a>'
            f'<span class="archive-source">{source}</span></li>'
        )
    anchor = f"day-{record['date']}"
    return (
        f'<section class="edition" aria-labelledby="{anchor}">'
        f'<h3 class="edition-title" id="{anchor}"><a href="{day_path(record["date"])}">'
        f"{html.escape(edition_label(day))}</a></h3>"
        f'<ul class="archive-list">{"".join(rows)}</ul>'
        "</section>"
    )



def render_archive_html(
    settings: Settings,
    items: list[dict[str, Any]],
    days: list[dict[str, Any]],
) -> str:
    """Link every closed day and its headlines, newest first, grouped by month."""
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
            f'<h2 class="section-label" id="month-{month}">{html.escape(name)}</h2>'
            + "\n".join(render_archive_day(settings, day, items) for day in month_days)
            + "</section>"
        )
    intro = (
        '<h1 class="page-title">Archive</h1>'
        '<p class="edition-note">Every past day, newest first. Use your browser&rsquo;s '
        "find to search headlines and sources, or open a day or item for its summaries. "
        'Today&rsquo;s items are on the <a href="/">front page</a>.</p>'
    )
    if month_links:
        intro += '<nav class="archive-months" aria-label="Months">' + "".join(month_links) + "</nav>"
    body = intro + ("\n".join(month_sections) or '<p class="edition-empty">No past days yet.</p>')
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"Archive of AI interviews and writing — {settings.title}",
        section_label=None,
        path=ARCHIVE_PATH,
        description=(
            f"Every day of {settings.title}: {sum(len(day['item_ids']) for day in days)} summarized "
            "AI interviews, talks, and essays, newest first."
        ),
        own_heading=True,
    )



def crumbs_html(trail: tuple[tuple[str, str], ...]) -> str:
    links = ' <span aria-hidden="true">/</span> '.join(
        f'<a href="{html.escape(path, quote=True)}">{html.escape(name)}</a>' for name, path in trail
    )
    return f'<nav class="crumbs" aria-label="Breadcrumb">{links}</nav>'



def render_day_page(
    settings: Settings,
    record: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    previous: str | None,
    following: str | None,
) -> str:
    day = dt.date.fromisoformat(record["date"])
    label = edition_label(day)
    members = set(record["item_ids"])
    day_items = [item for item in items if str(item["id"]) in members]
    count = len(day_items)
    path = day_path(record["date"])
    pager: list[str] = []
    if previous:
        pager.append(f'<a href="{day_path(previous)}">←&nbsp;{html.escape(edition_label(dt.date.fromisoformat(previous)))}</a>')
    if following:
        pager.append(f'<a href="{day_path(following)}">{html.escape(edition_label(dt.date.fromisoformat(following)))}&nbsp;→</a>')
    trail = (("Radar", "/"), ("Archive", ARCHIVE_PATH))
    body = (
        crumbs_html(trail)
        + f'<h1 class="page-title">AI interviews and writing from {html.escape(label)}</h1>'
        + f'<p class="edition-note">{count} item{"" if count == 1 else "s"} published that day.</p>'
        + render_episode_list(day_items)
        + (f'<nav class="pager" aria-label="Adjacent days">{"".join(pager)}</nav>' if pager else "")
    )
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"AI interviews and writing, {label} — {settings.title}",
        section_label=None,
        path=path,
        description=meta_description(f"{label}: " + "; ".join(str(item["title"]) for item in day_items)),
        structured=(
            {
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "name": f"AI interviews and writing from {label}",
                "url": settings.base_url + path,
                "datePublished": record["date"],
                "publisher": publisher_schema(settings),
                "mainEntity": item_list_schema(settings, day_items),
            },
            breadcrumb_schema(settings, (*trail, (label, path))),
        ),
        own_heading=True,
    )



def render_item_page(
    settings: Settings,
    item: dict[str, Any],
    *,
    day: str | None,
    people: list[str],
    person_pages: set[str],
) -> str:
    """The one place the site shows a long summary, with every original appearance."""
    title = str(item["title"])
    path = item_path(item)
    long_summary = clean_text(str(item.get("long_summary") or ""))
    short_summary = clean_text(str(item.get("short_summary") or ""))
    appearances = ordered_appearances(item)
    source_names = list(dict.fromkeys(source_display_name(value) for value in appearances))
    trail: tuple[tuple[str, str], ...] = (("Radar", "/"), ("Archive", ARCHIVE_PATH))
    if day:
        trail = (*trail, (edition_label(dt.date.fromisoformat(day)), day_path(day)))
    published = str(item.get("published_at") or "")
    meta = [f'<time datetime="{html.escape(published, quote=True)}">{html.escape(date_label(published))}</time>']
    meta.extend(escape_public_text(name) for name in source_names)
    person_links = [
        f'<a href="{person_path(name)}">More with {html.escape(name)}&nbsp;→</a>'
        for name in people
        if name in person_pages
    ]
    based_on = []
    for value in appearances:
        url = public_http_url(value.get("url"))
        schema_type = SOURCE_SCHEMA_TYPES.get(str(value.get("kind") or ""))
        if url and schema_type:
            based_on.append(
                {
                    "@type": schema_type,
                    "name": clean_text(str(value.get("title") or "")),
                    "url": url,
                    "isPartOf": {"@type": "CreativeWorkSeries", "name": source_display_name(value)},
                }
            )
    article: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title[:110],
        "description": short_summary,
        "url": settings.base_url + path,
        "mainEntityOfPage": settings.base_url + path,
        "datePublished": published,
        "author": publisher_schema(settings),
        "publisher": publisher_schema(settings),
        "isBasedOn": based_on,
    }
    if people:
        article["about"] = [{"@type": "Person", "name": name} for name in people]
    body = (
        crumbs_html(trail)
        + '<article class="item">'
        + f'<h1 class="page-title">{html.escape(title)}</h1>'
        + f'<p class="item-meta">{" · ".join(meta)}</p>'
        + f'<p class="summary lede">{escape_public_text(long_summary)}</p>'
        + '<h2 class="section-label">Original sources</h2>'
        + render_source_details(item)
        + f'<p class="edition-note item-note">{html.escape(settings.title)} wrote this summary from the '
        + f'publisher&rsquo;s notes with AI, not from a transcript. <a href="{ABOUT_PATH}">How it works</a>.</p>'
        + (f'<p class="item-link">{" ".join(person_links)}</p>' if person_links else "")
        + "</article>"
    )
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"{title} — {settings.title}",
        section_label=None,
        path=path,
        description=meta_description(short_summary),
        og_type="article",
        structured=(article, breadcrumb_schema(settings, (*trail, (title, path)))),
        own_heading=True,
    )



def person_groups(settings: Settings, items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Each followed person's items, in the order given, for all configured people."""
    groups: dict[str, list[dict[str, Any]]] = {names[0]: [] for names in settings.people}
    for item in items:
        for name in followed_names(settings, item):
            groups[name].append(item)
    return groups



def render_person_page(settings: Settings, name: str, items: list[dict[str, Any]]) -> str:
    role = dict(settings.person_roles).get(name, "")
    path = person_path(name)
    count = len(items)
    heading = f"{name} interviews, talks, and writing"
    newest = date_label(items[0].get("published_at"))
    oldest = date_label(items[-1].get("published_at"))
    intro = (
        f"{name}{f', {role},' if role else ''} is featured in {count} item{'' if count == 1 else 's'} on "
        f"{settings.title}, from {oldest} to {newest}, each summarized with links to the original."
    )
    trail = (("Radar", "/"), ("People", PEOPLE_PATH))
    body = (
        crumbs_html(trail)
        + f'<h1 class="page-title">{html.escape(heading)}</h1>'
        + f'<p class="edition-note">{html.escape(intro)}</p>'
        + render_episode_list(items)
    )
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"{heading} — {settings.title}",
        section_label=None,
        path=path,
        description=meta_description(intro),
        structured=(
            {
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "name": heading,
                "url": settings.base_url + path,
                "about": {"@type": "Person", "name": name, **({"jobTitle": role} if role else {})},
                "publisher": publisher_schema(settings),
                "mainEntity": item_list_schema(settings, items),
            },
            breadcrumb_schema(settings, (*trail, (name, path))),
        ),
        own_heading=True,
    )



def render_people_index(settings: Settings, groups: dict[str, list[dict[str, Any]]]) -> str:
    roles = dict(settings.person_roles)
    rows: list[str] = []
    for name, items in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        label = html.escape(name)
        if len(items) >= PERSON_PAGE_MIN_ITEMS:
            label = f'<a href="{person_path(name)}">{label}</a>'
        count = len(items)
        rows.append(
            f'<li class="feed-item"><p class="feed-name">{label}'
            f'<span class="person-role">{html.escape(roles.get(name, ""))}</span></p>'
            f'<p class="feed-links">{count} item{"" if count == 1 else "s"}</p></li>'
        )
    body = (
        '<h1 class="page-title">People</h1>'
        f'<p class="edition-note">{html.escape(settings.title)} follows these people, and an item '
        "qualifies when one of them is an actual guest, speaker, or author. Mentions do not count.</p>"
        f'<ul class="feed-list">{"".join(rows)}</ul>'
    )
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"AI leaders we follow — {settings.title}",
        section_label=None,
        path=PEOPLE_PATH,
        description=meta_description(
            "Interviews, talks, and writing tracked for AI leaders including "
            + ", ".join(names[0] for names in settings.people[:6])
            + "."
        ),
        own_heading=True,
    )



def render_about_page(settings: Settings) -> str:
    body = (
        f'<h1 class="page-title">About {html.escape(settings.title)}</h1>'
        '<div class="prose">'
        f"<p>{html.escape(settings.title)} tracks what the people shaping AI actually say. It follows a short "
        f'<a href="{PEOPLE_PATH}">list of lab leaders and researchers</a> across '
        f'<a href="{FEEDS_PATH}">{len(settings.sources)} podcast, video, and newsletter sources</a>, and '
        "publishes a summary whenever one of them is a real guest, speaker, or author.</p>"
        "<h2>How items are selected</h2>"
        "<p>Every hour, a worker reads the configured feeds. A model judges each new item against one rule: "
        "a followed person must take part. Mentions, third-party analysis, news roundups, and colleagues "
        "at the same company do not qualify. A local check then confirms the named person appears in the "
        "publisher&rsquo;s own title, byline, or notes before anything is published.</p>"
        "<h2>How summaries are written</h2>"
        "<p>Summaries are written by AI from publisher notes, such as episode descriptions. "
        f"{html.escape(settings.title)} does not use transcripts, so a summary describes what the publisher "
        "says an item covers and is not a verbatim record. Every summary links to the original, which is "
        "the source to rely on. Items with sparse notes wait until richer notes arrive.</p>"
        "<h2>What gets published</h2>"
        f'<p>New items appear on the <a href="/">front page</a> the day they are found. Each day is kept in the '
        f'<a href="{ARCHIVE_PATH}">archive</a>, and a daily <a href="/feed.xml">RSS edition</a> goes out in '
        "the evening. There are no ads or tracking scripts.</p>"
        f'<p>The code and data are <a href="{REPOSITORY_URL}" rel="noopener">public on GitHub</a>.</p>'
        "</div>"
    )
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"About — {settings.title}",
        section_label=None,
        path=ABOUT_PATH,
        description=meta_description(
            f"How {settings.title} selects AI interviews and writing from followed leaders, and how its "
            "summaries are written from publisher notes."
        ),
        own_heading=True,
    )



def render_not_found_page(settings: Settings) -> str:
    body = (
        '<h1 class="page-title">Page not found</h1>'
        '<p class="edition-note">That page does not exist. Try the <a href="/">front page</a>, '
        f'the <a href="{ARCHIVE_PATH}">archive</a>, or the <a href="{PEOPLE_PATH}">people</a> list.</p>'
    )
    return render_html(
        settings,
        [],
        main_content=body,
        page_title=f"Page not found — {settings.title}",
        section_label=None,
        noindex=True,
        own_heading=True,
    )



def render_robots(settings: Settings) -> str:
    return f"User-agent: *\nAllow: /\n\nSitemap: {settings.base_url}/sitemap.xml\n"



def render_sitemap(
    settings: Settings,
    items: list[dict[str, Any]],
    days: list[dict[str, Any]],
    groups: dict[str, list[dict[str, Any]]],
) -> str:
    """Every indexable URL; `lastmod` comes from archive state, never the build clock."""
    def seen(item: dict[str, Any]) -> str:
        return str(item.get("first_seen_at") or "")[:10]

    latest = max((seen(item) for item in items), default="")
    entries: list[tuple[str, str]] = [
        ("/", latest),
        (ARCHIVE_PATH, days[-1]["date"] if days else ""),
        (PEOPLE_PATH, latest),
        (FEEDS_PATH, ""),
        (ABOUT_PATH, ""),
    ]
    entries.extend(
        (person_path(name), max(seen(item) for item in group))
        for name, group in groups.items()
        if len(group) >= PERSON_PAGE_MIN_ITEMS
    )
    entries.extend((day_path(day["date"]), str(day["closed_at"])[:10]) for day in reversed(days))
    entries.extend((item_path(item), seen(item)) for item in items)
    rows = [
        f"  <url><loc>{html.escape(settings.base_url + path)}</loc>"
        + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
        + "</url>"
        for path, lastmod in entries
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(rows) + "\n</urlset>\n"
    )



def render_llms_txt(settings: Settings, groups: dict[str, list[dict[str, Any]]]) -> str:
    base = settings.base_url
    lines = [
        f"# {settings.title}",
        "",
        f"> {settings.description}",
        "",
        "Summaries are written by AI from publisher notes, not transcripts. Cite the linked original.",
        "",
        "## Site",
        "",
        f"- [About and methodology]({base}{ABOUT_PATH})",
        f"- [Followed people]({base}{PEOPLE_PATH})",
        f"- [Archive of every day]({base}{ARCHIVE_PATH})",
        f"- [Monitored sources]({base}{FEEDS_PATH})",
        f"- [Daily RSS feed]({base}/feed.xml)",
        f"- [Sitemap]({base}/sitemap.xml)",
        "",
        "## People",
        "",
    ]
    lines.extend(
        f"- [{name}]({base}{person_path(name)})"
        for name, group in groups.items()
        if len(group) >= PERSON_PAGE_MIN_ITEMS
    )
    return "\n".join(lines) + "\n"



def render_site(
    settings: Settings,
    items: list[dict[str, Any]],
    days: list[dict[str, Any]],
    editions: list[dict[str, Any]],
    days_closed_through: str | None,
) -> dict[str, str]:
    """Every generated file, keyed by its path under the public directory."""
    groups = person_groups(settings, items)
    person_pages = {name for name, group in groups.items() if len(group) >= PERSON_PAGE_MIN_ITEMS}
    rendered = {
        "index.html": render_index_html(settings, items, days, days_closed_through),
        "archive.html": render_archive_html(settings, items, days),
        "feeds.html": render_feeds_html(settings),
        "people.html": render_people_index(settings, groups),
        "about.html": render_about_page(settings),
        "404.html": render_not_found_page(settings),
        "feed.xml": render_rss(settings, items, editions, days),
        "sitemap.xml": render_sitemap(settings, items, days, groups),
        "robots.txt": render_robots(settings),
        "llms.txt": render_llms_txt(settings, groups),
        "_headers": render_headers(),
    }
    for index, record in enumerate(days):
        rendered[f"days/{record['date']}.html"] = render_day_page(
            settings,
            record,
            items,
            previous=days[index - 1]["date"] if index else None,
            following=days[index + 1]["date"] if index + 1 < len(days) else None,
        )
    day_of = {item_id: day["date"] for day in days for item_id in day["item_ids"]}
    for item in items:
        rendered[item_path(item).lstrip("/") + ".html"] = render_item_page(
            settings,
            item,
            day=day_of.get(str(item["id"])),
            people=followed_names(settings, item),
            person_pages=person_pages,
        )
    for name in sorted(person_pages):
        rendered[person_path(name).lstrip("/") + ".html"] = render_person_page(settings, name, groups[name])
    return rendered



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
      line-height: 1.5;
    }

    body.wide { --page: 70rem; }

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
      padding-top: 1.1rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--rule);
      background: var(--surface);
    }

    .brand {
      margin: 0;
      font-size: 1.2rem;
      font-weight: 780;
      letter-spacing: -0.01em;
    }

    .brand a { text-decoration: none; }

    .page-title {
      margin: 1.5rem 0 0.5rem;
      font-size: 1.6rem;
      font-weight: 720;
      letter-spacing: -0.015em;
      line-height: 1.25;
      text-wrap: balance;
    }

    .crumbs {
      margin-top: 1.25rem;
      color: var(--muted);
      font-size: 0.85rem;
    }

    .crumbs + .page-title,
    .crumbs + .item .page-title { margin-top: 0.5rem; }

    .item-meta {
      margin: 0 0 1.25rem;
      color: var(--muted);
      font-size: 0.875rem;
    }

    .lede {
      margin-bottom: 2rem;
      font-size: 1.0625rem;
      line-height: 1.7;
    }

    .edition-note.item-note { margin-top: 1.5rem; }

    .item-link {
      margin: 0.6rem 0 0;
      font-size: 0.9rem;
      font-weight: 600;
    }

    .item-link a,
    .crumbs a,
    .pager a,
    .prose a,
    .person-name a { color: var(--accent); }

    .person-name a { text-decoration: none; }

    .person-role {
      display: block;
      color: var(--muted);
      font-size: 0.875rem;
      font-weight: 400;
    }

    .pager {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 0.5rem 1rem;
      margin-top: 2rem;
      font-size: 0.9rem;
      font-weight: 600;
    }

    .prose { max-width: 68ch; line-height: 1.65; }

    .prose h2 {
      margin: 1.75rem 0 0.4rem;
      font-size: 1.1rem;
    }

    .prose p { margin: 0 0 0.9rem; }

    .archive-list {
      margin: 0;
      padding: 0.5rem 0 0;
      list-style: none;
    }

    .archive-list li { padding: 0.3rem 0; }

    .archive-list a { text-decoration-color: var(--rule); }

    .archive-source {
      display: block;
      color: var(--muted);
      font-size: 0.82rem;
    }

    h3 a { text-decoration: none; }

    h3 a:hover { text-decoration: underline; }

    .dek {
      margin: 0;
      color: var(--muted);
      font-size: 0.9rem;
    }

    body:not(.wide) .dek { display: none; }

    .header-links {
      display: flex;
      gap: 1rem;
      margin-left: auto;
      font-size: 0.9rem;
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
      margin: 0 0 0.5rem;
      padding: 0.9rem 0 0.4rem;
      color: var(--muted);
      font-size: 0.875rem;
    }

    .status strong { color: var(--ink); }

    .status .now {
      color: var(--accent);
      font-weight: 650;
    }

    .front {
      display: grid;
      gap: 2.5rem;
    }

    .day { padding: 0 0 1.75rem; }

    .day-label {
      display: flex;
      align-items: baseline;
      gap: 0.6rem;
      margin: 0;
      padding-bottom: 0.4rem;
      border-bottom: 2px solid var(--ink);
      font-size: 1rem;
      font-weight: 700;
    }

    .day-today .day-label { color: var(--accent); }

    .day-today .day-label { border-bottom-color: var(--accent); }

    .day-count {
      color: var(--muted);
      font-size: 0.85rem;
      font-weight: 400;
    }

    .day-empty {
      margin: 0;
      padding: 0.7rem 0 0;
      color: var(--muted);
    }

    .story-list {
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .story {
      padding: 0.7rem 0;
      border-bottom: 1px solid var(--rule);
    }

    .story:last-child { border-bottom: 0; }

    .story summary {
      display: flex;
      gap: 0.5rem;
      align-items: baseline;
      cursor: pointer;
      list-style: none;
    }

    .story summary::-webkit-details-marker { display: none; }

    .story summary:hover .headline {
      text-decoration: underline;
      text-decoration-color: var(--muted);
    }

    .mark { display: none; }

    .is-new .mark {
      display: block;
      flex: none;
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: var(--accent);
      transform: translateY(-0.1rem);
    }

    .headline {
      min-width: 0;
      font-size: 1.0625rem;
      font-weight: 500;
      line-height: 1.4;
    }

    .headline strong { font-weight: 700; }

    .story-body { padding: 0.5rem 0 0.25rem; }

    .media {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.35rem;
      margin: 0;
      padding-top: 0.3rem;
    }

    details[open] + .media { display: none; }

    .story-source {
      margin-right: 0.25rem;
      color: var(--muted);
      font-size: 0.875rem;
    }

    .medium {
      padding: 0.22rem 0.32rem;
      border: 1px solid var(--rule);
      border-radius: 3px;
      background: var(--surface);
      color: var(--muted);
      font: 600 0.68rem/1 var(--mono);
      letter-spacing: 0.04em;
      text-decoration: none;
    }

    .medium:hover {
      border-color: var(--accent);
      color: var(--accent);
    }

    .archive-link {
      margin: 0;
      font-weight: 650;
    }

    .archive-link a,
    .edition-note a,
    .source-title a { color: var(--accent); }

    .panel-label,
    .section-label {
      margin: 0 0 0.5rem;
      color: var(--muted);
      font-size: 0.75rem;
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
      grid-template-columns: repeat(auto-fill, minmax(10.5rem, 1fr));
      gap: 0 1.25rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .person {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 0.1rem 0.5rem;
      padding: 0.55rem 0;
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
      font: 0.75rem var(--mono);
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
      font-size: 0.82rem;
      text-decoration: none;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .person-latest:hover { text-decoration: underline; }

    .quiet {
      margin: 0;
      padding-top: 0.6rem;
      border-top: 1px solid var(--rule);
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.55;
    }

    .quiet strong { color: var(--ink); }

    @media (min-width: 64rem) {
      .front {
        grid-template-columns: minmax(0, 1fr) 17rem;
        gap: 3.5rem;
        align-items: start;
      }
      .roster { display: block; }
      .person-latest { display: block; }
    }

    .summary {
      max-width: 68ch;
      margin: 0;
      line-height: 1.6;
    }

    .source-list {
      display: grid;
      gap: 0.25rem;
      margin: 0.5rem 0 0;
      padding: 0;
      color: var(--muted);
      font-size: 0.875rem;
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
      font: 600 0.7rem var(--mono);
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
      padding: 1.1rem 0;
      border-bottom: 1px solid var(--rule);
    }

    .episode-list .episode:last-child { border-bottom: 0; }

    .episode-meta {
      margin: 0 0 0.2rem;
      color: var(--muted);
      font: 0.75rem var(--mono);
    }

    h2, h3 { text-wrap: balance; }

    h3 {
      margin: 0 0 0.3rem;
      font-size: 1.1rem;
      font-weight: 650;
      line-height: 1.35;
    }

    .edition-title {
      margin: 2.25rem 0 0;
      padding-bottom: 0.4rem;
      border-bottom: 2px solid var(--ink);
      font-size: 1.05rem;
      font-weight: 700;
    }

    .edition-note {
      margin: 0 0 1rem;
      color: var(--muted);
      font-size: 0.9rem;
    }

    .edition-empty {
      margin: 0;
      color: var(--muted);
    }

    main > .section-label:first-child { margin-top: 1.5rem; }

    .item .section-label { margin-top: 1.5rem; }

    .edition-title a { text-decoration: none; }

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
      padding: 0.75rem 0;
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
    wide: bool = False,
    path: str | None = None,
    description: str,
    og_type: str = "website",
    structured: tuple[dict[str, Any], ...] = (),
    noindex: bool = False,
    own_heading: bool = False,
) -> str:
    secondary = "\n      ".join(
        f'<a class="secondary-link" href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
        for label, href in NAV_LINKS
    )
    title_attr = html.escape(document_title, quote=True)
    description_attr = html.escape(description, quote=True)
    head: list[str] = []
    if noindex:
        head.append('<meta name="robots" content="noindex">')
    if path is not None:
        url = html.escape(settings.base_url + path, quote=True)
        head.extend((
            f'<link rel="canonical" href="{url}">',
            f'<meta property="og:site_name" content="{html.escape(settings.title, quote=True)}">',
            f'<meta property="og:type" content="{og_type}">',
            f'<meta property="og:title" content="{title_attr}">',
            f'<meta property="og:description" content="{description_attr}">',
            f'<meta property="og:url" content="{url}">',
            '<meta name="twitter:card" content="summary">',
        ))
    head.extend(json_ld_block(value) for value in structured)
    head_extra = "".join(f"\n  {line}" for line in head)
    brand_tag = "p" if own_heading else "h1"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(document_title)}</title>
  <meta name="description" content="{description_attr}">{head_extra}
  <link rel="alternate" type="application/rss+xml" title="{html.escape(settings.title, quote=True)}" href="/feed.xml">
  <style>{STYLE}  </style>
</head>
<body{' class="wide"' if wide else ""}>
  <header class="bar">
    <{brand_tag} class="brand"><a href="/">{html.escape(settings.title)}</a></{brand_tag}>
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
                f'<a href="{html.escape(feed_url, quote=True)}" rel="noopener">Feed</a>'
                ' <span aria-hidden="true">·</span> '
                f'<a href="{html.escape(homepage_url, quote=True)}" rel="noopener">Source</a>'
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
        main_content='<h1 class="page-title">Monitored sources</h1>' + "".join(groups),
        page_title=f"AI podcasts, YouTube channels, and newsletters we monitor — {settings.title}",
        section_label=None,
        path=FEEDS_PATH,
        description=meta_description(
            f"The {len(settings.sources)} podcast, YouTube, and newsletter feeds {settings.title} "
            "monitors for AI interviews and writing."
        ),
        own_heading=True,
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
            f'<a href="{html.escape(url, quote=True)}" rel="noopener">'
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
    days: list[dict[str, Any]] = (),
) -> str:
    """Render one RSS entry per closed daily edition, newest first."""
    ET.register_namespace("atom", ATOM_NAMESPACE)
    day_dates = {day["date"] for day in days}
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = settings.title
    ET.SubElement(channel, "link").text = settings.base_url + "/"
    ET.SubElement(channel, "description").text = settings.description
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(
        channel,
        f"{{{ATOM_NAMESPACE}}}link",
        {"href": settings.base_url + "/feed.xml", "rel": "self", "type": "application/rss+xml"},
    )
    last_close = parse_timestamp(editions[-1]["closed_at"]) if editions else None
    if last_close is not None:
        ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(last_close)
    for edition in reversed(editions):
        members = set(edition["item_ids"])
        edition_items = [item for item in items if str(item["id"]) in members]
        day = dt.date.fromisoformat(edition["date"])
        count = len(edition_items)
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = (
            f"{settings.title} — {edition_label(day)} ({count} item{'' if count == 1 else 's'})"
        )
        # Editions and site days close at different hours, so an edition's date may
        # have no day page yet.
        date = str(edition["date"])
        ET.SubElement(node, "link").text = settings.base_url + (
            day_path(date) if date in day_dates else ARCHIVE_PATH
        )
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
  Referrer-Policy: strict-origin-when-cross-origin
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
