// What a screen reader is told about the conversation — and when. The message list is NOT a live region: a streamed
// answer changes on every token, and announcing each change makes it unusable. This text goes to one polite
// `role="status"` region: empty while an answer streams, then the finished answer once (first MAX characters), or that
// an approval is needed.
import type { ChatState } from '../chat/chatState';
import type { Strings } from '../lib/i18n';

const MAX = 600;

export function announcement(s: ChatState, t: Strings): string {
  if (s.pending) return t.approvalAnnouncement;
  const last = s.messages[s.messages.length - 1];
  if (!last || last.role !== 'assistant' || last.pending) return '';
  const text = last.text.trim();
  if (!text) return '';
  return `${t.agent}: ${text.length > MAX ? `${text.slice(0, MAX)}…` : text}`;
}
