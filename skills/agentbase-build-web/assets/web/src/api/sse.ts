/**
 * Incremental SSE parser (WHATWG event-stream framing): LF, CRLF or CR line endings (a CRLF split across two
 * chunks included), an event ends at a blank line, several `data:` lines are joined with "\n", `:` comment /
 * keep-alive lines and other fields are ignored. `end()` flushes a last event that lacks the closing blank line
 * (some proxies strip it) — otherwise the final `done`/`interrupt` would be lost.
 * Pure TypeScript (no React Native imports) — unit-testable in plain Node.
 */
export function createSseParser(onData: (data: string) => void) {
  let buffer = '';
  let data: string[] = [];
  const dispatch = () => {
    if (data.length) onData(data.join('\n'));
    data = [];
  };
  const line = (l: string) => {
    if (l === '') return dispatch();
    if (l.startsWith(':')) return;
    const colon = l.indexOf(':');
    const field = colon < 0 ? l : l.slice(0, colon);
    const value = colon < 0 ? '' : l.slice(colon + 1).replace(/^ /, '');
    if (field === 'data') data.push(value);
  };
  return {
    push(chunk: string) {
      buffer += chunk;
      const held = buffer.endsWith('\r'); // maybe the first half of a CRLF: decide when the next chunk arrives
      const lines = (held ? buffer.slice(0, -1) : buffer).split(/\r\n|\r|\n/);
      buffer = lines.pop()! + (held ? '\r' : '');
      lines.forEach(line);
    },
    end() {
      if (buffer) line(buffer.replace(/\r$/, ''));
      buffer = '';
      dispatch();
    },
  };
}
