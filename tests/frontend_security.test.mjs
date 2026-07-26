import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../frontend/app.js", import.meta.url), "utf8");
const html = await readFile(new URL("../frontend/index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../frontend/styles.css", import.meta.url), "utf8");
const vercel = JSON.parse(await readFile(new URL("../vercel.json", import.meta.url), "utf8"));
const notices = await readFile(new URL("../THIRD_PARTY_NOTICES.md", import.meta.url), "utf8");
const vercelIgnore = await readFile(new URL("../.vercelignore", import.meta.url), "utf8");
const sheetjs = await readFile(new URL("../frontend/vendor/xlsx.full.min.js", import.meta.url));

test("session identifiers never fall back to Math.random", () => {
  const secureIdBlock = app.slice(app.indexOf("function secureClientId"), app.indexOf("function browserSessionId"));
  assert.match(secureIdBlock, /crypto\?\.getRandomValues/);
  assert.doesNotMatch(secureIdBlock, /Math\.random/);
});

test("interactive blot canvas supports pointer and keyboard input", () => {
  assert.match(app, /addEventListener\("pointerdown"/);
  assert.match(app, /addEventListener\("keydown"/);
  assert.match(html + app, /blotCanvasInstructions/);
  assert.match(css, /touch-action:\s*none/);
});

test("production CSP is self-hosted and rejects inline styles", () => {
  const csp = vercel.headers[0].headers.find(header => header.key === "Content-Security-Policy")?.value || "";
  assert.match(csp, /script-src 'self';/);
  assert.match(csp, /style-src 'self';/);
  assert.doesNotMatch(csp, /unsafe-inline|cdn\.sheetjs|fonts\.googleapis/);
  assert.match(html, /\.\/vendor\/xlsx\.full\.min\.js/);
});

test("every /api route has a Vercel function wrapper", async () => {
  // On Vercel, api/<name>.py IS the route. A route registered in Flask with no wrapper falls
  // through the /:path* -> /frontend/:path* rewrite and 404s in production, while working
  // perfectly against the single local Flask process — so local clicking cannot catch it.
  // /channel-stats, /channel-pixels and /blot-fingerprints all shipped that way.
  const py = await readFile(new URL("../backend/app.py", import.meta.url), "utf8");
  const routes = [...py.matchAll(/@app\.route\("\/api\/([a-z0-9-]+)"/g)].map((match) => match[1]);
  assert.ok(routes.length > 5, "no /api routes found; this test would be vacuous");
  const wrappers = new Set(
    (await readdir(new URL("../api/", import.meta.url)))
      .filter((name) => name.endsWith(".py"))
      .map((name) => name.slice(0, -3)),
  );
  const missing = [...new Set(routes)].filter((route) => !wrappers.has(route));
  assert.deepEqual(missing, [], `no api/<name>.py wrapper for: ${missing.join(", ")}`);
});

test("deployment never ships a .env or local blot data", () => {
  // .gitignore governs git-connected deploys; .vercelignore governs `vercel --prod` from a
  // working tree, and the CLI's own defaults miss a plain `.env`. backend/.env is what the
  // README tells everyone to create, and it holds BLOB_READ_WRITE_TOKEN — full read/write/
  // delete on every session's objects. backend/ itself must keep shipping (api/*.py import
  // from it), so these rules are the only thing keeping the secret out.
  assert.match(vercelIgnore, /^\.env$/m);
  assert.match(vercelIgnore, /^\*\*\/\.env$/m);
  assert.match(vercelIgnore, /^!\*\*\/\.env\.example$/m);
  assert.match(vercelIgnore, /^test_blots\/$/m);
  assert.match(vercelIgnore, /^\.vercel\/$/m);      // projectId / orgId
  assert.match(vercelIgnore, /^\*\*\/\.DS_Store$/m);
  assert.doesNotMatch(vercelIgnore, /^backend\/$/m, "excluding backend/ would 404 the whole API");
  // These three are runtime inputs, not clutter: Vercel installs Python deps from
  // requirements.txt and Node deps from package.json + lock. Excluding any of them would
  // break the deployment rather than tidy it.
  ["requirements.txt", "package.json", "package-lock.json"].forEach((needed) => {
    assert.doesNotMatch(vercelIgnore, new RegExp(`^${needed.replace(".", "\\.")}$`, "m"),
      `${needed} is needed by the install step`);
  });
});

test("the vendored SheetJS bundle matches its pinned hash", () => {
  // The pin used to live only as prose in THIRD_PARTY_NOTICES.md, which meant a modified
  // bundle shipped silently — "pinned" amounted to "documented once". Three things have to
  // agree: the file on disk, the notice, and the SRI attribute the browser enforces.
  const actual = createHash("sha384").update(sheetjs).digest("base64");
  const documented = notices.match(/sha384-([A-Za-z0-9+/=]+)/)?.[1];
  assert.ok(documented, "THIRD_PARTY_NOTICES.md no longer records a SHA-384 pin");
  assert.equal(actual, documented, "frontend/vendor/xlsx.full.min.js does not match its documented pin");

  const attribute = html.match(/integrity="sha384-([A-Za-z0-9+/=]+)"/)?.[1];
  assert.ok(attribute, "the SheetJS script tag lost its integrity attribute");
  assert.equal(attribute, actual, "the integrity attribute does not match the shipped bundle");
});

test("spreadsheet exports neutralize formula-like cells", () => {
  assert.match(app, /\^\\s\*\[=\+\\-@\\t\\r\]/);
  assert.match(app, /function csvCell/);
  assert.match(app, /function workbookCell/);
});

test("local ZIP validation uses the backend direct-upload limit", () => {
  assert.match(app, /deploymentConfig\.maxDirectUploadBytes/);
  assert.match(app, /deploymentConfig\.maxZipUploadBytes/);
});
