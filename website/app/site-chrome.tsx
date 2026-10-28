import Link from "next/link";
import { APP_STORE_URL } from "./site-config";

const navigation = [
  { href: "/#reading", label: "읽는 방식" },
  { href: "/#features", label: "주요 기능" },
  { href: "/privacy", label: "개인정보처리방침" },
] as const;

export function SiteHeader() {
  return (
    <>
      <a className="skip-link" href="#main-content">
        본문 바로가기
      </a>
      <header className="site-header">
        <div className="site-header__inner">
          <Link className="brand" href="/" aria-label="결 홈">
            <span className="brand__mark" aria-hidden="true">
              결
            </span>
            <span className="brand__descriptor">AI·기술 뉴스 리더</span>
          </Link>
          <nav className="site-nav" aria-label="주요 메뉴">
            {navigation.map((item) => (
              <Link key={item.href} href={item.href}>
                {item.label}
              </Link>
            ))}
          </nav>
          <a
            className="release-chip"
            href={APP_STORE_URL}
            target="_blank"
            rel="noreferrer"
            aria-label="App Store에서 결 다운로드"
          >
            App Store에서 다운로드
          </a>
        </div>
      </header>
    </>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="site-footer__top">
        <Link className="brand brand--footer" href="/" aria-label="결 홈">
          <span className="brand__mark" aria-hidden="true">
            결
          </span>
          <span className="brand__descriptor">긴 기사를 장면으로 읽는 앱</span>
        </Link>
        <p>
          헤드라인을 더 많이 넘기기보다,
          <br />한 이슈의 맥락을 끝까지 읽습니다.
        </p>
      </div>
      <div className="site-footer__bottom">
        <span>© 2026 Universis.Inc</span>
        <div className="site-footer__links">
          <Link href="/privacy">개인정보처리방침</Link>
          <a href="mailto:contact@example.com">
            contact@example.com
          </a>
        </div>
      </div>
    </footer>
  );
}
