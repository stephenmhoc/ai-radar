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

    rss = render_rss(config, episodes, slugs)
    (public_dir / "index.html").write_text(render_index(config, episodes, slugs), encoding="utf-8")
    (public_dir / "feed.xml").write_text(rss["xml"], encoding="utf-8")
    return {"episodes": len(episodes), "rss_items": rss["items"]}


def episode_slug(episode) -> str:
    title = episode["summary_title"] or episode["title"]
    return f"{episode['id']}-{slugify(title)}"


def render_index(config: Config, episodes, slugs: dict[int, str]) -> str:
    cards = "\n".join(render_card(config, episode, slugs[int(episode["id"])]) for episode in episodes)
    filters = render_filters(episodes)
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
        {filters}
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
    episode_path = episode_path_for(slug)
    image = f'<img src="{escape(image_url)}" alt="" loading="lazy">' if image_url else '<div class="art-fallback"></div>'
    lab_tags = "".join(f"<span>{escape(lab)}</span>" for lab in labs)
    summary = _summary_teaser(episode)
    status = _card_status(episode)
    status_html = f"<span>{escape(status)}</span>" if status else ""
    lab_tokens = " ".join(_lab_token(lab) for lab in labs)
    return f"""
    <article class="episode-card" data-labs="{escape(lab_tokens)}">
      <a class="art" href="{escape(episode_path)}">{image}</a>
      <div class="episode-body">
        <div class="meta-row">
          <span>{escape(episode['feed_name'])}</span>
          <span>{escape(date)}</span>
          {status_html}
        </div>
        <h2><a href="{escape(episode_path)}">{escape(episode['summary_title'] or episode['title'])}</a></h2>
        <p class="people"><strong>Hosts:</strong> {escape(comma_join(hosts) or 'Unknown')}</p>
        <p class="people"><strong>Guests:</strong> {escape(comma_join(guests) or 'Unknown')}</p>
        <p class="summary">{escape(summary)}</p>
        <div class="tags">{lab_tags}</div>
        <div class="actions">
          <a href="{escape(episode_path)}">Episode details</a>
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
    transcript = _transcript_html(episode)
    summary = _summary_html(episode)
    title = episode["summary_title"] or episode["title"]
    status = _detail_status(episode)
    status_html = f'<p class="status-pill">{escape(status)}</p>' if status else ""
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
              {status_html}
              <p class="people"><strong>Hosts:</strong> {escape(comma_join(hosts) or 'Unknown')}</p>
              <p class="people"><strong>Guests:</strong> {escape(comma_join(guests) or 'Unknown')}</p>
              <div class="tags">{topic_tags}</div>
              <div class="actions"><a href="#transcript">Jump to transcript</a>{source_link(episode)}</div>
            </div>
          </section>
          <section class="content-block">
            <h2>Summary</h2>
            {summary}
            <ul>{points}</ul>
          </section>
          <section class="content-block transcript" id="transcript">
            <h2>Transcript</h2>
            {transcript}
          </section>
        </main>
        """,
    )


def render_rss(config: Config, episodes, slugs: dict[int, str]) -> dict[str, int | str]:
    now = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc), usegmt=True)
    items = []
    for episode in episodes:
        if not _rss_ready(episode):
            continue
        slug = slugs[int(episode["id"])]
        link = episode_url_for(config, slug)
        pub_date = _rss_date(episode["published_at"])
        description = _rss_description(episode)
        items.append(
            f"""
            <item>
              <title>{escape(episode['summary_title'] or episode['title'])}</title>
              <link>{escape(link)}</link>
              <guid isPermaLink="true">{escape(link)}</guid>
              <pubDate>{escape(pub_date)}</pubDate>
              <description><![CDATA[{description}]]></description>
            </item>
            """
        )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
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
    return {"xml": xml, "items": len(items)}


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
  <script>
{FILTER_SCRIPT}
  </script>
</body>
</html>
"""


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#0f766e"/>
  <circle cx="22" cy="32" r="8" fill="#fbfcfd"/>
  <path d="M36 18v28M46 24v16M54 29v6" stroke="#f2e7d7" stroke-width="6" stroke-linecap="round"/>
</svg>
"""


