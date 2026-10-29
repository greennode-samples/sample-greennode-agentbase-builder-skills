'use client';
// Resolves the interface language in the browser (NEXT_PUBLIC_LOCALE, else navigator.languages), translates the shared chat
// words, sets <html lang> and provides the strings. Children render after that: the app is client-only behind sign-in, so
// the server renders no text that could mismatch.
import React, { createContext, useContext, useEffect, useState } from 'react';

import { applyChatText, type Locale, resolveLocale, STRINGS, type Strings } from './i18n';

const LocaleContext = createContext<Strings>(STRINGS.en);

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocale] = useState<Locale | null>(null);
  useEffect(() => {
    const resolved = resolveLocale(process.env.NEXT_PUBLIC_LOCALE, navigator.languages ?? [navigator.language]);
    applyChatText(resolved);
    document.documentElement.lang = resolved;
    setLocale(resolved);
  }, []);
  if (!locale) return null;
  return <LocaleContext.Provider value={STRINGS[locale]}>{children}</LocaleContext.Provider>;
}

export const useStrings = () => useContext(LocaleContext);
