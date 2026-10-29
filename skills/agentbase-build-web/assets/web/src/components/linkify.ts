// Finds the https links in model output. Pure TypeScript (no React Native imports) — unit-testable in plain Node.
// Only https is linked (e.g. the AUTHORIZATION_REQUIRED consent link from AgentBase Identity) — never javascript: or
// custom schemes coming from model output.

export type Part = { text: string; url?: string };

// Candidate: everything up to whitespace, quotes, angle brackets or a backtick (inline code `https://x?a=1`)
const CANDIDATE = /https:\/\/[^\s<>"'`]+/gi;
const TRAILING_PUNCT = /[.,;:!?]$/;
const EMPHASIS = ['***', '**', '*', '___', '__', '_', '~~'];

const count = (s: string, ch: string) => s.split(ch).length - 1;

/**
 * Trims what is not part of the URL: trailing sentence punctuation ("open https://x.test/a."), a closing bracket
 * without its opening one in the URL ("[link](https://x.test/b)", "(https://x.test/e)" — but Wikipedia's
 * "Foo_(bar)" keeps its ")"), and markdown emphasis only when the SAME marker opens right before the URL
 * ("**https://x.test/d**") — a trailing "_" that belongs to the URL ("?state=abc_") is kept.
 */
function trimUrl(url: string, before: string): string {
  for (;;) {
    const prev = url;
    if (TRAILING_PUNCT.test(url)) url = url.slice(0, -1);
    for (const [open, close] of [['(', ')'], ['[', ']'], ['{', '}']]) {
      if (url.endsWith(close) && count(url, close) > count(url, open)) url = url.slice(0, -1);
    }
    for (const m of EMPHASIS) {
      if (url.endsWith(m) && before.endsWith(m) && url.length > 'https://'.length + m.length) {
        url = url.slice(0, -m.length);
        break;
      }
    }
    if (url === prev) return url;
  }
}

export function linkify(text: string): Part[] {
  const parts: Part[] = [];
  let last = 0;
  for (const match of text.matchAll(CANDIDATE)) {
    const start = match.index ?? 0;
    const url = trimUrl(match[0], text.slice(Math.max(0, start - 3), start));
    if (url.length <= 'https://'.length) continue;
    if (start > last) parts.push({ text: text.slice(last, start) });
    parts.push({ text: url, url });
    last = start + url.length;
  }
  if (last < text.length) parts.push({ text: text.slice(last) });
  return parts;
}
