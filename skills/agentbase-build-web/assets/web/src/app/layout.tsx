import './globals.css';

import type { Metadata } from 'next';
import { headers } from 'next/headers';
import type React from 'react';

export const metadata: Metadata = {
  title: '__PROJECT_NAME__',
  description: 'Chat with the __PROJECT_NAME__ agent',
  referrer: 'no-referrer',
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Reading the request headers renders every page per request — required for the CSP nonce set by src/proxy.ts
  await headers();
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
