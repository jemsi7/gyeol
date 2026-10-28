import type { Metadata } from "next";
import Image from "next/image";
import { SiteFooter, SiteHeader } from "./site-chrome";
import { APP_STORE_URL } from "./site-config";

export const metadata: Metadata = {
  title: "결 | 긴 기사를 장면으로 읽는 AI·기술 뉴스",
  description:
    "오래 남을 AI·기술 기사를 고르고, 원문의 핵심 흐름과 출처·사진·수치를 한 장면씩 이어 읽는 iPhone·iPad 뉴스 앱입니다.",
  alternates: { canonical: "/" },
};

const principles = [
  {
    number: "01",
    title: "오래 남을 기사부터",
    body: "반복적이고 얕은 소식을 덜어 내고, 시간이 지나도 맥락이 남는 AI·기술 기사를 읽기 루트로 묶습니다.",
  },
  {
    number: "02",
    title: "긴 기사를 한 장면씩",
    body: "한 문단 요약으로 끝내지 않습니다. 원문의 핵심 흐름을 따라 한 화면에 한 논점씩 이어 보여 줍니다.",
  },
  {
    number: "03",
    title: "근거를 읽기 가까이에",
    body: "출처와 원문, 사진과 인용, 수치와 그래프를 필요한 장면에 두어 직접 확인할 길을 남깁니다.",
  },
] as const;

const trustItems = [
  {
    title: "로그인 없이 읽기",
    body: "읽기는 바로 시작하고, 계정 기능이 필요할 때만 Apple 로그인을 선택합니다.",
  },
  {
    title: "광고·추적 SDK 없음",
    body: "읽기 시간을 광고 프로필로 만들기 위한 분석·광고 SDK를 앱에 넣지 않았습니다.",
  },
  {
    title: "계정으로 이어지는 읽기",
    body: "로그인하면 보관함·읽던 장면·설정을 계정에 동기화하고, 로그인하지 않으면 이 기기에만 남깁니다.",
  },
] as const;

