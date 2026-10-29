import { describe, expect, it } from 'vitest';

import { createSseParser } from '../src/api/sse';

const parse = (chunks: string[]) => {
  const out: string[] = [];
  const p = createSseParser((d) => out.push(d));
  chunks.forEach((c) => p.push(c));
  p.end();
  return out;
};

describe('createSseParser', () => {
  it('handles LF, CRLF and CR line endings', () => {
    expect(parse(['data: a\n\n', 'data: b\r\n\r\n', 'data: c\r\r'])).toEqual(['a', 'b', 'c']);
  });
  it('joins a CRLF split across chunks', () => {
    expect(parse(['data: a\r', '\n\r', '\ndata: b\n\n'])).toEqual(['a', 'b']);
  });
  it('joins multi-line data with \\n and ignores comments / other fields', () => {
    expect(parse([': keep-alive\n', 'event: x\nid: 1\ndata: l1\ndata: l2\n\n'])).toEqual(['l1\nl2']);
  });
  it('flushes the last event without a closing blank line at EOF', () => {
    expect(parse(['data: {"event":"done"}'])).toEqual(['{"event":"done"}']);
  });
  it('splits an event across many small chunks', () => {
    const frame = 'data: {"event":"token","data":"héllo"}\n\n';
    expect(parse(frame.split(''))).toEqual(['{"event":"token","data":"héllo"}']);
  });
});
