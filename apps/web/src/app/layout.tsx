import Link from "next/link";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata = { title: "AgentGit", description: "Git for agent runs" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header>
          <Link href="/" className="brand">
            AgentGit
          </Link>
          <span className="muted">Git for agent runs</span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