FILTER_SCRIPT = """(() => {
    const buttons = Array.from(document.querySelectorAll("[data-filter]"));
    const cards = Array.from(document.querySelectorAll(".episode-card"));
    if (!buttons.length || !cards.length) return;

    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const filter = button.dataset.filter || "all";
        buttons.forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", active ? "true" : "false");
        });
        cards.forEach((card) => {
          const labs = (card.dataset.labs || "").split(" ");
          card.hidden = filter !== "all" && !labs.includes(filter);
        });
      });
    });
  })();"""


def episode_url_for(config: Config, slug: str) -> str:
    return urljoin(config.site.base_url.rstrip("/") + "/", episode_path_for(slug).lstrip("/"))


def episode_path_for(slug: str) -> str:
    return f"/episodes/{slug}/"


def source_link(episode, *, icon: bool = True) -> str:
    if not episode["episode_url"]:
        return ""
    icon_html = '<span class="external-icon" aria-hidden="true">↗</span>' if icon else ""
    return f'<a class="external-link" href="{escape(episode["episode_url"])}" target="_blank" rel="noopener noreferrer">Original episode{icon_html}</a>'


def render_filters(episodes) -> str:
    counts: dict[str, int] = {}
    for episode in episodes:
        for lab in _people(episode["labs_json"]):
            counts[lab] = counts.get(lab, 0) + 1
    buttons = [
        f'<button type="button" class="filter-button active" data-filter="all" aria-pressed="true">All <span>{len(episodes)}</span></button>'
    ]
    for lab, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower())):
        buttons.append(
            f'<button type="button" class="filter-button" data-filter="{escape(_lab_token(lab))}" aria-pressed="false">{escape(lab)} <span>{count}</span></button>'
        )
    return f"""
    <section class="filters" aria-label="Company filters">
      <div class="filter-label">Company</div>
      <div class="filter-buttons">{''.join(buttons)}</div>
    </section>
    """


def _rss_ready(episode) -> bool:
    return bool(
        episode["transcript_text"]
        and (episode["summary_text"] or episode["summary_html"])
        and episode["status"] == "published"
    )


def _rss_description(episode) -> str:
    hosts = _people(episode["hosts_json"]) or _people(episode["feed_hosts_json"])
    guests = _people(episode["guests_json"]) or _people(episode["matched_people_json"])
    labs = _people(episode["labs_json"])
    summary = episode["summary_html"] or paragraphs_to_html(episode["summary_text"] or "")
    source = source_link(episode, icon=False)
    source_paragraph = f"<p>{source}</p>" if source else ""
    return f"""
<p><strong>Podcast:</strong> {escape(episode['feed_name'])}</p>
<p><strong>Episode:</strong> {escape(episode['title'])}</p>
<p><strong>Hosts:</strong> {escape(comma_join(hosts) or 'Unknown')}</p>
<p><strong>Guests:</strong> {escape(comma_join(guests) or 'Unknown')}</p>
<p><strong>Where they work:</strong> {escape(comma_join(labs) or 'Unknown')}</p>
<h3>Summary</h3>
{summary}
{source_paragraph}
"""


def _summary_teaser(episode) -> str:
    if episode["summary_text"]:
        return str(episode["summary_text"]).splitlines()[0]
    description = str(episode["description"] or "").strip()
    if description:
        return "Summary pending. " + description[:260].strip()
    return "Summary pending."


def _summary_html(episode) -> str:
    if episode["summary_html"] or episode["summary_text"]:
        return episode["summary_html"] or paragraphs_to_html(episode["summary_text"] or "")
    description = str(episode["description"] or "").strip()
    if description:
        return f'<p class="notice">Summary pending. Metadata is shown until local transcription and summarization finish.</p>{paragraphs_to_html(description)}'
    return '<p class="notice">Summary pending. Local transcription and summarization have not finished yet.</p>'


