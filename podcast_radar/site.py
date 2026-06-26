from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import shutil
from pathlib import Path
from urllib.parse import urljoin

from .config import Config
from . import storage
from .text import comma_join, escape, paragraphs_to_html, slugify, transcript_to_html


def build_site(config: Config, conn) -> dict[str, int]:
    episodes = storage.public_episodes(conn, limit=config.site.max_items)
    public_dir = config.app.public_dir
    if public_dir.exists():
        shutil.rmtree(public_dir)
    (public_dir / "assets").mkdir(parents=True, exist_ok=True)
    (public_dir / "episodes").mkdir(parents=True, exist_ok=True)

    style_name = style_asset_name()
    (public_dir / "assets" / style_name).write_text(STYLE_CSS, encoding="utf-8")
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
    controls = render_controls(config, episodes)
    search_panel = render_search_panel(episodes)
    if not cards:
        cards = """
        <section class="empty">
          <h2>No relevant episodes yet</h2>
          <p>The radar has not published any fully verified episodes yet.</p>
        </section>
        """
    return page(
        config,
        title=config.site.title,
        body=f"""
        <header class="masthead">
          <div>
            <p class="eyebrow">AI lab podcast intelligence</p>
            <h1>{escape(config.site.title)}</h1>
            <p class="lede">{escape(config.site.description)}</p>
          </div>
          {search_panel}
        </header>
        {controls}
        <div class="content-grid">
          <main aria-live="polite">
            <div class="episode-list">
              {cards}
            </div>
            <nav class="pagination" data-pagination aria-label="Episode pages" hidden>
              <button type="button" data-page-prev>Previous</button>
              <div class="page-buttons" data-page-buttons></div>
              <button type="button" data-page-next>Next</button>
              <p data-page-status></p>
            </nav>
          </main>
          {render_side_rail(episodes, slugs)}
        </div>
        """,
    )


def render_card(config: Config, episode, slug: str) -> str:
    image_url = episode["image_url"] or episode["feed_image_url"] or ""
    hosts = _people(episode["hosts_json"]) or _people(episode["feed_hosts_json"])
    guests = _people(episode["guests_json"]) or _people(episode["matched_people_json"])
    labs = _people(episode["labs_json"])
    topics = _people(episode["topics_json"])
    episode_path = episode_path_for(slug)
    image = f'<img src="{escape(image_url)}" alt="" loading="lazy">' if image_url else '<div class="art-fallback"></div>'
    display_topics = _display_topics(topics, labs)
    summary = _summary_teaser(episode)
    topline = _topline(episode, guests=guests)
    source_title = _compact_sentence(str(episode["title"]), max_chars=92)
    lab_tokens = " ".join(_lab_token(lab) for lab in labs)
    topic_tokens = " ".join(_lab_token(topic) for topic in display_topics)
    search_text = " ".join(
        str(value or "")
        for value in (
            episode["feed_name"],
            episode["summary_title"] or episode["title"],
            comma_join(hosts),
            comma_join(guests),
            comma_join(labs),
            comma_join(topics),
            summary,
        )
    ).lower()
    published_sort = episode["published_at"] or ""
    feed_token = _lab_token(str(episode["feed_name"]))
    return f"""
    <a class="episode-card" href="{escape(episode_path)}" data-labs="{escape(lab_tokens)}" data-topics="{escape(topic_tokens)}" data-feed="{escape(feed_token)}" data-search="{escape(search_text)}" data-date="{escape(published_sort)}">
      <span class="art">{image}</span>
      <div class="episode-body">
        <h2>{escape(topline)}</h2>
        <p class="source-line"><span>{escape(episode['feed_name'])}</span><span>{escape(source_title)}</span></p>
      </div>
    </a>
    """


