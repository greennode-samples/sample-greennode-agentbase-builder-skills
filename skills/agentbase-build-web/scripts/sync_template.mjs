#!/usr/bin/env node
// Keeps the web app's TEMPLATE files in step with agentbase-build-web, without a git history and without clobbering
// your edits. Called by setup_web.sh; run it through `setup_web.sh … --update`.
//
//   node sync_template.mjs <template-dir> <web-dir> <project-name> record|update
//
// <web-dir>/.agentbase-web.json (commit it) maps every template file to the SHA-256 of the content setup wrote:
//   record   after setup: store the hash of each template file that is still exactly what the template wrote
//            (a file you kept or edited is not recorded — it counts as yours)
//   update   per template file, against the current template (placeholders replaced):
//              missing                       → added
//              equal to the new template     → nothing to do
//              equal to what setup wrote     → you never edited it: overwritten with the new template
//              anything else (you edited it) → left alone; the new version is written next to it as
//                                              <file>.agentbase-new for you to merge, and listed
//            Files the template dropped are listed, never deleted. Exit 1 when something needs a merge.
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';

const [tplDir, webDir, name, mode] = process.argv.slice(2);
if (!tplDir || !webDir || !name || !['record', 'update'].includes(mode ?? '')) {
  console.error('usage: sync_template.mjs <template-dir> <web-dir> <project-name> record|update');
  process.exit(2);
}
const MANIFEST = join(webDir, '.agentbase-web.json');
const sha = (text) => createHash('sha256').update(text).digest('hex');

function* walk(dir) {
  for (const entry of readdirSync(dir).sort()) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) yield* walk(path);
    else yield path;
  }
}

const template = new Map(); // relative path → content with the project name filled in
for (const path of walk(tplDir)) {
  template.set(relative(tplDir, path).split('\\').join('/'), readFileSync(path, 'utf8').split('__PROJECT_NAME__').join(name));
}
const recorded = existsSync(MANIFEST) ? (JSON.parse(readFileSync(MANIFEST, 'utf8')).files ?? {}) : {};
const next = { ...recorded };
const report = { added: [], updated: [], merge: [], dropped: [] };

for (const [rel, content] of template) {
  const target = join(webDir, rel);
  const exists = existsSync(target);
  const current = exists ? readFileSync(target, 'utf8') : null;
  if (mode === 'record') {
    if (current === content) next[rel] = sha(content);
    else delete next[rel];
    continue;
  }
  if (!exists) {
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, content);
    report.added.push(rel);
    next[rel] = sha(content);
  } else if (current === content) {
    next[rel] = sha(content);
  } else if (recorded[rel] && sha(current) === recorded[rel]) {
    writeFileSync(target, content);
    report.updated.push(rel);
    next[rel] = sha(content);
  } else {
    writeFileSync(`${target}.agentbase-new`, content);
    report.merge.push(rel);
    delete next[rel];
  }
}
for (const rel of Object.keys(recorded)) if (!template.has(rel)) report.dropped.push(rel);

writeFileSync(MANIFEST, JSON.stringify({ files: Object.fromEntries(Object.entries(next).sort()) }, null, 2) + '\n');
if (mode === 'update') {
  const show = (title, list) => list.length && console.log(`${title}:\n${list.map((f) => `  ${f}`).join('\n')}`);
  show('Added (new in the template)', report.added);
  show('Updated (you had not edited them)', report.updated);
  show('You edited these — merge the .agentbase-new file next to each, then delete it', report.merge);
  show('No longer in the template (kept; delete them if you do not use them)', report.dropped);
  if (!Object.values(report).some((l) => l.length)) console.log('The template files are up to date.');
  process.exit(report.merge.length ? 1 : 0);
}
