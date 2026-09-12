import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "LexTrace — Legal precedent intelligence",
  description: "Evidence-grounded legal research over U.S. case law",
};

export default function Layout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><header><Link className="brand" href="/">LexTrace<span>Legal precedent intelligence</span></Link><nav><Link href="/">Research</Link><Link href="/search">Search</Link></nav></header><main>{children}</main><footer>Legal research and information — not a substitute for professional legal advice.</footer></body></html>;
}