def render_episode_page(config: Config, episode, slug: str) -> str:
    image_url = episode["image_url"] or episode["feed_image_url"] or ""
    hosts = _people(episode["hosts_json"]) or _people(episode["feed_hosts_json"])
    guests = _people(episode["guests_json"]) or _people(episode["matched_people_json"])
    key_points = _people(episode["key_points_json"])
    topics = _people(episode["topics_json"])
    labs = _people(episode["labs_json"])
    image = f'<img src="{escape(image_url)}" alt="" loading="lazy">' if image_url else '<div class="art-fallback"></div>'
    points = "".join(f"<li>{escape(point)}</li>" for point in key_points)
    topic_tags = "".join(f"<span>{escape(topic)}</span>" for topic in _display_topics(topics, labs))
    transcript = _transcript_html(episode)
    summary = _summary_html(episode)
    title = episode["summary_title"] or episode["title"]
    status = _detail_status(episode)
    status_html = f'<p class="status-pill">{escape(status)}</p>' if status else ""
    why = _why_it_matters(episode)
    digest = render_episode_digest(episode, hosts=hosts, guests=guests, labs=labs, key_points=key_points, why=why)
    return page(
        config,
        title=title,
        body=f"""
        <nav class="top-nav" aria-label="Episode navigation"><a href="../../index.html">Back to episodes</a></nav>
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
              <div class="actions"><a href="#summary">Read summary</a><a href="#transcript">Jump to transcript</a>{source_link(episode)}</div>
            </div>
          </section>
          {digest}
          <section class="content-block summary-block" id="summary">
            <p class="eyebrow">Episode summary</p>
            <h2>Summary</h2>
            {summary}
            <ul>{points}</ul>
          </section>
          <section class="content-block transcript" id="transcript">
            <p class="eyebrow">Source material</p>
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
    style_name = style_asset_name()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <meta name="description" content="{escape(config.site.description)}">
  <link rel="alternate" type="application/rss+xml" title="{escape(config.site.rss_title)}" href="/feed.xml">
  <link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/{style_name}">
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


def style_asset_name() -> str:
    digest = hashlib.sha256(STYLE_CSS.encode("utf-8")).hexdigest()[:10]
    return f"style-{digest}.css"


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#0f766e"/>
  <circle cx="22" cy="32" r="8" fill="#fbfcfd"/>
  <path d="M36 18v28M46 24v16M54 29v6" stroke="#f2e7d7" stroke-width="6" stroke-linecap="round"/>
</svg>
"""


FILTER_SCRIPT = """(() => {
    const PAGE_SIZE = 24;
    const buttons = Array.from(document.querySelectorAll("[data-filter]"));
    const cards = Array.from(document.querySelectorAll(".episode-card"));
    const list = document.querySelector(".episode-list");
    const searchInput = document.querySelector("[data-search-input]");
    const resultCount = document.querySelector("[data-result-count]");
    const pagination = document.querySelector("[data-pagination]");
    const pageButtons = document.querySelector("[data-page-buttons]");
    const pageStatus = document.querySelector("[data-page-status]");
    const previousPage = document.querySelector("[data-page-prev]");
    const nextPage = document.querySelector("[data-page-next]");
    if (!buttons.length || !cards.length) return;

    let activeLab = "all";
    let activePage = 1;

    const matchingCards = () => {
      const queryTokens = (searchInput?.value || "")
        .trim()
        .toLowerCase()
        .split(/\\s+/)
        .filter(Boolean);

      return cards.filter((card) => {
        const labs = (card.dataset.labs || "").split(" ");
        const matchesLab = activeLab === "all" || labs.includes(activeLab);
        const haystack = card.dataset.search || "";
        const matchesSearch = !queryTokens.length || queryTokens.every((token) => haystack.includes(token));
        return matchesLab && matchesSearch;
      });
    };

    const renderPagination = (total) => {
      const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
      activePage = Math.min(Math.max(activePage, 1), pageCount);

      if (!pagination || !pageButtons || !pageStatus || !previousPage || !nextPage) return;
      pagination.hidden = total <= PAGE_SIZE;
      pageButtons.innerHTML = "";

      const pages = Array.from({ length: pageCount }, (_, index) => index + 1)
        .filter((page) => page === 1 || page === pageCount || Math.abs(page - activePage) <= 1);
      let lastPage = 0;
      pages.forEach((page) => {
        if (page - lastPage > 1) {
          const gap = document.createElement("span");
          gap.textContent = "More";
          gap.className = "page-gap";
          pageButtons.append(gap);
        }
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = String(page);
        button.dataset.pageNumber = String(page);
        button.setAttribute("aria-current", page === activePage ? "page" : "false");
        button.addEventListener("click", () => {
          activePage = page;
          apply();
          list?.scrollIntoView({ block: "start" });
        });
        pageButtons.append(button);
        lastPage = page;
      });

      previousPage.disabled = activePage <= 1;
      nextPage.disabled = activePage >= pageCount;
      const start = total ? (activePage - 1) * PAGE_SIZE + 1 : 0;
      const end = Math.min(activePage * PAGE_SIZE, total);
      pageStatus.textContent = total > PAGE_SIZE ? `Showing ${start}-${end} of ${total}` : "";
    };

    const apply = () => {
      const matches = matchingCards();
      const visible = matches.length;
      const pageCount = Math.max(1, Math.ceil(visible / PAGE_SIZE));
      activePage = Math.min(activePage, pageCount);
      const pageStart = (activePage - 1) * PAGE_SIZE;
      const pageCards = new Set(matches.slice(pageStart, pageStart + PAGE_SIZE));

      cards.forEach((card) => {
        card.hidden = !pageCards.has(card);
      });

      if (resultCount) resultCount.textContent = String(visible);
      renderPagination(visible);

      let empty = document.querySelector("[data-filter-empty]");
      if (!visible) {
        if (!empty) {
          empty = document.createElement("section");
          empty.className = "empty";
          empty.dataset.filterEmpty = "true";
          empty.innerHTML = "<h2>No matching episodes</h2><p>Adjust the filters or search terms.</p>";
          list?.append(empty);
        }
      } else {
        empty?.remove();
      }
    };

    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        activeLab = button.dataset.filter || "all";
        activePage = 1;
        buttons.forEach((item) => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", active ? "true" : "false");
        });
        apply();
      });
    });

    previousPage?.addEventListener("click", () => {
      activePage -= 1;
      apply();
      list?.scrollIntoView({ block: "start" });
    });

    nextPage?.addEventListener("click", () => {
      activePage += 1;
      apply();
      list?.scrollIntoView({ block: "start" });
    });

    searchInput?.addEventListener("input", () => {
      activePage = 1;
      apply();
    });

    apply();
  })();"""


def episode_url_for(config: Config, slug: str) -> str:
    return urljoin(config.site.base_url.rstrip("/") + "/", episode_path_for(slug).lstrip("/"))


def episode_path_for(slug: str) -> str:
    return f"/episodes/{slug}/"


def source_link(episode, *, icon: bool = True, label: str = "Go to episode") -> str:
    if not episode["episode_url"]:
        return ""
    icon_html = '<span class="external-icon" aria-hidden="true">↗</span>' if icon else ""
    return f'<a class="external-link" href="{escape(episode["episode_url"])}" target="_blank" rel="noopener noreferrer">{escape(label)}{icon_html}</a>'


def render_briefing(config: Config, episodes, slugs: dict[int, str]) -> str:
    if not episodes:
        return ""
    lead = episodes[0]
    lead_slug = slugs[int(lead["id"])]
    lead_path = episode_path_for(lead_slug)
    image_url = lead["image_url"] or lead["feed_image_url"] or ""
    image = f'<img src="{escape(image_url)}" alt="" loading="lazy">' if image_url else '<div class="art-fallback"></div>'
    guests = _people(lead["guests_json"]) or _people(lead["matched_people_json"])
    labs = _people(lead["labs_json"])
    lead_topics = _display_topics(_people(lead["topics_json"]), labs)
    lead_topic_tags = "".join(f"<span>{escape(topic)}</span>" for topic in lead_topics[:4])
    lab_count = len({lab for episode in episodes for lab in _people(episode["labs_json"])})
    feed_count = len({str(episode["feed_name"]) for episode in episodes})
    topic_count = len({_lab_token(topic) for episode in episodes for topic in _display_topics(_people(episode["topics_json"]), _people(episode["labs_json"]))})
    updated = _date_label(lead["published_at"])
    queue = "".join(render_signal_item(episode, slugs[int(episode["id"])]) for episode in episodes[1:4])
    return f"""
    <section class="briefing" aria-label="Latest AI lab episode">
      <article class="spotlight">
        <a class="spotlight-art" href="{escape(lead_path)}">{image}</a>
        <div class="spotlight-copy">
          <p class="eyebrow">Latest verified signal</p>
          <h2><a href="{escape(lead_path)}">{escape(lead['summary_title'] or lead['title'])}</a></h2>
          <p class="spotlight-meta">{escape(comma_join(guests) or 'Unknown guest')} · {escape(comma_join(labs) or 'Unknown lab')} · {escape(lead['feed_name'])}</p>
          <p class="spotlight-signal">{escape(_why_it_matters(lead))}</p>
          <div class="topic-tags">{lead_topic_tags}</div>
          <div class="actions"><a href="{escape(lead_path)}">Open episode</a>{source_link(lead)}</div>
        </div>
      </article>
      <aside class="radar-panel">
        <div class="metric-grid" aria-label="Coverage summary">
          <div><strong>{len(episodes)}</strong><span>Episodes</span></div>
          <div><strong>{lab_count}</strong><span>Labs</span></div>
          <div><strong>{feed_count}</strong><span>Podcasts</span></div>
          <div><strong>{topic_count}</strong><span>Topics</span></div>
        </div>
        <div class="lab-meter">
          <div class="panel-heading"><span>Coverage</span><span>Latest {escape(updated)}</span></div>
          {render_lab_meter(episodes)}
        </div>
        <div class="signal-queue">
          <div class="panel-heading"><span>Next signals</span><span>Newest first</span></div>
          {queue}
        </div>
      </aside>
    </section>
    """


def render_lab_meter(episodes) -> str:
    counts: dict[str, int] = {}
    for episode in episodes:
        for lab in _people(episode["labs_json"]):
            counts[lab] = counts.get(lab, 0) + 1
    if not counts:
        return ""
    max_count = max(counts.values())
    rows = []
    for lab, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))[:6]:
        width = max(8, round((count / max_count) * 100))
        rows.append(
            f"""
            <div class="meter-row">
              <span>{escape(lab)}</span>
              <div><i style="width: {width}%"></i></div>
              <b>{count}</b>
            </div>
            """
        )
    return "".join(rows)


