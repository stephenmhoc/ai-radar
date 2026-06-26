import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";
import { chromium } from "@playwright/test";
import axe from "axe-core";

const root = resolve(process.cwd(), "public");
const outDir = resolve(process.cwd(), "var", "site-checks");
const requestedPort = Number(process.env.AI_RADAR_VERIFY_PORT || 0);
const host = "127.0.0.1";

const types = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".svg", "image/svg+xml"],
  [".xml", "application/xml; charset=utf-8"],
]);

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url || "/", `http://${host}:${port}`);
    let pathname = decodeURIComponent(url.pathname);
    if (pathname.endsWith("/")) pathname += "index.html";
    const requested = resolve(root, `.${normalize(pathname)}`);
    if (requested !== root && !requested.startsWith(root + sep)) {
      res.writeHead(403);
      res.end("Forbidden");
      return;
    }
    const body = await readFile(requested);
    res.writeHead(200, { "Content-Type": types.get(extname(requested)) || "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404);
    res.end("Not found");
  }
});

await new Promise((resolveListen) => server.listen(requestedPort, host, resolveListen));
await mkdir(outDir, { recursive: true });
const address = server.address();
const port = typeof address === "object" && address ? address.port : requestedPort;

const browser = await chromium.launch();
try {
  await checkViewport(browser, { name: "desktop", width: 1280, height: 900 });
  await checkViewport(browser, { name: "mobile", width: 390, height: 844 });
  console.log(`site verification passed: http://${host}:${port}/`);
} finally {
  await browser.close();
  await new Promise((resolveClose) => server.close(resolveClose));
}

