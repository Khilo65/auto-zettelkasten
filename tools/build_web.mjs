import { build } from "esbuild";
import { readFile, writeFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const out = path.join(root, "src/auto_zettelkasten/web");
const results = [];
for (const [entry, output, globalName] of [
  ["web_entry.js", "workspace-vendor.js", "WorkspaceKit"],
  ["workspace_index_worker.js", "workspace-index-worker.js"],
  ["workspace_graph_worker.js", "workspace-graph-worker.js"],
]) results.push(await build({
  entryPoints: [path.join(root, "tools", entry)],
  outfile: path.join(out, output),
  bundle: true,
  minify: true,
  format: "iife",
  ...(globalName ? { globalName } : {}),
  target: ["safari15", "chrome100"],
  legalComments: "inline",
  metafile: true,
}));

const packages = new Map();
for (const input of new Set(results.flatMap((result) => Object.keys(result.metafile.inputs)))) {
  if (!input.includes("node_modules/")) continue;
  let directory = path.dirname(path.resolve(root, input));
  while (directory !== root) {
    try {
      const manifest = JSON.parse(await readFile(path.join(directory, "package.json"), "utf8"));
      if (manifest.name && manifest.version) {
        packages.set(`${manifest.name}@${manifest.version}`, { directory, manifest });
        break;
      }
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    directory = path.dirname(directory);
  }
}
const notices = ["Third-party components bundled in the Markdown workspace.\n"];
for (const [name, { directory, manifest }] of [...packages].sort(([a], [b]) => a.localeCompare(b, "en"))) {
  const files = (await readdir(directory)).filter((file) => /^(licen[cs]e|copying|notice)([._-]|$)/i.test(file));
  if (!files.length) throw new Error(`Missing license text for ${name}`);
  notices.push(`\n${name} (${manifest.license || "see license"})\n${"=".repeat(72)}\n`);
  for (const file of files.sort()) notices.push(await readFile(path.join(directory, file), "utf8"), "\n");
}
await writeFile(path.join(out, "workspace-licenses.txt"), notices.join(""));
console.log(`Built workspace and workers with notices for ${packages.size} packages.`);