def render_signal_item(episode, slug: str) -> str:
    labs = _people(episode["labs_json"])
    guests = _people(episode["guests_json"]) or _people(episode["matched_people_json"])
    return f"""
    <a class="signal-item" href="{escape(episode_path_for(slug))}">
      <span>{escape(_date_label(episode["published_at"]))}</span>
      <strong>{escape(episode["summary_title"] or episode["title"])}</strong>
      <em>{escape(comma_join(guests) or "Unknown guest")} · {escape(comma_join(labs) or "Unknown lab")}</em>
    </a>
    """


def render_search_panel(episodes) -> str:
    lab_count = len({lab for episode in episodes for lab in _people(episode["labs_json"])})
    feed_count = len({str(episode["feed_name"]) for episode in episodes})
    latest = _date_label(episodes[0]["published_at"]) if episodes else "No episodes"
    return f"""
    <div class="search-panel">
      <label class="command-search">
        <span>Search radar</span>
        <input type="search" placeholder="Guest, lab, claim, topic, podcast" data-search-input>
      </label>
      <div class="search-meta">
        <span><strong data-result-count>{len(episodes)}</strong> visible</span>
        <span>{lab_count} labs</span>
        <span>{feed_count} podcasts</span>
        <span>latest {escape(latest)}</span>
      </div>
    </div>
    """


