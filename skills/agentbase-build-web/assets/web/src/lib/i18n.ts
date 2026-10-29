// Interface language of the web app: English and Vietnamese. NEXT_PUBLIC_LOCALE = auto | en | vi (auto: the browser's
// languages). The words of the SHARED chat logic (src/chat/chatState.ts, identical in the Expo app) are English constants
// there; applyChatText() translates them in place — add a language by adding a dictionary here, nothing else changes.
import { TEXT, TOOL_STATUS } from '../chat/chatState';

export type Locale = 'en' | 'vi';
export const LOCALES: readonly Locale[] = ['en', 'vi'];

const en = {
  loading: 'Loading…',
  signIn: 'Sign in',
  newConversation: 'New',
  signOut: 'Sign out',
  conversation: 'Conversation',
  you: 'You',
  agent: 'Agent',
  toolsUsed: 'Tools used',
  goodAnswer: 'Good answer',
  badAnswer: 'Bad answer',
  retry: 'Retry',
  startNew: 'Start new conversation',
  dismiss: 'Dismiss',
  message: 'Message',
  answerApprovalFirst: 'Answer the approval above first',
  send: 'Send',
  stop: 'Stop',
  approvalRegion: 'Approval needed',
  approvalTitle: 'Your confirmation is needed',
  approve: 'Approve',
  edit: 'Edit',
  reject: 'Reject',
  decisionFor: (tool: string) => `Decision for ${tool}`,
  argumentsFor: (tool: string) => `Arguments for ${tool} (JSON)`,
  reasonFor: (tool: string) => `Reason for rejecting ${tool}`,
  reasonPlaceholder: 'Reason for rejecting (optional)',
  submitDecision: 'Submit decision',
  approvalAnnouncement: 'Your confirmation is needed before the agent continues.',
  serverUnreachable: (why: string) => `The app server is unreachable or misconfigured (${why}) — see its log.`,
  authErrors: {
    idp_error: 'The sign-in provider refused the request (check the redirect URI registered at the IdP).',
    idp_unavailable: 'The sign-in provider is unreachable — try again.',
    state: 'The sign-in took too long or was started in another tab — try again.',
    login_failed: 'Sign-in failed — try again.',
  } as Record<string, string>,
};
export type Strings = typeof en;

const vi: Strings = {
  loading: 'Đang tải…',
  signIn: 'Đăng nhập',
  newConversation: 'Mới',
  signOut: 'Đăng xuất',
  conversation: 'Cuộc trò chuyện',
  you: 'Bạn',
  agent: 'Trợ lý',
  toolsUsed: 'Công cụ đã dùng',
  goodAnswer: 'Câu trả lời tốt',
  badAnswer: 'Câu trả lời chưa tốt',
  retry: 'Thử lại',
  startNew: 'Bắt đầu cuộc trò chuyện mới',
  dismiss: 'Đóng',
  message: 'Tin nhắn',
  answerApprovalFirst: 'Hãy trả lời yêu cầu xác nhận ở trên trước',
  send: 'Gửi',
  stop: 'Dừng',
  approvalRegion: 'Cần xác nhận',
  approvalTitle: 'Cần bạn xác nhận',
  approve: 'Đồng ý',
  edit: 'Sửa',
  reject: 'Từ chối',
  decisionFor: (tool) => `Quyết định cho ${tool}`,
  argumentsFor: (tool) => `Tham số của ${tool} (JSON)`,
  reasonFor: (tool) => `Lý do từ chối ${tool}`,
  reasonPlaceholder: 'Lý do từ chối (không bắt buộc)',
  submitDecision: 'Gửi quyết định',
  approvalAnnouncement: 'Cần bạn xác nhận trước khi trợ lý tiếp tục.',
  serverUnreachable: (why) => `Máy chủ của ứng dụng không truy cập được hoặc cấu hình sai (${why}) — xem log của nó.`,
  authErrors: {
    idp_error: 'Nhà cung cấp đăng nhập từ chối yêu cầu (kiểm tra redirect URI đã đăng ký ở IdP).',
    idp_unavailable: 'Không kết nối được nhà cung cấp đăng nhập — hãy thử lại.',
    state: 'Phiên đăng nhập quá lâu hoặc được mở ở tab khác — hãy thử lại.',
    login_failed: 'Đăng nhập thất bại — hãy thử lại.',
  },
};

