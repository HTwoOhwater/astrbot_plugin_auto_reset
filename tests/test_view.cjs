/* Local browser acceptance test. Install playwright or set NODE_PATH to it.
 * BROWSER_EXECUTABLE optionally selects an installed Chromium/Edge binary.
 * Fixtures contain no server configuration, credentials or chat messages.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");

const viewRoot = path.resolve(__dirname, "../views/daily-memory");
const artifactDir = process.env.ROLLOVER_UI_ARTIFACT_DIR;
const fixture = {
  version: "2.1.0", revision: "initial", now: "2026-10-26T10:12:00+08:00", next_run: "2026-10-27T04:00:00+08:00", day: "2026-10-26", busy: false,
  config: {enabled: true, sessions: ["qq:GroupMessage:123456", "qq:GroupMessage:654321"], hour: 4, minute: 0},
  stats: {today_done: 1, pending_jobs: 1, paused_jobs: 0},
  dependencies: [{name: "AstrBot", version: "4.29.1", expected: "4.29.1", active: true}, {name: "陪伴插件", version: "6.7.0", expected: "6.7.0", active: true}, {name: "记忆插件", version: "1.10.5", expected: "1.10.5", active: true}],
  checks: [{session: "qq:GroupMessage:123456", ok: true, reason: ""}, {session: "qq:GroupMessage:654321", ok: true, reason: ""}],
  jobs: [{day: "2026-10-26", completed_day: "2026-10-26", session: "qq:GroupMessage:123456", phase: "done", summary_state: "complete", pending_count: 0, error: "", summary_error: ""}, {day: "2026-10-26", completed_day: "", session: "qq:GroupMessage:654321", phase: "waiting", summary_state: "pending", pending_count: 21, error: "session_busy_retry_later", summary_error: ""}]
};

async function main() {
  const server = http.createServer((req, res) => {
    const files = {"/": ["index.html", "text/html"], "/app.js": ["app.js", "application/javascript"], "/style.css": ["style.css", "text/css"]};
    const file = files[req.url];
    if (!file) {res.writeHead(404); res.end(); return;}
    res.writeHead(200, {"Content-Type": `${file[1]}; charset=utf-8`});
    res.end(fs.readFileSync(path.join(viewRoot, file[0])));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath: process.env.BROWSER_EXECUTABLE} : {})});
    const errors = [];
    const page = await browser.newPage({viewport: {width: 1360, height: 1100}});
    page.on("pageerror", error => errors.push(error.message));
    await page.addInitScript(data => {
      window.__fixture = data;
      window.__posts = [];
      window.AstrBotPluginView = {
        ready: async () => ({isDark: false}),
        apiGet: async endpoint => {if (endpoint !== "panel/status") throw Error("Unexpected endpoint"); return structuredClone(window.__fixture);},
        apiPost: async (endpoint, payload) => {
          window.__posts.push({endpoint, payload});
          if (endpoint === "panel/settings") {
            window.__fixture.config = payload.config;
            window.__fixture.revision = `saved-${window.__posts.length}`;
            return {message: "设置已保存，下次巡检使用新设置。"};
          }
          if (endpoint === "panel/action") return {message: "已排队，会话空闲后执行。"};
          throw Error("Unexpected endpoint");
        }
      };
    }, fixture);
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => document.querySelector("#check-badge").textContent === "检查通过");
    assert.equal(await page.locator("#today-count").textContent(), "1 / 2");
    assert.equal(await page.locator("#jobs tr").count(), 2);
    assert.equal(await page.evaluate(() => window.__posts.length), 0, "Loading must never mutate config or enqueue work");
    if (artifactDir) {
      fs.mkdirSync(artifactDir, {recursive: true});
      await page.screenshot({path: path.join(artifactDir, "daily-memory-light.png"), fullPage: true, animations: "disabled"});
      await page.evaluate(() => document.documentElement.dataset.theme = "dark");
      await page.screenshot({path: path.join(artifactDir, "daily-memory-dark.png"), fullPage: true, animations: "disabled"});
      await page.evaluate(() => document.documentElement.dataset.theme = "light");
    }
    await page.locator("#run-now").click();
    await page.locator("#confirmation button[value=cancel]").click();
    assert.equal(await page.evaluate(() => window.__posts.length), 0, "Cancel must not enqueue work");
    await page.locator("#run-now").click();
    await page.locator("#confirm-action").click();
    await page.waitForFunction(() => window.__posts.length === 1);
    assert.equal((await page.evaluate(() => window.__posts[0])).payload.confirmed, true);
    assert.equal((await page.evaluate(() => window.__posts[0])).payload.session, fixture.config.sessions[0]);
    await page.locator("#run-time").fill("05:15");
    assert.equal(await page.locator("#run-now").isDisabled(), true, "Unsaved scope/time edits disable actions");
    await page.locator("#save").click();
    await page.waitForFunction(() => document.querySelector("#dirty").textContent === "设置已同步");
    const saved = await page.evaluate(() => window.__posts.at(-1));
    assert.equal(saved.payload.config.hour, 5);
    assert.equal(saved.payload.config.minute, 15);
    assert.equal(saved.payload.revision, "initial");
    await page.locator("#run-time").fill("06:00");
    await page.locator("#refresh").click();
    await page.locator("#confirmation button[value=cancel]").click();
    assert.equal(await page.locator("#run-time").inputValue(), "06:00");
    await page.locator("#refresh").click();
    await page.locator("#confirm-action").click();
    await page.waitForFunction(() => document.querySelector("#run-time").value === "05:15");
    // Dependency/user-controlled strings are text, never executable markup.
    await page.evaluate(() => {window.__fixture.jobs[0].error = '<img src=x onerror="window.__xss=true">'; window.__fixture.busy = true;});
    await page.locator("#refresh").click();
    await page.waitForFunction(() => document.querySelector("#run-now").disabled);
    assert.equal(await page.locator("img").count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.__xss)), false);
    await page.evaluate(data => {window.__fixture = data;}, fixture);
    await page.locator("#refresh").click();
    await page.waitForFunction(() => document.querySelector("#run-time").value === "04:00");
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, "Mobile layout must not overflow the page");
    if (artifactDir) await page.screenshot({path: path.join(artifactDir, "daily-memory-mobile.png"), fullPage: true, animations: "disabled"});
    assert.deepEqual(errors, []);
    await page.close();
    const standalone = await browser.newPage();
    await standalone.goto(`http://127.0.0.1:${server.address().port}/`);
    await standalone.waitForFunction(() => document.querySelector("#notice").textContent.includes("请从 AstrBot 后台"));
    assert.equal(await standalone.locator("#save").isDisabled(), true);
    console.log("Browser acceptance passed: rendering, confirmation, save/reload, scope, XSS, mobile and missing bridge.");
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