async function checkViewport(browserInstance, viewport) {
  const page = await browserInstance.newPage({ viewport });
  await page.goto(`http://${host}:${port}/`, { waitUntil: "networkidle" });
  await runAxe(page, `${viewport.name} homepage`);
  await assertSocialPreview(page, `${viewport.name} homepage`, /\/assets\/social\/ai-radar-[a-f0-9]{10}\.(png|svg)$/);

  await expectCount(page, ".episode-card", 1, "episode cards");
  await expectCount(page, ".skip-link", 1, "skip links");
  await expectCount(page, ".search-panel", 1, "header search panels");
  await expectCount(page, ".rss-card .follow-link", 1, "follow-along RSS links");
  await expectCount(page, ".rss-card [data-copy-url]", 1, "RSS feed copy buttons");
  await expectCount(page, ".content-grid", 1, "content grids");
  await expectCount(page, ".side-rail", 1, "side rails");
  await expectCount(page, ".site-footer", 1, "site footers");
  await expectCount(page, "[data-pagination]", 1, "pagination controls");
  await expectCount(page, ".filter-button", 2, "filter buttons");
  await expectCount(page, "[data-search-input]", 1, "search controls");
  await expectCount(page, ".source-line", 1, "episode source lines");
  await expectCount(page, ".source-line .source-main", 1, "episode source titles");
  await expectCount(page, ".source-line .source-details", 1, "episode date and runtime metadata");
  await expectCount(page, ".rail-item .rail-source", 1, "newest episode metadata");
  const footerHref = await page.getByRole("link", { name: "MeriMeriMeri Software" }).getAttribute("href");
  if (footerHref !== "https://merimerimeri.com/") {
    throw new Error(`footer credit should link to MeriMeriMeri Software, got: ${footerHref}`);
  }
  if (await page.locator(".search-meta, [data-result-count], [data-result-label]").count()) {
    throw new Error("homepage should not show search result metadata under the search field");
  }
  await page.keyboard.press("Tab");
  const activeSkipHref = await page.evaluate(() => document.activeElement?.getAttribute("href"));
  if (activeSkipHref !== "#main-content") {
    throw new Error("first keyboard tab stop should be the skip link");
  }
  if (await page.locator('[data-filter="google"]').count()) {
    throw new Error("Google and Google DeepMind filters should be consolidated");
  }
  if (!(await page.locator('[data-filter="google-deepmind"]').count())) {
    throw new Error("missing consolidated Google DeepMind filter");
  }

  if (await page.locator(".briefing, .spotlight, .metric-grid, [data-topic-filter], [data-feed-filter], [data-sort]").count()) {
    throw new Error("index should not show the old briefing banner or dropdown filter stack");
  }

  if (await page.getByText("Summary and transcript ready").count()) {
    throw new Error("index still shows removed ready-status copy");
  }
  if (await page.getByText("Verified brief").count()) {
    throw new Error("index still shows verified-brief card status copy");
  }
  if (await page.getByText("briefings").count() || await page.getByText("Briefings").count()) {
    throw new Error("index should not use briefing language");
  }

  const firstCard = page.locator(".episode-card").first();
  const firstDetailHref = await firstCard.getAttribute("href");
  if (!firstDetailHref) throw new Error("episode cards should be whole-card links");
  if (await firstCard.locator(".tag-row, .summary, .signal, .actions").count()) {
    throw new Error("episode cards should not show tags, summaries, signals, or action rows");
  }
  const visibleInitialCards = await page.locator(".episode-card:visible").count();
  const cardBox = await page.locator(".episode-card:visible").first().boundingBox();
  if (!cardBox || cardBox.height < 90) {
    throw new Error("episode cards should retain enough visual surface to scan as distinct rows");
  }
  if (visibleInitialCards > 24) {
    throw new Error(`pagination should limit the initial visible card count, found ${visibleInitialCards}`);
  }
  if (await page.locator("[data-pagination]:visible").count()) {
    const nextButton = page.locator("[data-page-next]");
    const firstPageHref = firstDetailHref;
    await nextButton.click();
    const secondPageFirstHref = await page.locator(".episode-card:visible").first().getAttribute("href");
    if (!secondPageFirstHref || secondPageFirstHref === firstPageHref) {
      throw new Error("pagination did not advance the visible episode page");
    }
    if (!(await page.getByText(/Showing /).count())) {
      throw new Error("pagination status did not render");
    }
  }

  if (viewport.name === "desktop") {
    const grid = page.locator(".content-grid");
    const gridStyle = await grid.evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    const columns = gridStyle.trim().split(/\s+/);
    if (columns.length !== 2) {
      throw new Error(`desktop homepage should split the episode stream and side rail, got: ${gridStyle}`);
    }
    const listBox = await page.locator(".episode-list").boundingBox();
    if (!listBox || listBox.width > 760) {
      throw new Error(`episode stream should avoid over-wide rows, got width: ${listBox?.width}`);
    }
    if (!(await page.locator(".source-title:visible").count())) {
      throw new Error("desktop cards should keep episode names in source metadata");
    }
    if (!(await page.locator(".newest-card:visible").count())) {
      throw new Error("desktop should show newest episodes");
    }
  }

  const originalLink = firstCard.locator(".source-line .external-link");
  if (await originalLink.count()) {
    const target = await originalLink.getAttribute("target");
    const rel = await originalLink.getAttribute("rel");
    if (target !== "_blank" || !rel?.includes("noopener")) {
      throw new Error("original episode links must open safely in a new tab");
    }
  }

  if (viewport.name === "mobile") {
    const visibleCard = page.locator(".episode-card:visible").first();
    const style = await visibleCard.evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    const columnWidths = style.trim().match(/\d+(\.\d+)?px/g) || [];
    const firstColumn = Number.parseFloat(columnWidths[0] || "0");
    if (columnWidths.length !== 2 || firstColumn < 40 || firstColumn > 70) {
      throw new Error(`mobile card should use a compact two-column row, got: ${style}`);
    }
    const artBox = await visibleCard.locator(".art").boundingBox();
    if (!artBox) throw new Error("missing mobile episode art");
    if (artBox.width > 70 || Math.abs(artBox.width - artBox.height) > 2) {
      throw new Error(`mobile art should stay in a constrained square box, got: ${artBox.width}x${artBox.height}`);
    }
    if (await page.locator(".source-title:visible").count()) {
      throw new Error("mobile cards should hide episode names in source metadata");
    }
    if (await page.locator(".newest-card:visible").count()) {
      throw new Error("mobile should hide newest episodes");
    }
  }

  const searchInput = page.locator("[data-search-input]");
  await searchInput.fill("__no_matching_episode__");
  if (await page.locator(".episode-card:visible").count()) {
    throw new Error("search did not hide non-matching cards");
  }
  if (!(await page.getByText("No matching episodes").count())) {
    throw new Error("empty search state did not render");
  }
  await searchInput.fill("");

  const openAiFilter = page.locator('[data-filter="openai"]');
  if (await openAiFilter.count()) {
    await openAiFilter.click();
    const visibleCards = await page.locator(".episode-card:visible").count();
    const mismatched = await page.locator('.episode-card:visible:not([data-labs~="openai"])').count();
    if (visibleCards < 1) throw new Error("OpenAI filter hid every card");
    if (mismatched !== 0) throw new Error("OpenAI filter left non-OpenAI cards visible");
  }

  await page.screenshot({ path: join(outDir, `${viewport.name}.png`), fullPage: true });
  if (!firstDetailHref) throw new Error("missing first episode detail href");
  await page.goto(new URL(firstDetailHref, `http://${host}:${port}/`).toString(), { waitUntil: "networkidle" });
  await runAxe(page, `${viewport.name} detail page`);
  await assertSocialPreview(page, `${viewport.name} detail page`, /\/assets\/social\/episode-.+\.(png|svg)$/);
  const detailNavLinks = await page.locator(".top-nav a").count();
  if (detailNavLinks !== 1) {
    throw new Error(`detail page should have exactly one nav link, found ${detailNavLinks}`);
  }
  if (!(await page.getByRole("link", { name: "Back to episodes" }).count())) {
    throw new Error("detail page should keep navigation to a single back link");
  }
  if (await page.getByRole("link", { name: /Summary|Transcript|RSS feed|All episodes/ }).count()) {
    throw new Error("detail page should not show the old multi-button nav");
  }
  if (!(await page.getByRole("link", { name: /Go to episode|Open original episode|Original podcast/ }).count())) {
    throw new Error("detail page should link out to the original podcast or episode");
  }
  await expectCount(page, ".podcast-tools", 1, "podcast link tools");
  await expectCount(page, ".podcast-tools [data-copy-url]", 1, "podcast feed copy buttons");
  await expectCount(page, ".podcast-tools input[readonly]", 1, "copyable podcast feed URLs");
  await expectCount(page, ".brief-grid", 1, "episode brief grids");
  await expectCount(page, ".brief-signal", 1, "why it matters sections");
  await expectCount(page, ".brief-facts dd", 4, "episode fact values");
  await expectCount(page, ".transcript-sentence", 1, "transcript sentences");
  await page.screenshot({ path: join(outDir, `${viewport.name}-detail.png`), fullPage: true });
  await page.close();
}