export default function Home() {
  const structuredData = {
    "@context": "https://schema.org",
    "@type": "MobileApplication",
    name: "결",
    alternateName: "결 AI·기술 뉴스",
    applicationCategory: "NewsApplication",
    operatingSystem: "iOS, iPadOS",
    installUrl: APP_STORE_URL,
    description:
      "AI·기술 산업의 긴 기사를 읽기 루트로 고르고, 원문의 핵심 흐름과 출처·사진·수치를 한 장면씩 이어 읽는 뉴스 앱입니다.",
  };

  return (
    <>
      <SiteHeader />
      <main id="main-content">
        <section className="hero section-shell" aria-labelledby="hero-title">
          <div className="hero__copy">
            <p className="eyebrow">AI·TECH NEWS READER</p>
            <h1 id="hero-title">
              긴 기사를 넘기듯,
              <span>맥락은 끝까지.</span>
            </h1>
            <p className="hero__lede">
              결은 AI·기술 산업의 오래 남을 기사를 고르고, 원문의 핵심
              흐름과 출처·사진·수치를 한 장면씩 이어 보여 줍니다.
            </p>
            <div className="hero__actions" aria-label="서비스 안내">
              <a className="button button--primary" href="#reading">
                결이 다른 이유 <span aria-hidden="true">↓</span>
              </a>
              <a className="text-link" href="/privacy">
                개인정보처리방침 <span aria-hidden="true">↗</span>
              </a>
            </div>
            <a
              className="launch-status"
              href={APP_STORE_URL}
              target="_blank"
              rel="noreferrer"
              aria-label="App Store에서 결 다운로드"
            >
              <span className="status-dot" aria-hidden="true" />
              iPhone · iPad&nbsp;&nbsp;/&nbsp;&nbsp;App Store에서 다운로드
              <span className="launch-status__arrow" aria-hidden="true">↗</span>
            </a>
          </div>

          <div className="hero__visual" aria-label="결 앱 홈 화면 미리보기">
            <div className="hero__folio" aria-hidden="true">
              <span>오늘의 결</span>
              <strong>01—04</strong>
            </div>
            <figure className="phone-frame phone-frame--hero">
              <Image
                src="/screens/home.png"
                width="390"
                height="844"
                alt="결 앱의 읽기 홈. 이어 읽을 기사와 오늘의 AI·기술 기사를 세로 목록으로 보여 준다."
                unoptimized
              />
            </figure>
            <p className="hero__caption">
              <span>01</span>
              오늘 무엇부터 읽을지,
              <br />한눈에 고르는 읽기 홈
            </p>
          </div>
        </section>

        <section className="manifesto section-shell" id="reading">
          <p className="section-index">읽는 방식 / 01</p>
          <div className="manifesto__statement">
            <p>뉴스는 쉬지 않고 도착합니다.</p>
            <h2>
              많이 본 것과,
              <br />제대로 이해한 것은 다릅니다.
            </h2>
          </div>
          <p className="manifesto__body">
            제목은 짧고 피드는 빠르지만, 중요한 조건과 숫자, 서로 다른
            주장의 관계는 긴 기사 안에 남아 있습니다. 결은 헤드라인을 더
            보태기보다 무엇을 어떤 순서로 읽을지, 어떻게 끝까지 읽을지를
            함께 설계합니다.
          </p>
        </section>

        <section className="principles section-shell" aria-label="결의 세 가지 원칙">
          {principles.map((principle) => (
            <article className="principle" key={principle.number}>
              <span className="principle__number">{principle.number}</span>
              <h3>{principle.title}</h3>
              <p>{principle.body}</p>
            </article>
          ))}
        </section>

        <section className="feature feature--reader section-shell" id="features">
          <div className="feature__visual feature__visual--teal">
            <figure className="phone-frame phone-frame--reader">
              <Image
                src="/screens/reader.png"
                width="1206"
                height="2622"
                loading="lazy"
                alt="결의 장면형 리더. 기사 제목, 장면 진행, 본문과 다음 장면 버튼이 한 화면에 정돈되어 있다."
                unoptimized
              />
            </figure>
            <span className="feature__visual-label">SCENE READER</span>
          </div>
          <div className="feature__copy">
            <p className="section-index">장면형 리더 / 02</p>
            <h2>한 화면에,<br />한 논점씩.</h2>
            <p className="feature__lead">
              요약 한 덩어리로 줄이지 않습니다. 앞 장면의 질문이 다음
              장면의 근거로 이어지도록 긴 기사의 핵심 흐름을 다시 엮습니다.
            </p>
            <dl className="feature-list">
              <div>
                <dt>장면 이동</dt>
                <dd>좌우로 넘기거나 명확한 버튼으로 내 속도에 맞춰 읽습니다.</dd>
              </div>
              <div>
                <dt>출처와 원문</dt>
                <dd>확인이 필요한 순간, 기사 목차와 원문으로 돌아갑니다.</dd>
              </div>
              <div>
                <dt>이어 읽기</dt>
                <dd>멈춘 기사와 장면을 기억해 다음 방문을 바로 잇습니다.</dd>
              </div>
            </dl>
          </div>
        </section>

        <section className="feature feature--discovery section-shell">
          <div className="feature__copy">
            <p className="section-index">둘러보기 / 03</p>
            <h2>오늘의 속도와,<br />오래 남을 깊이.</h2>
            <p className="feature__lead">
              주제별 읽기 루트를 살피거나 키워드로 찾아보세요. 빠르게 볼
              기사와 깊게 읽을 기사를 나누고, 마음에 남은 기사는 보관함에
              담아 다시 시작할 수 있습니다.
            </p>
            <ul className="check-list">
              <li>AI·기술 산업에 집중한 읽기 루트</li>
              <li>빠른 결과와 깊은 결과를 나눈 검색</li>
              <li>보관함과 저장된 콘텐츠의 제한적 오프라인 읽기</li>
            </ul>
          </div>
          <div className="feature__visual feature__visual--paper">
            <span className="route-stamp" aria-hidden="true">DEEP / QUICK</span>
            <figure className="phone-frame phone-frame--discovery">
              <Image
                src="/screens/discovery.png"
                width="1206"
                height="2622"
                loading="lazy"
                alt="결의 둘러보기와 검색 화면. 주제별 읽기 루트와 빠른 결과, 깊은 결과를 구분해 보여 준다."
                unoptimized
              />
            </figure>
          </div>
        </section>

        <section className="trust-section" aria-labelledby="trust-title">
          <div className="section-shell">
            <div className="trust-section__heading">
              <p className="section-index section-index--light">읽기에만 집중 / 04</p>
              <h2 id="trust-title">조용히 읽고,<br />필요할 때만 연결합니다.</h2>
            </div>
            <div className="trust-grid">
              {trustItems.map((item, index) => (
                <article key={item.title}>
                  <span>0{index + 1}</span>
                  <h3>{item.title}</h3>
                  <p>{item.body}</p>
                </article>
              ))}
            </div>
            <a className="trust-section__link" href="/privacy">
              개인정보 처리 방식 자세히 보기 <span aria-hidden="true">↗</span>
            </a>
          </div>
        </section>

        <section className="access-section section-shell">
          <div>
            <p className="section-index">iPhone + iPad / 05</p>
            <h2>화면은 달라도,<br />읽기의 결은 그대로.</h2>
          </div>
          <div className="access-section__body">
            <p>
              작은 iPhone 화면부터 iPad의 넓은 읽기 면까지 여백과 줄 길이를
              다시 맞춥니다. Dynamic Type, VoiceOver, Reduce Motion을
              지원해 각자의 방식으로 차분하게 읽을 수 있습니다.
            </p>
            <div className="access-labels" aria-label="지원 기능">
              <span>Dynamic Type</span>
              <span>VoiceOver</span>
              <span>Reduce Motion</span>
            </div>
          </div>
        </section>

        <section className="closing section-shell" aria-labelledby="closing-title">
          <p className="eyebrow">AVAILABLE ON IPHONE &amp; IPAD</p>
          <h2 id="closing-title">오늘 읽을 가치가 있는 기사부터,<br />한 장면씩 끝까지.</h2>
          <p>결을 지금 App Store에서 만나보세요.</p>
          <div className="closing__actions">
            <a
              className="button button--primary"
              href={APP_STORE_URL}
              target="_blank"
              rel="noreferrer"
              aria-label="App Store에서 결 다운로드"
            >
              App Store에서 다운로드 <span aria-hidden="true">↗</span>
            </a>
            <a className="text-link" href="mailto:contact@example.com">
              문의하기 <span aria-hidden="true">↗</span>
            </a>
          </div>
        </section>
      </main>
      <SiteFooter />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(structuredData) }}
      />
    </>
  );
}
