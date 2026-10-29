import { describe, expect, it } from 'vitest';

import { linkify } from '../src/components/linkify';

const urls = (t: string) => linkify(t).filter((p) => p.url).map((p) => p.url);

describe('linkify', () => {
  it('links https only — never javascript:, data: or http:', () => {
    expect(urls('a javascript:alert(1) b data:text/html,x c http://x.test d')).toEqual([]);
    expect(urls('open https://x.test/a.')).toEqual(['https://x.test/a']);
  });
  it('trims markdown brackets / emphasis but keeps balanced parens and a trailing _ of the URL', () => {
    expect(urls('[link](https://x.test/b)')).toEqual(['https://x.test/b']);
    expect(urls('see https://en.wikipedia.org/wiki/Foo_(bar)')).toEqual(['https://en.wikipedia.org/wiki/Foo_(bar)']);
    expect(urls('**https://x.test/d**')).toEqual(['https://x.test/d']);
    expect(urls('https://x.test/?state=abc_')).toEqual(['https://x.test/?state=abc_']);
  });
  it('stops at quotes, angle brackets and backticks (no attribute / tag injection)', () => {
    expect(urls('https://x.test/a"onmouseover="x')).toEqual(['https://x.test/a']);
    expect(urls('<https://x.test/b><script>')).toEqual(['https://x.test/b']);
  });
  it('keeps the whole text', () => {
    const t = 'a https://x.test b';
    expect(linkify(t).map((p) => p.text).join('')).toBe(t);
  });
});
