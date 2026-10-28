import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

async function requestOrigin() {
  const requestHeaders = await headers();
  const rawHost =
    requestHeaders.get("x-forwarded-host")?.split(",")[0]?.trim() ||
    requestHeaders.get("host")?.trim() ||
    "localhost";
  const host = /^[a-z0-9.-]+(?::\d+)?$/i.test(rawHost)
    ? rawHost
    : "localhost";
  const rawProtocol =
    requestHeaders.get("x-forwarded-proto")?.split(",")[0]?.trim() || "";
  const protocol =
    rawProtocol === "http" || rawProtocol === "https"
      ? rawProtocol
      : host.startsWith("localhost") || host.startsWith("127.0.0.1")
        ? "http"
        : "https";

  return new URL(`${protocol}://${host}`);
}

export async function generateMetadata(): Promise<Metadata> {
  const origin = await requestOrigin();
  const description =
    "오래 남을 AI·기술 기사를 고르고, 원문의 핵심 흐름과 출처·사진·수치를 한 장면씩 이어 읽는 iPhone·iPad 뉴스 앱입니다.";
  const socialImage = new URL("/og.png", origin).toString();

  return {
    metadataBase: origin,
    title: {
      default: "결 | 긴 기사를 장면으로 읽는 AI·기술 뉴스",
      template: "%s | 결",
    },
    description,
    applicationName: "결",
    keywords: [
      "AI 기술 뉴스",
      "긴 기사 읽기",
      "장면형 뉴스 리더",
      "기사 보관함",
      "iPhone 뉴스 앱",
    ],
    alternates: { canonical: "/" },
    icons: {
      icon: [{ url: "/app-icon.png", type: "image/png", sizes: "1024x1024" }],
      apple: [{ url: "/app-icon.png", sizes: "1024x1024" }],
    },
    openGraph: {
      type: "website",
      locale: "ko_KR",
      siteName: "결",
      title: "결 — 긴 기사를 넘기듯, 맥락은 끝까지",
      description,
      url: origin,
      images: [
        {
          url: socialImage,
          width: 1200,
          height: 630,
          alt: "결 — 긴 기사를 장면으로 읽는 AI·기술 뉴스 앱",
        },
      ],
    },
    twitter: {
      card: "summary_large_image",
      title: "결 — 긴 기사를 넘기듯, 맥락은 끝까지",
      description,
      images: [socialImage],
    },
    robots: { index: true, follow: true },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