def render_side_rail(episodes, slugs: dict[int, str]) -> str:
    if not episodes:
        return ""
    latest = "".join(render_rail_item(episode, slugs[int(episode["id"])]) for episode in episodes[:5])
    return f"""
    <aside class="side-rail" aria-label="Radar sidebar">
      <section class="rail-card rss-card">
        <p class="eyebrow">Follow along</p>
        <a class="follow-link" href="feed.xml">Use our RSS feed to stay up to date <span aria-hidden="true">↗</span></a>
      </section>
      <section class="rail-card">
        <div class="rail-heading"><span>Newest episodes</span><span>{len(episodes)} total</span></div>
        <div class="rail-list">{latest}</div>
      </section>
    </aside>
    """


def render_rail_item(episode, slug: str) -> str:
    guests = _people(episode["guests_json"]) or _people(episode["matched_people_json"])
    return f"""
    <a class="rail-item" href="{escape(episode_path_for(slug))}">
      <strong>{escape(_compact_sentence(_topline(episode, guests=guests), max_chars=74))}</strong>
      <span>{escape(episode["feed_name"])}</span>
    </a>
    """


def render_controls(config: Config, episodes) -> str:
    lab_counts: dict[str, int] = {}
    for episode in episodes:
        for lab in _people(episode["labs_json"]):
            lab_counts[lab] = lab_counts.get(lab, 0) + 1

    buttons = [
        f'<button type="button" class="filter-button active" data-filter="all" aria-pressed="true">All <span>{len(episodes)}</span></button>'
    ]
    for lab, count in sorted(lab_counts.items(), key=lambda item: (-item[1], item[0].lower())):
        buttons.append(
            f'<button type="button" class="filter-button" data-filter="{escape(_lab_token(lab))}" aria-pressed="false">{escape(lab)} <span>{count}</span></button>'
        )

    return f"""
    <section class="filters" aria-label="Company filters">
      <div class="filter-label">Quick lab pivots</div>
      <div class="filter-buttons">{''.join(buttons)}</div>
    </section>
    """