def _transcript_html(episode) -> str:
    if episode["transcript_text"]:
        return paragraphs_to_html(episode["transcript_text"] or "")
    status = str(episode["status"])
    reason = str(episode["skip_reason"] or "").strip()
    if status == "transcription_failed":
        detail = f" {escape(reason)}" if reason else ""
        return f'<p class="notice error">Transcript unavailable because local transcription failed.{detail}</p>'
    if status == "summary_failed":
        return '<p class="notice">Transcript is available, but summarization failed.</p>'
    return '<p class="notice">Transcript pending. This episode has been included and local transcription is still running or queued.</p>'


def _card_status(episode) -> str:
    status = str(episode["status"])
    if status == "relevant":
        return "Pending transcript"
    if status == "transcribed":
        return "Pending summary"
    if status == "transcription_failed":
        return "Transcription failed"
    if status == "summary_failed":
        return "Summary failed"
    return ""


def _detail_status(episode) -> str:
    status = str(episode["status"])
    labels = {
        "relevant": "Transcript pending",
        "transcribed": "Summary pending",
        "transcription_failed": "Transcription failed",
        "summary_failed": "Summary failed",
    }
    return labels.get(status, "")


def _lab_token(lab: str) -> str:
    return slugify(lab)


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
  min-height: 230px;
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: flex-end;
  padding: 38px 0 26px;
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
.top-nav a,
.filter-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 38px;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: var(--ink);
  text-decoration: none;
  font-size: 0.92rem;
}

.filters {
  display: grid;
  grid-template-columns: 92px 1fr;
  gap: 14px;
  align-items: start;
  padding: 18px 0 8px;
}

.filter-label {
  color: var(--muted);
  font-size: 0.82rem;
  font-weight: 700;
  text-transform: uppercase;
}

.filter-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.filter-button {
  cursor: pointer;
  color: var(--muted);
  font: inherit;
  min-height: 34px;
  padding: 6px 10px;
}

.filter-button span {
  color: var(--muted);
  font-size: 0.78rem;
}

.filter-button.active {
  color: var(--ink);
  border-color: #8ebbb4;
  background: #eef8f5;
}

.episode-list {
  display: grid;
  gap: 10px;
  padding: 18px 0;
}

.episode-card {
  display: grid;
  grid-template-columns: 92px minmax(0, 1fr);
  gap: 14px;
  padding: 12px;
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
  gap: 6px 10px;
  color: var(--muted);
  font-size: 0.86rem;
}

.meta-row span:not(:last-child)::after {
  content: "·";
  color: #9aa6b2;
  margin-left: 10px;
}

.episode-card h2 {
  margin: 4px 0 8px;
  font-size: 1.13rem;
  line-height: 1.2;
  letter-spacing: 0;
}

.episode-card h2 a {
  color: var(--ink);
  text-decoration: none;
}

.people,
.summary {
  margin: 3px 0;
  color: var(--muted);
}

.people {
  font-size: 0.94rem;
}

.summary {
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.status-pill,
.notice {
  color: var(--muted);
  background: #eef4f8;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
}

.status-pill {
  display: inline-block;
  margin: 14px 0 8px;
  font-size: 0.9rem;
}

.notice.error {
  color: #8a2f24;
  background: #fff1ee;
  border-color: #f0c4bc;
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 9px 0;
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
  flex-wrap: nowrap;
  gap: 8px;
}

.actions a {
  min-height: 36px;
  white-space: nowrap;
}

.external-icon {
  line-height: 1;
  font-size: 0.95em;
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
  .filters { grid-template-columns: 1fr; gap: 8px; padding-top: 16px; }
  .filter-buttons {
    flex-wrap: nowrap;
    overflow-x: auto;
    padding-bottom: 4px;
    scrollbar-width: none;
  }
  .filter-buttons::-webkit-scrollbar { display: none; }
  .filter-button { flex: 0 0 auto; }
  .episode-card { grid-template-columns: 1fr; gap: 10px; padding: 12px; }
  .art {
    aspect-ratio: 16 / 5;
    max-height: 96px;
  }
  .episode-card h2 { font-size: 1.08rem; }
  .summary { display: none; }
  .actions { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .actions a { min-width: 0; padding: 8px 9px; }
  .detail-hero { grid-template-columns: 1fr; }
  .detail-art { max-width: 180px; }
}
"""