export const STRINGS: Record<Locale, Strings> = { en, vi };

/** The words of the shared chat logic (chatState.ts TEXT / TOOL_STATUS) per language; English = the originals. */
const viChat = {
  text: {
    waiting: 'Đang chờ bạn xác nhận…',
    approvalGone:
      'ℹ️ Yêu cầu xác nhận này không còn chờ nữa — nó đã được xử lý (có thể bởi một lần thử trước mà phản hồi chưa về) ' +
      'hoặc đã hết hạn. Không có gì được chạy lại. Hãy gửi tin nhắn để tiếp tục.',
    approvalResynced: 'ℹ️ Trợ lý đang chờ yêu cầu xác nhận bên dưới.',
    supersededNote: 'Yêu cầu trước đã được xử lý — đây là yêu cầu xác nhận mà trợ lý đang chờ.',
    chatBlocked: '⏸ Trợ lý đang chờ bạn xác nhận bên dưới — tin nhắn của bạn chưa được gửi. Hãy gửi lại sau đó.',
    stuck:
      '⚠️ Cuộc trò chuyện này đang chờ một yêu cầu xác nhận không thể hiển thị ở đây nên không tiếp tục được. ' +
      'Hãy bắt đầu cuộc trò chuyện mới.',
    stuckNotice: 'Cuộc trò chuyện này không thể tiếp tục.',
    resumeRetryNote:
      'Lần thử trước có thể đã hoàn tất trên máy chủ. Gửi lại là an toàn: nếu đã chạy, bạn sẽ được báo và không có gì chạy hai lần.',
    sessionExpired: '⚠️ Phiên đăng nhập đã hết hạn — hãy đăng nhập lại để tiếp tục.',
    closed: 'Kết nối đã đóng trước khi câu trả lời hoàn tất.',
    stopped: '⏹ Đã dừng.',
    failedNotice: 'Không thể hoàn tất tin nhắn này.',
  },
  status: { thinking: 'Đang suy nghĩ…', tool: 'Đang dùng một công cụ…', checking: 'Đang kiểm tra câu trả lời…' },
  tools: {
    search_knowledge: 'Đang tìm trong cơ sở kiến thức…',
    remember: 'Đang lưu vào bộ nhớ…',
    recall_memory: 'Đang xem lại các ghi chú đã lưu…',
    get_current_time: 'Đang xem giờ…',
  },
};

const original = { text: { ...TEXT, status: { ...TEXT.status } }, tools: { ...TOOL_STATUS } };

export function applyChatText(locale: Locale): void {
  if (locale === 'vi') {
    Object.assign(TEXT, viChat.text);
    TEXT.status = { ...viChat.status };
    Object.assign(TOOL_STATUS, viChat.tools);
  } else {
    Object.assign(TEXT, original.text);
    TEXT.status = { ...original.text.status };
    Object.assign(TOOL_STATUS, original.tools);
  }
}

/** `setting` is NEXT_PUBLIC_LOCALE; `languages` the browser's (navigator.languages), most preferred first. */
export function resolveLocale(setting: string | undefined, languages: readonly string[] = []): Locale {
  const wanted = (setting ?? 'auto').trim().toLowerCase();
  if ((LOCALES as readonly string[]).includes(wanted)) return wanted as Locale;
  for (const lang of languages) {
    const base = lang.toLowerCase().split('-')[0] ?? '';
    if ((LOCALES as readonly string[]).includes(base)) return base as Locale;
  }
  return 'en';
}
