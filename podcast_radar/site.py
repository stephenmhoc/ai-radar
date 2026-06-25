from __future__ import annotations

import datetime as dt
import email.utils
import shutil
from pathlib import Path
from urllib.parse import urljoin

from .config import Config
from . import storage
from .text import comma_join, escape, paragraphs_to_html, slugify


def build_site(config: Config, conn) -> dict[str, int]:
    episodes = storage.public_episodes(conn, limit=config.site.max_items)
    public_dir = config.app.public_dir
    if public_dir.exists():
        shutil.rmtree(public_dir)
    (public_dir / "assets").mkdir(parents=True, exist_ok=True)
    (public_dir / "episodes").mkdir(parents=True, exist_ok=True)

    (public_dir / "assets" / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    (public_dir / "assets" / "favicon.svg").write_text(FAVICON_SVG, encoding="utf-8")
    (public_dir / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
    if config.site.cname:
        (public_dir / "CNAME").write_text(config.site.cname + "\n", encoding="utf-8")

    slugs: dict[int, str] = {}
    for episode in episodes:
        slug = episode_slug(episode)
        slugs[int(episode["id"])] = slug
        episode_dir = public_dir / "episodes" / slug
        episode_dir.mkdir(parents=True, exist_ok=True)
        (episode_dir / "index.html").write_text(
            render_episode_page(config, episode, slug),
            encoding="utf-8",
        )

    (public_dir / "index.html").write_text(render_index(config, episodes, slugs), encoding="utf-8")
    (public_dir / "feed.xml").write_text(render_rss(config, episodes, slugs), encoding="utf-8")
    return {"episodes": len(episodes)}


def episode_slug(episode) -> str:
    title = episode["summary_title"] or episode["title"]
    return f"{episode['id']}-{slugify(title)}"


def render_index(config: Config, episodes, slugs: dict[int, str]) -> str:
    cards = "\n".join(render_card(config, episode, slugs[int(episode["id"])]) for episode in episodes)
    if not cards:
        cards = """
        <section class="empty">
          <h2>No relevant episodes yet</h2>
          <p>The radar has not published any qualifying episodes yet.</p>
        </section>
        """
    return page(
        config,
        title=config.site.title,
        body=f"""
        <header class="hero">
          <div>
            <p class="eyebrow">Podcast monitor</p>
            <h1>{escape(config.site.title)}</h1>
            <p class="lede">{escape(config.site.description)}</p>
          </div>
          <a class="rss-link" href="feed.xml">RSS feed</a>
        </header>
        <main class="episode-list">
          {cards}
        </main>
        """,
    )


def render_card(config: Config, episode, slug: str) -> str:
    image_url = episode["image_url"] or episode["feed_image_url"] or ""
    hosts = _people(episode["hosts_json"]) or _people(episode["feed_hosts_json"])
    guests = _people(episode["guests_json"]) or _people(episode["matched_people_json"])
    labs = _people(episode["labs_json"])
    date = _date_label(episode["published_at"])
    episode_url = episode_url_for(config, slug)
    image = f'<img src="{escape(image_url)}" alt="" loading="lazy">' if image_url else '<div class="art-fallback"></div>'
    lab_tags = "".join(f"<span>{escape(lab)}</span>" for lab in labs)
    return f"""
    <article class="episode-card">
      <a class="art" href="{escape(episode_url)}">{image}</a>
      <div class="episode-body">
        <div class="meta-row">
          <span>{escape(episode['feed_name'])}</span>
          <span>{escape(date)}</span>
        </div>
        <h2><a href="{escape(episode_url)}">{escape(episode['summary_title'] or episode['title'])}</a></h2>
        <p class="people"><strong>Hosts:</strong> {escape(comma_join(hosts) or 'Unknown')}</p>
        <p class="people"><strong>Guests:</strong> {escape(comma_join(guests) or 'Unknown')}</p>
        <p class="summary">{escape(episode['summary_text'] or '').splitlines()[0] if episode['summary_text'] else ''}</p>
        <div class="tags">{lab_tags}</div>
        <div class="actions">
          <a href="{escape(episode_url)}">View transcript</a>
          {source_link(episode)}
        </div>
      </div>
    </article>
    """


def render_episode_page(config: Config, episode, slug: str) -> str:
    image_url = episode["image_url"] or episode["feed_image_url"] or ""
    hosts = _people(episode["hosts_json"]) or _people(episode["feed_hosts_json"])
    guests = _people(episode["guests_json"]) or _people(episode["matched_people_json"])
    key_points = _people(episode["key_points_json"])
    topics = _people(episode["topics_json"])
    image = f'<img src="{escape(image_url)}" alt="" loading="lazy">' if image_url else '<div class="art-fallback"></div>'
    points = "".join(f"<li>{escape(point)}</li>" for point in key_points)
    topic_tags = "".join(f"<span>{escape(topic)}</span>" for topic in topics)
    transcript = paragraphs_to_html(episode["transcript_text"] or "")
    summary = episode["summary_html"] or paragraphs_to_html(episode["summary_text"] or "")
    title = episode["summary_title"] or episode["title"]
    return page(
        config,
        title=title,
        body=f"""
        <nav class="top-nav"><a href="../../index.html">All episodes</a><a href="../../feed.xml">RSS feed</a></nav>
        <main class="detail">
          <section class="detail-hero">
            <div class="detail-art">{image}</div>
            <div>
              <p class="eyebrow">{escape(episode['feed_name'])} · {escape(_date_label(episode['published_at']))}</p>
              <h1>{escape(title)}</h1>
              <p class="people"><strong>Hosts:</strong> {escape(comma_join(hosts) or 'Unknown')}</p>
              <p class="people"><strong>Guests:</strong> {escape(comma_join(guests) or 'Unknown')}</p>
              <div class="tags">{topic_tags}</div>
              <div class="actions">{source_link(episode)}</div>
            </div>
          </section>
          <section class="content-block">
            <h2>Summary</h2>
            {summary}
            <ul>{points}</ul>
          </section>
          <section class="content-block transcript">
            <h2>Transcript</h2>
            {transcript}
          </section>
        </main>
        """,
    )


def render_rss(config: Config, episodes, slugs: dict[int, str]) -> str:
    now = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc), usegmt=True)
    items = []
    for episode in episodes:
        slug = slugs[int(episode["id"])]
        link = episode_url_for(config, slug)
        pub_date = _rss_date(episode["published_at"])
        description = episode["summary_html"] or paragraphs_to_html(episode["summary_text"] or "")
        source = source_link(episode)
        items.append(
            f"""
            <item>
              <title>{escape(episode['summary_title'] or episode['title'])}</title>
              <link>{escape(link)}</link>
              <guid isPermaLink="true">{escape(link)}</guid>
              <pubDate>{escape(pub_date)}</pubDate>
              <description><![CDATA[{description}<p>{source}</p>]]></description>
            </item>
            """
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{escape(config.site.rss_title)}</title>
    <link>{escape(config.site.base_url.rstrip('/') + '/')}</link>
    <description>{escape(config.site.rss_description)}</description>
    <lastBuildDate>{escape(now)}</lastBuildDate>
    {''.join(items)}
  </channel>
</rss>
"""


def page(config: Config, *, title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <meta name="description" content="{escape(config.site.description)}">
  <link rel="alternate" type="application/rss+xml" title="{escape(config.site.rss_title)}" href="/feed.xml">
  <link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/style.css">
</head>
<body>
  <div class="shell">
    {body}
  </div>
</body>
</html>
"""


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#0f766e"/>
  <circle cx="22" cy="32" r="8" fill="#fbfcfd"/>
  <path d="M36 18v28M46 24v16M54 29v6" stroke="#f2e7d7" stroke-width="6" stroke-linecap="round"/>
</svg>
"""


def episode_url_for(config: Config, slug: str) -> str:
    if config.site.base_url:
        return urljoin(config.site.base_url.rstrip("/") + "/", f"episodes/{slug}/")
    return f"episodes/{slug}/"


def source_link(episode) -> str:
    if not episode["episode_url"]:
        return ""
    return f'<a href="{escape(episode["episode_url"])}">Original episode</a>'


def _people(value: str | None) -> list[str]:
    return storage.loads(value, default=[])


def _date_label(value: str | None) -> str:
    if not value:
        return "Undated"
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:16]
    return parsed.date().isoformat()


def _rss_date(value: str | None) -> str:
    if not value:
        return email.utils.format_datetime(dt.datetime.now(dt.timezone.utc), usegmt=True)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return email.utils.format_datetime(parsed.astimezone(dt.timezone.utc), usegmt=True)


STYLE_CSS = """
:root {
  color-scheme: light;
  --ink: #1f2933;
  --muted: #64717f;
  --line: #d9e1e8;
  --paper: #fbfcfd;
  --panel: #ffffff;
  --accent: #0f766e;
  --accent-2: #b45309;
  --link: #0b5cad;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.55;
}

a { color: var(--link); text-decoration-thickness: 0.08em; text-underline-offset: 0.18em; }

.shell {
  width: min(1120px, calc(100% - 32px));
  margin: 0 auto;
  padding: 28px 0 56px;
}

.hero {
  min-height: 260px;
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: flex-end;
  padding: 42px 0 30px;
  border-bottom: 1px solid var(--line);
}

.eyebrow {
  margin: 0 0 10px;
  color: var(--accent);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}

h1 {
  max-width: 760px;
  margin: 0;
  font-size: clamp(2.3rem, 7vw, 5.1rem);
  line-height: 0.95;
  letter-spacing: 0;
}

.lede {
  max-width: 680px;
  margin: 22px 0 0;
  color: var(--muted);
  font-size: 1.05rem;
}

.rss-link,
.actions a,
.top-nav a {
  display: inline-flex;
  align-items: center;
  min-height: 38px;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: var(--ink);
  text-decoration: none;
  font-size: 0.92rem;
}

.episode-list {
  display: grid;
  gap: 16px;
  padding: 28px 0;
}

.episode-card {
  display: grid;
  grid-template-columns: 148px 1fr;
  gap: 18px;
  padding: 16px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}

.art,
.detail-art {
  aspect-ratio: 1;
  width: 100%;
  overflow: hidden;
  border-radius: 8px;
  background: #e6edf2;
}

.art img,
.detail-art img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.art-fallback {
  width: 100%;
  height: 100%;
  background: linear-gradient(135deg, #dce8e5, #f2e7d7);
}

.meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  color: var(--muted);
  font-size: 0.86rem;
}

.episode-card h2 {
  margin: 6px 0 10px;
  font-size: 1.35rem;
  line-height: 1.18;
  letter-spacing: 0;
}

.episode-card h2 a {
  color: var(--ink);
  text-decoration: none;
}

.people,
.summary {
  margin: 4px 0;
  color: var(--muted);
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 12px 0;
}

.tags span {
  border: 1px solid #cbd9d5;
  color: #0f5f58;
  background: #eef8f5;
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 0.82rem;
}

.actions,
.top-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.top-nav {
  justify-content: space-between;
  padding: 18px 0 28px;
}

.detail {
  padding-bottom: 48px;
}

.detail-hero {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 28px;
  align-items: start;
  padding-bottom: 28px;
  border-bottom: 1px solid var(--line);
}

.content-block {
  max-width: 850px;
  margin: 30px 0 0;
  padding-bottom: 16px;
}

.content-block h2 {
  margin: 0 0 12px;
  font-size: 1.15rem;
}

.transcript {
  color: #303b45;
}

.empty {
  padding: 42px 0;
  color: var(--muted);
}

@media (max-width: 720px) {
  .shell { width: min(100% - 24px, 1120px); padding-top: 18px; }
  .hero { min-height: 220px; display: block; }
  .rss-link { margin-top: 18px; }
  .episode-card { grid-template-columns: 88px 1fr; gap: 12px; padding: 12px; }
  .episode-card h2 { font-size: 1.08rem; }
  .summary { display: none; }
  .detail-hero { grid-template-columns: 1fr; }
  .detail-art { max-width: 180px; }
}
"""
