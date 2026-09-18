import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "LexTrace — Litigation Argument Intelligence",
  description: "Trace every argument. Test every authority.",
};

export default function Layout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><header><Link className="brand" href="/">LexTrace<span>Litigation Argument Intelligence</span></Link><nav><Link href="/matters">My matters</Link><Link href="/research">Research</Link><Link href="/search">Find cases</Link></nav></header><main>{children}</main><footer>Legal research and information — not a substitute for professional legal advice.</footer></body></html>;
}
