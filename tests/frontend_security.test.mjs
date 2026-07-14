import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../frontend/app.js", import.meta.url), "utf8");
const html = await readFile(new URL("../frontend/index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../frontend/styles.css", import.meta.url), "utf8");
const vercel = JSON.parse(await readFile(new URL("../vercel.json", import.meta.url), "utf8"));

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

test("spreadsheet exports neutralize formula-like cells", () => {
  assert.match(app, /\^\\s\*\[=\+\\-@\\t\\r\]/);
  assert.match(app, /function csvCell/);
  assert.match(app, /function workbookCell/);
});

test("local ZIP validation uses the backend direct-upload limit", () => {
  assert.match(app, /deploymentConfig\.maxDirectUploadBytes/);
  assert.match(app, /deploymentConfig\.maxZipUploadBytes/);
});