def render_episode_digest(episode, *, hosts: list[str], guests: list[str], labs: list[str], key_points: list[str], why: str) -> str:
    fact_rows = [
        ("Podcast", str(episode["feed_name"])),
        ("Published", _date_label(episode["published_at"])),
        ("Duration", _duration_label(episode["duration"]) or "Unknown"),
        ("Guests", comma_join(guests) or "Unknown"),
        ("Labs", comma_join(labs) or "Unknown"),
        ("Hosts", comma_join(hosts) or "Unknown"),
    ]
    facts = "".join(f"<div><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>" for label, value in fact_rows)
    top_points = "".join(f"<li>{escape(point)}</li>" for point in key_points[:4])
    points_html = f"<ul>{top_points}</ul>" if top_points else '<p class="notice">Key points will appear after summarization.</p>'
    return f"""
    <section class="brief-grid" aria-label="Episode brief">
      <div class="brief-main">
        <p class="eyebrow">Why it matters</p>
        <p class="brief-signal">{escape(why)}</p>
        <h2>Key claims</h2>
        {points_html}
      </div>
      <aside class="brief-facts">
        <h2>Episode facts</h2>
        <dl>{facts}</dl>
      </aside>
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


def _why_it_matters(episode) -> str:
    key_points = _people(episode["key_points_json"])
    if key_points:
        return key_points[0]
    summary = str(episode["summary_text"] or "").strip()
    if summary:
        return _first_sentence(summary)
    reason = str(episode["skip_reason"] or "").strip()
    if reason:
        return reason
    description = str(episode["description"] or "").strip()
    if description:
        return _first_sentence(description)
    return "This episode was verified as relevant to major AI lab activity."


def _topline(episode, *, guests: list[str]) -> str:
    guest_label = comma_join(guests) or "The guest"
    signal = _why_it_matters(episode)
    for guest in guests:
        if signal.casefold().startswith(guest.casefold()):
            return _compact_sentence(signal, max_chars=92)
    return _compact_sentence(f"{guest_label}: {signal}", max_chars=92)


def _compact_sentence(value: str, *, max_chars: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    cutoff = normalized.rfind(" ", 0, max_chars - 1)
    if cutoff < max_chars * 0.62:
        cutoff = max_chars - 1
    return normalized[:cutoff].rstrip(" ,;:.-")


def _first_sentence(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return ""
    for index, char in enumerate(normalized):
        if char in ".!?" and index >= 36:
            return normalized[: index + 1]
    return normalized[:220].strip()


def _affiliation_label(labs: list[str]) -> str:
    if not labs:
        return ""
    return f' <span>{escape(comma_join(labs))}</span>'


def _summary_html(episode) -> str:
    if episode["summary_html"] or episode["summary_text"]:
        return episode["summary_html"] or paragraphs_to_html(episode["summary_text"] or "")
    description = str(episode["description"] or "").strip()
    if description:
        return f'<p class="notice">Summary pending. Metadata is shown until local transcription and summarization finish.</p>{paragraphs_to_html(description)}'
    return '<p class="notice">Summary pending. Local transcription and summarization have not finished yet.</p>'


def _transcript_html(episode) -> str:
    if episode["transcript_text"]:
        return transcript_to_html(episode["transcript_text"] or "")
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


def _display_topics(topics: list[str], labs: list[str]) -> list[str]:
    lab_tokens = {_lab_token(lab) for lab in labs}
    display: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        token = _lab_token(topic)
        if not token or token in lab_tokens or token in seen:
            continue
        display.append(topic)
        seen.add(token)
    return display


def _date_label(value: str | None) -> str:
    if not value:
        return "Undated"
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:16]
    return parsed.date().isoformat()


def _duration_label(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if text.isdigit():
        total_seconds = int(text)
        minutes = total_seconds // 60
        hours = minutes // 60
        remaining_minutes = minutes % 60
        if hours:
            return f"{hours}h {remaining_minutes:02d}m"
        return f"{minutes}m"
    parts = text.split(":")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        hours, minutes, _seconds = (int(part) for part in parts)
        if hours:
            return f"{hours}h {minutes:02d}m"
        return f"{minutes}m"
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        minutes, _seconds = (int(part) for part in parts)
        return f"{minutes}m"
    return text


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
  --ink-strong: #111827;
  --muted: #64717f;
  --muted-2: #8a97a6;
  --line: #d9e1e8;
  --line-strong: #b9c7d3;
  --paper: #fbfcfd;
  --panel: #ffffff;
  --panel-warm: #fffaf2;
  --accent: #0f766e;
  --accent-2: #b45309;
  --accent-3: #2f5f9f;
  --link: #0b5cad;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background:
    radial-gradient(circle at 12% -8%, rgba(15, 118, 110, 0.10), transparent 30rem),
    linear-gradient(180deg, #f7faf9 0, var(--paper) 420px);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.55;
}

a { color: var(--link); text-decoration-thickness: 0.08em; text-underline-offset: 0.18em; }

.shell {
  width: min(1180px, calc(100% - 32px));
  margin: 0 auto;
  padding: 24px 0 56px;
}

.masthead {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(340px, 0.72fr);
  gap: 28px;
  align-items: end;
  padding: 14px 0 18px;
}

.eyebrow {
  margin: 0 0 10px;
  color: var(--accent);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}

.masthead h1,
.detail h1 {
  max-width: 760px;
  margin: 0;
  color: var(--ink-strong);
  font-size: clamp(2.15rem, 4vw, 3.25rem);
  line-height: 0.98;
  letter-spacing: 0;
}

.detail h1 {
  font-size: clamp(2rem, 4vw, 3.8rem);
}

.lede {
  max-width: 580px;
  margin: 10px 0 0;
  color: var(--muted);
  font-size: 0.98rem;
}

.rss-link,
.rss-text-link,
.actions a,
.top-nav a,
.filter-button,
.command-search input,
.controls input,
.controls select {
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

.rss-link:hover,
.rss-text-link:hover,
.actions a:hover,
.top-nav a:hover,
.filter-button:hover {
  border-color: var(--line-strong);
  background: #f6faf9;
}

.site-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--muted);
  font-size: 0.86rem;
  white-space: nowrap;
}

.search-panel {
  min-width: 0;
  padding: 12px;
  border: 1px solid rgba(185, 199, 211, 0.78);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.88);
  box-shadow: 0 16px 42px rgba(31, 41, 51, 0.08);
  backdrop-filter: blur(14px);
}

.command-search {
  display: grid;
  gap: 6px;
}

.command-search span {
  color: var(--accent);
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0;
  text-transform: uppercase;
}

.command-search input {
  width: 100%;
  min-height: 44px;
  justify-content: start;
  padding: 10px 13px;
  border-color: #c8d5df;
  font: inherit;
  font-size: 0.98rem;
}

.command-search input:focus {
  outline: 3px solid rgba(15, 118, 110, 0.16);
  border-color: #6fa59e;
}

.search-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  align-items: center;
  margin-top: 9px;
  color: var(--muted);
  font-size: 0.78rem;
}

.search-meta strong {
  color: var(--ink-strong);
}

.search-meta .rss-link {
  min-height: 28px;
  margin-left: auto;
  padding: 4px 8px;
  font-size: 0.78rem;
}

.rss-text-link {
  min-height: 0;
  margin-left: auto;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--link);
  font-size: 0.78rem;
  font-weight: 650;
  text-decoration: underline;
  text-underline-offset: 0.18em;
}

.rss-text-link:hover {
  background: transparent;
  color: #084987;
}

.briefing {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(340px, 0.95fr);
  gap: 14px;
  align-items: stretch;
  padding: 18px 0 14px;
}

.spotlight {
  display: grid;
  grid-template-columns: minmax(180px, 0.52fr) minmax(0, 1fr);
  gap: 18px;
  min-height: 310px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: linear-gradient(135deg, #ffffff 0%, #ffffff 58%, #f7fbfa 100%);
  box-shadow: 0 18px 50px rgba(31, 41, 51, 0.08);
}

.spotlight-art {
  min-height: 100%;
  background: #e6edf2;
}

.spotlight-art img,
.spotlight-art .art-fallback {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.spotlight-copy {
  min-width: 0;
  padding: 24px 22px 20px 0;
  align-self: center;
}

.spotlight h2 {
  max-width: 680px;
  margin: 0 0 10px;
  color: var(--ink-strong);
  font-size: clamp(1.55rem, 3vw, 2.45rem);
  line-height: 1.04;
  letter-spacing: 0;
}

.spotlight h2 a {
  color: inherit;
  text-decoration: none;
}

.spotlight-meta {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 0.94rem;
}

.spotlight-signal {
  margin: 0 0 14px;
  color: var(--ink);
  font-size: 1.05rem;
  line-height: 1.45;
}

.radar-panel {
  display: grid;
  grid-template-rows: auto auto 1fr;
  gap: 10px;
  min-width: 0;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}

.metric-grid div,
.lab-meter,
.signal-queue {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.82);
}

.metric-grid div {
  padding: 11px 10px;
}

.metric-grid strong {
  display: block;
  color: var(--ink-strong);
  font-size: 1.35rem;
  line-height: 1;
}

.metric-grid span {
  color: var(--muted);
  font-size: 0.74rem;
  font-weight: 700;
  text-transform: uppercase;
}

.lab-meter,
.signal-queue {
  padding: 12px;
}

.panel-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
}

.meter-row {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr) 24px;
  gap: 8px;
  align-items: center;
  margin: 8px 0;
  font-size: 0.84rem;
}

.meter-row span {
  overflow: hidden;
  color: var(--ink);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meter-row div {
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: #edf2f5;
}

.meter-row i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--accent), var(--accent-3));
}

.meter-row b {
  color: var(--muted);
  font-size: 0.78rem;
  text-align: right;
}

.signal-queue {
  display: grid;
  align-content: start;
}

.signal-item {
  display: grid;
  gap: 2px;
  padding: 9px 0;
  border-top: 1px solid var(--line);
  color: inherit;
  text-decoration: none;
}

.signal-item:first-of-type {
  border-top: 0;
  padding-top: 0;
}

.signal-item span,
.signal-item em {
  color: var(--muted);
  font-size: 0.76rem;
  font-style: normal;
}

.signal-item strong {
  display: -webkit-box;
  overflow: hidden;
  color: var(--ink-strong);
  font-size: 0.9rem;
  line-height: 1.24;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.controls {
  position: sticky;
  top: 0;
  z-index: 5;
  display: grid;
  grid-template-columns: minmax(220px, 1.5fr) repeat(3, minmax(150px, 0.8fr));
  gap: 10px;
  align-items: end;
  margin: 4px 0 0;
  padding: 12px 0 9px;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  background: rgba(251, 252, 253, 0.94);
  backdrop-filter: blur(12px);
}

.controls label {
  display: grid;
  gap: 5px;
  min-width: 0;
}

.controls label span {
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
}

.controls input,
.controls select {
  width: 100%;
  min-height: 36px;
  justify-content: start;
  color: var(--ink);
  font: inherit;
}

.controls input {
  padding: 8px 11px;
}

.filters {
  display: grid;
  grid-template-columns: 122px 1fr;
  gap: 12px;
  align-items: center;
  margin-top: 6px;
  padding: 12px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}

.filter-label {
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}

.filter-buttons {
  display: flex;
  flex-wrap: nowrap;
  gap: 7px;
  overflow-x: auto;
  padding-bottom: 2px;
  scrollbar-width: none;
}

.filter-buttons::-webkit-scrollbar {
  display: none;
}

.filter-button {
  flex: 0 0 auto;
  cursor: pointer;
  color: var(--muted);
  font: inherit;
  min-height: 34px;
  padding: 6px 11px;
  border-radius: 999px;
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

.content-grid {
  display: grid;
  grid-template-columns: minmax(0, 710px) minmax(280px, 1fr);
  gap: 38px;
  align-items: start;
  padding-top: 14px;
}

.episode-list {
  display: grid;
  gap: 10px;
  padding: 0;
}

.pagination {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  margin-top: 14px;
  padding: 12px;
  border: 1px solid rgba(202, 214, 224, 0.98);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.82);
  box-shadow: 0 10px 28px rgba(31, 41, 51, 0.035);
}

.pagination[hidden] {
  display: none !important;
}

.pagination button {
  min-height: 34px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: var(--ink);
  cursor: pointer;
  font: inherit;
  font-size: 0.86rem;
}

.pagination button:hover:not(:disabled) {
  border-color: var(--line-strong);
  background: #f6faf9;
}

.pagination button:disabled {
  cursor: default;
  opacity: 0.48;
}

.page-buttons {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
  min-width: 0;
}

.page-buttons button[aria-current="page"] {
  border-color: #8ebbb4;
  background: #eef8f5;
  color: var(--ink-strong);
  font-weight: 760;
}

.page-gap {
  align-self: center;
  color: var(--muted);
  font-size: 0.78rem;
}

.pagination p {
  grid-column: 1 / -1;
  margin: 0;
  color: var(--muted);
  font-size: 0.78rem;
  text-align: center;
}

.episode-card {
  position: relative;
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 16px;
  align-items: center;
  min-height: 118px;
  padding: 15px 16px 15px 14px;
  background:
    linear-gradient(90deg, rgba(15, 118, 110, 0.035), transparent 26%),
    rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(202, 214, 224, 0.98);
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(31, 41, 51, 0.05), 0 10px 28px rgba(31, 41, 51, 0.025);
  color: inherit;
  text-decoration: none;
  cursor: pointer;
}

.episode-card::before {
  position: absolute;
  inset: 8px auto 8px -1px;
  width: 3px;
  border-radius: 999px;
  background: linear-gradient(180deg, var(--accent), var(--accent-3));
  content: "";
  opacity: 0.28;
}

.episode-card:hover {
  border-color: rgba(142, 187, 180, 0.9);
  background: #ffffff;
  box-shadow: 0 16px 36px rgba(31, 41, 51, 0.10);
}

.episode-card:hover::before {
  opacity: 1;
}

.episode-card[hidden] {
  display: none !important;
}

.art,
.detail-art {
  aspect-ratio: 1;
  width: 100%;
  overflow: hidden;
  border-radius: 6px;
  background: #e6edf2;
}

.episode-body {
  min-width: 0;
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
  gap: 4px 8px;
  color: var(--muted);
  font-size: 0.76rem;
}

.meta-row span:not(:last-child)::after {
  content: "·";
  color: #9aa6b2;
  margin-left: 8px;
}

.episode-card h2 {
  margin: 0;
  font-size: 1.07rem;
  line-height: 1.25;
  letter-spacing: 0;
  color: var(--ink-strong);
}

.episode-card:hover h2 {
  color: var(--link);
}

.source-line {
  display: block;
  overflow: hidden;
  max-height: 2.7em;
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 0.84rem;
  line-height: 1.35;
}

.source-line span:first-child {
  color: #586472;
  font-weight: 650;
}

.source-line span:not(:last-child)::after {
  content: "·";
  color: #9aa6b2;
  margin-left: 8px;
  margin-right: 8px;
}

.source-line a {
  color: var(--link);
  text-decoration: none;
}

.side-rail {
  position: sticky;
  top: 16px;
  display: grid;
  gap: 12px;
  min-width: 0;
}

.rail-card {
  border: 1px solid rgba(202, 214, 224, 0.98);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.84);
  box-shadow: 0 10px 28px rgba(31, 41, 51, 0.04);
}

.rss-card {
  padding: 14px;
  background:
    linear-gradient(135deg, rgba(15, 118, 110, 0.09), transparent 58%),
    rgba(255, 255, 255, 0.92);
}

.rss-card .eyebrow {
  margin-bottom: 7px;
  font-size: 0.68rem;
}

.rss-card a {
  color: var(--ink-strong);
  font-size: 0.98rem;
  font-weight: 760;
  line-height: 1.25;
  text-decoration: none;
}

.rss-card .follow-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--link);
  text-decoration: underline;
  text-underline-offset: 0.18em;
}

.rss-card .follow-link span {
  font-size: 0.9em;
  line-height: 1;
}

.rss-card a:hover {
  color: var(--link);
}

.rail-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px 10px;
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0;
  text-transform: uppercase;
}

.rail-list {
  display: grid;
}

.rail-item {
  display: grid;
  gap: 4px;
  padding: 11px 14px;
  border-top: 1px solid var(--line);
  color: inherit;
  text-decoration: none;
}

.rail-item strong {
  color: var(--ink-strong);
  font-size: 0.88rem;
  line-height: 1.25;
}

.rail-item span {
  color: var(--muted);
  font-size: 0.78rem;
}

.rail-item:hover {
  background: #f7fbfa;
}

.people,
.summary,
.signal {
  margin: 2px 0;
  color: var(--muted);
}

.people {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  font-size: 0.9rem;
  line-height: 1.4;
}

.people strong {
  color: var(--ink);
}

.signal {
  color: var(--ink);
  font-size: 0.92rem;
  line-height: 1.35;
}

.summary {
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
  font-size: 0.88rem;
  line-height: 1.38;
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

.tag-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin: 7px 0;
}

.tags,
.topic-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
}

.tags span,
.topic-tags span {
  border: 1px solid #cbd9d5;
  color: #0f5f58;
  background: #eef8f5;
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 0.82rem;
}

.topic-tags span {
  color: #48525d;
  border-color: #d8e0e7;
  background: #f5f7f9;
}

.actions,
.top-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.actions a {
  min-height: 34px;
  padding: 7px 10px;
  white-space: nowrap;
  font-size: 0.88rem;
}

.external-icon {
  line-height: 1;
  font-size: 0.95em;
}

.top-nav {
  justify-content: flex-start;
  padding: 4px 0 0;
  margin-bottom: 16px;
}

.top-nav a {
  min-height: 34px;
  padding: 7px 11px;
  color: var(--muted);
  background: transparent;
}

.top-nav a::before {
  content: "←";
  font-size: 1.05em;
  line-height: 1;
}

.detail {
  padding-bottom: 48px;
}

.detail-hero {
  display: grid;
  grid-template-columns: 240px 1fr;
  gap: 30px;
  align-items: start;
  margin-top: 0;
  padding: 22px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: linear-gradient(135deg, #ffffff 0%, #ffffff 62%, var(--panel-warm) 100%);
  box-shadow: 0 18px 50px rgba(31, 41, 51, 0.08);
}

.detail-hero .tags {
  margin: 14px 0 18px;
}

.detail-hero .actions {
  padding-top: 2px;
}

.detail-art {
  box-shadow: 0 14px 34px rgba(31, 41, 51, 0.14);
}

.brief-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 24px;
  align-items: start;
  margin-top: 18px;
  padding: 0;
}

.brief-main,
.brief-facts {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.brief-main {
  padding: 20px;
}

.brief-facts {
  padding: 18px;
}

.brief-main h2,
.brief-facts h2 {
  margin: 14px 0 10px;
  font-size: 1rem;
}

.brief-signal {
  margin: 0;
  color: var(--ink);
  font-size: 1.08rem;
  line-height: 1.45;
}

.brief-main ul,
.content-block ul {
  padding-left: 1.2rem;
}

.brief-main li,
.content-block li {
  margin: 0 0 0.48rem;
}

.brief-facts {
  border-left: 1px solid var(--line);
}

.brief-facts dl {
  display: grid;
  gap: 10px;
  margin: 0;
}

.brief-facts dt {
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
}

.brief-facts dd {
  margin: 2px 0 0;
}

.content-block {
  max-width: 920px;
  margin: 18px 0 0;
  padding: 22px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.content-block h2 {
  margin: 0 0 12px;
  color: var(--ink-strong);
  font-size: 1.28rem;
}

.transcript {
  color: #303b45;
  max-width: 900px;
  background: #ffffff;
}

.transcript-sentence {
  max-width: 76ch;
  margin: 0 0 0.72rem;
  font-size: 0.98rem;
  line-height: 1.62;
}

.empty {
  padding: 42px 0;
  color: var(--muted);
}

@media (max-width: 720px) {
  .shell { width: min(100% - 24px, 1120px); padding-top: 18px; }
  .masthead {
    grid-template-columns: 1fr;
    gap: 14px;
    padding-top: 8px;
  }
  .masthead h1 { font-size: 2.05rem; }
  .lede { font-size: 0.94rem; }
  .site-actions { margin-top: 14px; }
  .rss-link { margin-top: 18px; }
  .search-panel { padding: 10px; }
  .command-search { gap: 4px; }
  .command-search input {
    min-height: 38px;
    padding: 7px 10px;
    font-size: 0.9rem;
  }
  .search-meta {
    display: block;
    margin-top: 6px;
  }
  .search-meta span:not(:first-child) {
    display: none;
  }
  .briefing { grid-template-columns: 1fr; padding-top: 14px; }
  .spotlight {
    grid-template-columns: 86px minmax(0, 1fr);
    gap: 12px;
    min-height: 0;
  }
  .spotlight-art { min-height: 100%; }
  .spotlight-copy { padding: 14px 12px 14px 0; }
  .spotlight .eyebrow { margin-bottom: 6px; font-size: 0.68rem; }
  .spotlight h2 { font-size: 1.2rem; line-height: 1.08; }
  .spotlight-meta { font-size: 0.82rem; }
  .spotlight-signal {
    display: -webkit-box;
    overflow: hidden;
    font-size: 0.9rem;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
  }
  .spotlight .topic-tags { display: none; }
  .metric-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .metric-grid div { padding: 9px 8px; }
  .metric-grid strong { font-size: 1.05rem; }
  .metric-grid span { font-size: 0.62rem; }
  .signal-queue { display: none; }
  .meter-row { grid-template-columns: 88px minmax(0, 1fr) 22px; }
  .controls { grid-template-columns: 1fr 1fr; gap: 8px; top: 0; }
  .controls .search-box { grid-column: 1 / -1; }
  .filters {
    grid-template-columns: 1fr;
    gap: 8px;
    margin-top: 2px;
    padding: 10px 0;
  }
  .filter-buttons {
    flex-wrap: nowrap;
    overflow-x: auto;
    padding-bottom: 4px;
    scrollbar-width: none;
  }
  .filter-buttons::-webkit-scrollbar { display: none; }
  .filter-button { flex: 0 0 auto; }
  .content-grid {
    grid-template-columns: 1fr;
    gap: 14px;
    padding-top: 10px;
  }
  .side-rail {
    position: static;
    order: 2;
  }
  .episode-list { gap: 8px; padding-top: 0; }
  .pagination {
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-top: 10px;
    padding: 9px;
  }
  .page-buttons {
    grid-column: 1 / -1;
    grid-row: 1;
  }
  .pagination [data-page-prev] {
    grid-column: 1;
    grid-row: 2;
  }
  .pagination [data-page-next] {
    grid-column: 2;
    grid-row: 2;
  }
  .pagination p {
    grid-row: 3;
  }
  .episode-card {
    grid-template-columns: 60px minmax(0, 1fr);
    gap: 11px;
    min-height: 92px;
    padding: 10px 11px 10px 10px;
  }
  .art {
    width: 60px;
    height: 60px;
    justify-self: start;
  }
  .episode-card h2 { font-size: 0.95rem; }
  .source-line {
    max-height: 1.35em;
    font-size: 0.78rem;
  }
  .people { font-size: 0.86rem; }
  .signal { font-size: 0.88rem; }
  .summary {
    display: -webkit-box;
    -webkit-line-clamp: 1;
  }
  .tags { gap: 5px; margin: 7px 0; }
  .tags span { font-size: 0.78rem; padding: 2px 7px; }
  .actions {
    flex-wrap: wrap;
    gap: 6px;
  }
  .actions a { flex: 1 1 calc(50% - 6px); min-width: 0; padding: 7px 8px; font-size: 0.83rem; }
  .detail-hero .actions a { justify-content: center; }
  .top-nav { margin-bottom: 12px; }
  .detail-hero { grid-template-columns: 1fr; gap: 16px; padding: 14px; }
  .detail-art { max-width: 180px; }
  .brief-grid { grid-template-columns: 1fr; gap: 18px; }
  .brief-main,
  .brief-facts,
  .content-block { padding: 16px; }
  .brief-facts { border-left: 1px solid var(--line); }
  .transcript-sentence { font-size: 0.94rem; }
}
"""
