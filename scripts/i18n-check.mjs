// Checks the interface translations in web/y/lang/.
//   node scripts/i18n-check.mjs          report
//   node scripts/i18n-check.mjs --strict exit 1 when something is missing
// Keys are the original Korean text. en.js is the reference: every language should
// have the same keys, and every literal $t("...") in the code should exist in en.js.
import { readdirSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const langDir = join(root, "web/y/lang");
const load = async (f) => (await import(pathToFileURL(join(langDir, f)).href)).default;

const en = await load("en.js");
const enKeys = new Set(Object.keys(en));
let problems = 0;
const warn = (msg) => { problems++; console.log((process.env.GITHUB_ACTIONS ? "::warning::" : "  ") + msg); };

// 1. keys used in the code
const used = new Set();
for (const f of readdirSync(join(root, "web/y")).filter((f) => f.endsWith(".js"))) {
  const src = readFileSync(join(root, "web/y", f), "utf8");
  for (const m of src.matchAll(/\$t\(\s*"((?:[^"\\]|\\.)*)"/g)) used.add(JSON.parse(`"${m[1]}"`));
  for (const m of src.matchAll(/\$t\(\s*'((?:[^'\\]|\\.)*)'/g)) used.add(m[1].replace(/\\'/g, "'"));
}
const missingEn = [...used].filter((k) => !enKeys.has(k));
console.log(`code: ${used.size} literal keys, ${missingEn.length} missing from en.js`);
for (const k of missingEn) warn(`en.js is missing ${JSON.stringify(k)}`);

// 2. every language against en.js
const placeholders = (s) => (String(s).match(/\{\d\}/g) || []).sort().join();
for (const f of readdirSync(langDir).filter((f) => f.endsWith(".js") && f !== "en.js").sort()) {
  const d = await load(f);
  const keys = new Set(Object.keys(d));
  const missing = [...enKeys].filter((k) => !keys.has(k));
  const extra = [...keys].filter((k) => !enKeys.has(k));
  const mismatch = [...keys].filter((k) => enKeys.has(k) && placeholders(en[k]) !== placeholders(d[k]));
  console.log(`${f.padEnd(6)} ${keys.size} keys, ${missing.length} missing, ${extra.length} unused, ${mismatch.length} placeholder mismatches`);
  for (const k of missing) warn(`${f} is missing ${JSON.stringify(k)}`);
  for (const k of mismatch) warn(`${f} placeholders differ for ${JSON.stringify(k)}`);
}
if (process.argv.includes("--strict") && problems) process.exit(1);
