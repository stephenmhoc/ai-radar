import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";
import { chromium } from "@playwright/test";

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

  await expectCount(page, ".episode-card", 1, "episode cards");
  await expectCount(page, ".briefing", 1, "briefing sections");
  await expectCount(page, ".spotlight", 1, "spotlight lead cards");
  await expectCount(page, ".metric-grid div", 4, "coverage metrics");
  await expectCount(page, ".meter-row", 1, "lab meter rows");
  await expectCount(page, ".filter-button", 2, "filter buttons");
  await expectCount(page, "[data-search-input]", 1, "search controls");
  await expectCount(page, "[data-topic-filter]", 1, "topic filters");
  await expectCount(page, "[data-feed-filter]", 1, "podcast filters");
  await expectCount(page, ".signal", 1, "episode relevance signals");
  await expectCount(page, ".external-icon", 1, "external link icons");
  await expectCount(page, ".actions", 1, "action rows");

  if (await page.getByText("Summary and transcript ready").count()) {
    throw new Error("index still shows removed ready-status copy");
  }

  const firstCard = page.locator(".episode-card").first();
  const firstDetailHref = await firstCard.getByRole("link", { name: "Episode details" }).getAttribute("href");
  const buttonsBox = await firstCard.locator(".actions").boundingBox();
  const detailBox = await firstCard.getByRole("link", { name: "Episode details" }).boundingBox();
  if (!buttonsBox || !detailBox) throw new Error("missing episode action button layout");
  if (detailBox.y < buttonsBox.y - 1 || detailBox.y > buttonsBox.y + buttonsBox.height + 1) {
    throw new Error("episode details button is outside the action row");
  }

  const originalLink = firstCard.getByRole("link", { name: /Original episode/ }).first();
  if (await originalLink.count()) {
    const target = await originalLink.getAttribute("target");
    const rel = await originalLink.getAttribute("rel");
    if (target !== "_blank" || !rel?.includes("noopener")) {
      throw new Error("original episode links must open safely in a new tab");
    }
  }

  if (viewport.name === "mobile") {
    const style = await firstCard.evaluate((element) => getComputedStyle(element).gridTemplateColumns);
    const columns = style.trim().split(/\s+/);
    if (columns.length !== 2) {
      throw new Error(`mobile card should use a compact two-column row, got: ${style}`);
    }
    const artBox = await firstCard.locator(".art").boundingBox();
    if (!artBox) throw new Error("missing mobile episode art");
    if (artBox.width > 70 || Math.abs(artBox.width - artBox.height) > 2) {
      throw new Error(`mobile art should stay in a constrained square box, got: ${artBox.width}x${artBox.height}`);
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
    const allCards = await page.locator(".episode-card:visible").count();
    await openAiFilter.click();
    const visibleCards = await page.locator(".episode-card:visible").count();
    const mismatched = await page.locator('.episode-card:visible:not([data-labs~="openai"])').count();
    if (visibleCards < 1) throw new Error("OpenAI filter hid every card");
    if (visibleCards >= allCards) throw new Error("OpenAI filter did not reduce the visible card count");
    if (mismatched !== 0) throw new Error("OpenAI filter left non-OpenAI cards visible");
  }

  await page.screenshot({ path: join(outDir, `${viewport.name}.png`), fullPage: true });
  if (!firstDetailHref) throw new Error("missing first episode detail href");
  await page.goto(new URL(firstDetailHref, `http://${host}:${port}/`).toString(), { waitUntil: "networkidle" });
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