async function expectCount(page, selector, minimum, label) {
  const count = await page.locator(selector).count();
  if (count < minimum) {
    throw new Error(`expected at least ${minimum} ${label}, found ${count}`);
  }
}

async function runAxe(page, label) {
  await page.addScriptTag({ content: axe.source });
  const results = await page.evaluate(async () => await window.axe.run(document));
  const violations = results.violations.filter((violation) => violation.impact !== "minor");
  if (!violations.length) return;

  const details = violations
    .map((violation) => {
      const targets = violation.nodes
        .slice(0, 4)
        .map((node) => node.target.join(" "))
        .join("; ");
      return `${violation.id} (${violation.impact}): ${violation.help} [${targets}]`;
    })
    .join("\n");
  throw new Error(`accessibility violations on ${label}:\n${details}`);
}

async function assertSocialPreview(page, label, imagePattern) {
  const image = await metaContent(page, 'meta[property="og:image"]');
  const secureImage = await metaContent(page, 'meta[property="og:image:secure_url"]');
  const width = await metaContent(page, 'meta[property="og:image:width"]');
  const height = await metaContent(page, 'meta[property="og:image:height"]');
  const imageAlt = await metaContent(page, 'meta[property="og:image:alt"]');
  const twitterImage = await metaContent(page, 'meta[name="twitter:image"]');
  const twitterAlt = await metaContent(page, 'meta[name="twitter:image:alt"]');

  if (!imagePattern.test(image)) {
    throw new Error(`${label} has unexpected preview image: ${image}`);
  }
  if (secureImage !== image || twitterImage !== image) {
    throw new Error(`${label} preview image URLs should match across OG and Twitter metadata`);
  }
  if (width !== "1200" || height !== "630") {
    throw new Error(`${label} preview image should declare 1200x630 dimensions`);
  }
  if (!imageAlt || twitterAlt !== imageAlt) {
    throw new Error(`${label} preview image should include matching alt text`);
  }
}

async function metaContent(page, selector) {
  const value = await page.locator(selector).first().getAttribute("content");
  if (!value) {
    throw new Error(`missing metadata: ${selector}`);
  }
  return value;
}
