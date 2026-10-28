import type { Metadata } from "next";
import { SiteFooter, SiteHeader } from "../site-chrome";

export const metadata: Metadata = {
  title: "개인정보처리방침",
  description:
    "결 앱과 공식 사이트의 개인정보 처리 항목, 목적, 보관 기간, 국외 이전과 이용자 권리를 안내합니다.",
  alternates: { canonical: "/privacy" },
};

const collectionRows = [
  {
    category: "Apple 로그인 (선택)",
    data: "Apple 사용자 식별자, Apple이 제공한 이메일(가려진 전달 주소 포함), 사용자가 최초 제공을 허용한 이름, 인증·갱신 토큰, 세션 정보",
    purpose: "본인 확인, 계정 생성·유지, 로그인 상태 제공, 계정 삭제와 보안 대응",
    retention:
      "계정 삭제 시까지. 로그인 세션은 발급 후 최대 30일이며, 계정 삭제 시 계정·세션·기사 의견을 함께 삭제합니다.",
  },
  {
    category: "앱 진위·설치·단말 정보",
    data: "설치 식별자(UUID), App Attest 키 식별자·공개키·검증 영수증·환경·검증 횟수, IDFV(제공되는 경우), 플랫폼, 기기 모델 코드, OS 버전, 앱 버전·빌드, 언어·지역",
    purpose: "정식 앱 확인, API 오남용·재전송 방지, 호환성·보안 오류 진단",
    retention:
      "현재 구현 기준 서비스 종료 또는 이용자의 삭제 요청 시까지. 단기 인증 challenge는 만료 후 정리합니다.",
  },
  {
    category: "개인정보 동의 기록",
    data: "설치 식별자(UUID), App Attest 키 식별자, 동의한 방침 버전·URL, 앱 버전·빌드, 동의 시각, 동의 영수증 식별자(UUID), 로그인한 경우 계정과 영수증의 연결",
    purpose:
      "현재 개인정보처리방침에 동의했는지 확인하고 회원·비회원의 동의 사실을 증빙",
    retention:
      "설치 단위 동의 기록은 위 App Attest 설치·단말 정보와 같은 기간 보관합니다. 계정과 영수증의 연결은 계정 삭제 시 삭제하지만, 설치 단위 동의 기록은 계정과 분리해 해당 보관 기간까지 유지합니다.",
  },
  {
    category: "계정 읽기 상태 (로그인 후)",
    data: "계정 식별자, 저장한 기사 식별자·저장 시각, 기사별 장면 진행, 마지막 기사 식별자, 글자 크기·행간·화면 테마·동작 효과·미디어 절약·오프라인 캐시 설정, 서버 revision·갱신 시각",
    purpose:
      "로그인한 기기 사이에서 보관함·이어 읽기·읽기 설정을 복원하고 변경 사항을 동기화",
    retention:
      "계정 삭제 시까지 최신 상태만 보관합니다. 저장 기사는 최대 100개, 진행은 최대 500개이며, 계정 삭제 시 서버 상태와 기기의 해당 계정 캐시를 삭제합니다.",
  },
  {
    category: "익명 기사 조회 집계 (회원·비회원)",
    data: "Reader 진입 때 임의 생성한 이벤트 식별자(UUID), 기사 식별자, 서버 수신 시각과 기사별 누적 조회 수",
    purpose:
      "중복 전송을 한 번만 반영해 기사별 누적 조회를 집계하고 기사 품질·서비스 운영 판단에 활용",
    retention:
      "이벤트 영수증과 누적 수는 해당 기사가 삭제되거나 서비스가 종료될 때까지 보관합니다. 조회 영수증과 누적 표에는 계정 식별자나 설치 식별자를 저장하지 않습니다.",
  },
  {
    category: "계정 검색·기사 열람 활동 (로그인 후)",
    data: "계정 식별자, 클라이언트 이벤트 식별자(UUID), 이벤트 유형(검색·기사 열기·장면 열람·기사 완료), 발생 시각·서버 수신 시각, IANA 시간대, UTC 오프셋, 현지 날짜·시간(hour), 검색어·결과 수, 기사 식별자, 장면 순서·전체 장면 수",
    purpose:
      "시간대별 검색·읽기 행동을 집계·분석하고 서비스를 개선하며 Home·일반 다음 기사 개인화 추천을 제공",
    retention:
      "계정 삭제 시까지 원시 이벤트 단위로 보관합니다. 로그아웃해도 보관하며, 앱에서 계정을 삭제하면 서버 원장과 기기의 해당 계정 전송 대기 기록을 삭제합니다. 추천 요청 때 최근 180일 안의 최신 최대 500건만 읽어 관심 주제와 시간대 신호를 요청 중 메모리에서 계산합니다. 이 추천 프로필은 별도 데이터베이스에 저장하지 않고 외부 AI·광고 제공자에게 전송하지 않습니다. 이번 버전에서는 개인 이력 화면 또는 광고 목적으로 사용하지 않습니다.",
  },
  {
    category: "기사 의견 (로그인 후 선택 제출)",
    data: "계정 식별자, 기사 식별자, 선택한 고정 의견 사유, 생성·수정 시각",
    purpose: "기사 품질과 추천 경험 개선, 중복 의견의 최신 상태 유지",
    retention:
      "계정 삭제 시까지. 같은 기사에 다시 제출하면 기존 사유와 수정 시각을 최신 값으로 바꿉니다.",
  },
  {
    category: "새 editorial 알림 (로그인 후 선택)",
    data: "APNs device token의 암호화된 원문과 SHA-256 중복 방지 해시, development·production 알림 환경, 계정·세션·설치 식별자, 등록 상태와 등록·갱신·오류 시각",
    purpose:
      "사용자가 선택한 새 editorial 알림의 수신 기기 등록, 중복 방지, Apple Push Notification service(APNs) 전송과 오류 대응",
    retention:
      "최신 device token을 등록하면 기존 값을 교체합니다. 앱에서 알림 끄기, 로그아웃, 계정 삭제 또는 만료 세션 정리 시 서버 등록을 삭제합니다. APNs가 token을 유효하지 않다고 알리면 즉시 비활성화하고 이후 전송에서 제외하며, 재등록 또는 앞의 삭제 시점까지 보관합니다.",
  },
  {
    category: "실시간 검색·서비스 요청",
    data: "사용자가 입력한 검색어, 요청한 기사·기능 식별자와 처리 결과",
    purpose: "검색 결과와 요청한 기사·기능 제공, 오류 대응",
    retention:
      "요청 자체는 결과 제공 후 별도 기능 이력으로 저장하지 않습니다. 다만 로그인 상태에서 성공한 검색과 기사 열람 행동은 위 계정 활동 기록으로 저장합니다. 아래 접속 기록에 포함되는 요청 정보는 최대 7일입니다.",
  },
  {
    category: "접속·보안 기록",
    data: "IP 주소, 요청 시각, 요청 경로, 응답 상태",
    purpose: "서비스 운영, 장애 진단, 공격·비정상 이용 탐지",
    retention:
      "Railway Hobby 로그 정책에 따라 최대 7일. 법령에 따른 보존 의무가 있는 경우에는 해당 기간까지 보관할 수 있습니다.",
  },
] as const;

const localRows = [
  {
    title: "비회원 읽기 상태",
    body: "로그인하지 않고 만든 보관함, 저장 시각, 기사별 장면 진행, 마지막 기사와 읽기 설정은 이 기기에만 남고 계정 서버로 전송하지 않습니다. 로그인하지 않은 사용자의 검색어와 계정 행동 원장은 서버에 저장하지 않습니다. 다만 Reader에 명시적으로 진입할 때 임의 UUID와 기사 식별자를 익명 조회 API로 전송하며, 조회 영수증·누적 수에는 계정·설치 식별자를 저장하지 않습니다.",
  },
  {
    title: "로그인 계정의 기기 캐시",
    body: "서버와 동기화한 계정 읽기 상태의 로컬 사본·전송 대기 변경, 검색·기사 열람 활동의 전송 대기 이벤트와 Home 개인화 추천 캐시는 재실행·오프라인 사용을 위해 계정별로 기기에 저장합니다. 로그아웃하면 보존한 채 숨기고, 같은 계정으로 다시 로그인하면 복원하며, 계정을 삭제하면 해당 계정 캐시와 전송 대기를 삭제합니다.",
  },
  {
    title: "콘텐츠와 앱 상태",
    body: "최근 받은 기사·이미지와 오프라인 자료, 온보딩 확인 상태와 앱 버전 표시 정보는 기기에 남으며 계정 읽기 상태로 동기화하지 않습니다.",
  },
  {
    title: "알림 선택과 iOS 권한",
    body: "앱에서 알림을 켰는지 여부와 iOS의 알림·사운드 권한 상태는 기기에서만 확인·보관하며 계정 서버로 보내지 않습니다. 앱에서 알림을 끄거나 로그아웃하면 선택값과 서버 등록을 해제하고, 시스템 권한은 iOS 설정에서 변경할 수 있습니다.",
  },
] as const;

const toc = [
  ["scope", "1. 적용 범위와 운영자"],
  ["data", "2. 처리하는 정보"],
  ["local", "3. 기기 저장과 동기화 경계"],
  ["sharing", "4. 제공·위탁·국외 이전"],
  ["rights", "5. 이용자의 권리와 삭제"],
  ["security", "6. 안전성 확보 조치"],
  ["website", "7. 웹사이트의 쿠키·분석"],
  ["changes", "8. 방침의 변경"],
  ["contact", "9. 문의처"],
] as const;

export default function PrivacyPage() {
  return (
    <>
      <SiteHeader />
      <main id="main-content" className="privacy-page">
        <header className="privacy-hero section-shell">
          <p className="eyebrow">PRIVACY POLICY · VERSION 1.5.0</p>
          <h1>개인정보처리방침</h1>
          <p className="privacy-hero__lede">
            결은 읽기에 필요한 정보만 처리하고, 기기에 남는 기록과 서버로
            전송되는 정보를 구분해 설명합니다.
          </p>
          <div className="policy-meta" aria-label="방침 정보">
            <span>운영자&nbsp;&nbsp;Universis.Inc</span>
            <span>시행일&nbsp;&nbsp;2026. 07. 26.</span>
            <a href="mailto:contact@example.com">
              문의&nbsp;&nbsp;contact@example.com
            </a>
          </div>
        </header>

        <section className="privacy-summary" aria-label="개인정보 처리 요약">
          <div className="section-shell privacy-summary__grid">
            <article>
              <span>01</span>
              <h2>로그인은 선택</h2>
              <p>Apple 로그인 없이도 읽기를 시작할 수 있습니다.</p>
            </article>
            <article>
              <span>02</span>
              <h2>광고 추적 없음</h2>
              <p>광고 식별자와 위치·연락처를 수집하지 않습니다.</p>
            </article>
            <article>
              <span>03</span>
              <h2>로그인하면 계정 동기화</h2>
              <p>보관함·진행·읽기 설정을 계정에 저장하고, 비회원 기록은 기기에만 남깁니다.</p>
            </article>
          </div>
        </section>

        <div className="privacy-layout section-shell">
          <aside className="privacy-toc" aria-label="개인정보처리방침 목차">
            <p>목차</p>
            <ol>
              {toc.map(([href, label]) => (
                <li key={href}>
                  <a href={`#${href}`}>{label}</a>
                </li>
              ))}
            </ol>
          </aside>

          <article className="policy-content">
            <section id="scope">
              <p className="policy-number">01</p>
              <h2>적용 범위와 운영자</h2>
              <p>
                이 방침은 Universis.Inc가 제공하는 iPhone·iPad 앱
                <strong> ‘결’</strong>과 이 공식 웹사이트에 적용됩니다.
                Universis.Inc는 아래 목적과 범위에서 개인정보를 처리하는
                개인정보처리자입니다.
              </p>
              <dl className="policy-definition">
                <div>
                  <dt>운영 주체</dt>
                  <dd>Universis.Inc</dd>
                </div>
                <div>
                  <dt>개인정보 보호 문의</dt>
                  <dd>
                    <a href="mailto:contact@example.com">
                      contact@example.com
                    </a>
                  </dd>
                </div>
                <div>
                  <dt>처리 원칙</dt>
                  <dd>서비스 제공·보안에 필요한 범위로 최소화</dd>
                </div>
              </dl>
            </section>

            <section id="data">
              <p className="policy-number">02</p>
              <h2>처리하는 정보, 목적과 보관 기간</h2>
              <p>
                앱은 이용자가 기능을 선택하거나 서비스에 요청을 보내는
                과정에서 아래 정보를 TLS로 암호화해 처리합니다. 위치,
                연락처, 사진 보관함, 카메라·마이크, 광고 식별자는 수집하지
                않습니다.
              </p>
              <div className="table-scroll" tabIndex={0} role="region" aria-label="개인정보 처리 항목 표">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">구분</th>
                      <th scope="col">처리 항목</th>
                      <th scope="col">목적</th>
                      <th scope="col">보관 기간</th>
                    </tr>
                  </thead>
                  <tbody>
                    {collectionRows.map((row) => (
                      <tr key={row.category}>
                        <th scope="row">{row.category}</th>
                        <td>{row.data}</td>
                        <td>{row.purpose}</td>
                        <td>{row.retention}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="policy-note">
                보관 기간이 끝나거나 목적이 달성되면 복구하기 어려운 방식으로
                지체 없이 삭제합니다. 다만 관계 법령이 별도 보관을 요구하는
                경우에는 해당 정보만 분리해 법정 기간 동안 보관할 수 있습니다.
                동의 원장에는 IP 주소, APNs device token, Apple 인증 token이나
                authorization code, 개인정보처리방침 본문 전체를 저장하지
                않습니다.
              </p>
            </section>

            <section id="local">
              <p className="policy-number">03</p>
              <h2>기기 저장과 계정 동기화의 경계</h2>
              <p>
                앱은 아래 정보를 Application Support 영역에 저장합니다.
                비회원 읽기 상태와 콘텐츠 캐시는 서버로 보내지 않지만,
                로그인한 계정의 보관함·진행·읽기 설정은 위 표의 계정 읽기
                상태로 서버와 동기화합니다.
              </p>
              <div className="local-data-list">
                {localRows.map((row, index) => (
                  <div key={row.title}>
                    <span>0{index + 1}</span>
                    <h3>{row.title}</h3>
                    <p>{row.body}</p>
                  </div>
                ))}
              </div>
              <div className="policy-callout">
                <strong>계정 삭제와 기기 기록 삭제는 서로 다릅니다.</strong>
                <p>
                  로그아웃하면 서버의 계정 읽기 상태·행동 원장과 기기의 해당
                  계정 캐시·전송 대기 이벤트는 보존하되 화면에서 숨기고 비회원
                  상태로 전환합니다. 앱에서 계정을 삭제하면 서버의 계정·세션과
                  기사 의견·계정 읽기 상태·검색 및 기사 열람 행동 원장, APNs
                  알림 등록·동의 영수증 연결과 기기의 해당 계정 캐시·전송 대기
                  이벤트를 삭제합니다. 계정과 분리된 설치 단위 동의 기록과
                  로그인하지 않고 만든 비회원 기록은 남습니다. 계정·설치
                  식별자를 저장하지 않는 익명 기사 조회 영수증과 누적 수는
                  계정 기록에 연결하거나 계정 삭제 대상으로 식별할 수 없으며,
                  해당 기사 삭제 또는 서비스 종료 때 삭제됩니다. 앱 삭제는
                  기기의 비회원 기록과 캐시를 지우지만 서버 계정은 자동
                  삭제하지 않으므로, 서버 정보까지 지우려면 먼저 앱에서
                  계정을 삭제해 주세요.
                </p>
              </div>
            </section>

            <section id="sharing">
              <p className="policy-number">04</p>
              <h2>제3자 제공, 처리위탁과 국외 이전</h2>
              <p>
                Universis.Inc는 개인정보를 판매하거나 광고 목적으로 제3자에게
                제공하지 않습니다. 이용자의 별도 동의가 있거나 법령상 의무가
                있는 경우를 제외하고 처리 목적 밖으로 제공하지 않습니다.
              </p>
              <h3>서비스 제공을 위한 국외 처리</h3>
              <div className="table-scroll" tabIndex={0} role="region" aria-label="개인정보 국외 처리 표">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">수탁자·서비스</th>
                      <th scope="col">국가</th>
                      <th scope="col">항목과 목적</th>
                      <th scope="col">시기·방법</th>
                      <th scope="col">보관</th>
                      <th scope="col">관련 근거</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th scope="row">
                        Railway Corporation
                        <small>contact@example.com</small>
                      </th>
                      <td>Singapore, United States</td>
                      <td>
                        계정·계정 읽기 상태·계정 검색 및 기사 열람 활동·익명
                        기사 조회 이벤트와 누적 수·설치·앱 인증·기사 의견·
                        검색 및 개인화 추천 요청·IP 접속 기록의 API,
                        데이터베이스, 보안·운영 인프라 제공
                      </td>
                      <td>서비스 이용 중 TLS로 전송·처리</td>
                      <td>위 항목별 기간과 서비스 계약·법정 의무까지</td>
                      <td>
                        개인정보 보호법 제28조의8 제1항 제3호 가목
                        (계약 이행을 위한 처리위탁·보관)
                      </td>
                    </tr>
                    <tr>
                      <th scope="row">Apple Inc.</th>
                      <td>United States 등 Apple의 운영 국가</td>
                      <td>
                        이용자가 Apple 로그인을 선택한 경우 인증 요청,
                        authorization code 검증과 계정 연결·해제
                      </td>
                      <td>로그인·계정 삭제 시 Apple 보안 채널로 전송</td>
                      <td>Apple의 개인정보 처리방침과 이용자 설정에 따름</td>
                      <td>
                        개인정보 보호법 제28조의8 제1항 제3호 가목
                        (계약 이행을 위한 처리위탁)
                      </td>
                    </tr>
                    <tr>
                      <th scope="row">
                        Apple Inc. · APNs
                        <small>
                          <a href="https://www.apple.com/kr/privacy/contact">
                            Apple 개인정보 문의
                          </a>
                        </small>
                      </th>
                      <td>
                        미국, EU, 영국 및 Global CBPR 참여국에 위치한 Apple
                        서버
                      </td>
                      <td>
                        APNs device token, 알림 제목, 본문(부제 또는 요약),
                        기사 식별자, bundle topic·development/production
                        환경, APNs request ID와 전송·오류 진단 정보의 수신
                        기기 식별·알림 전달·보안 및 오류 대응
                      </td>
                      <td>
                        새 editorial 최초 발행 시 Apple APNs API로 TLS
                        암호화 전송
                      </td>
                      <td>
                        즉시 전달하며, 기기가 오프라인인 등 바로 전달할 수
                        없으면 Apple 기준 최대 30일 보관할 수 있습니다.
                        그 밖에는 처리 목적이 달성되고 법적·사업상 보관
                        필요가 없어질 때까지 Apple 정책에 따라 처리합니다.
                      </td>
                      <td>
                        개인정보 보호법 제28조의8 제1항 제3호 가목
                        (계약 이행을 위한 처리위탁·보관)
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p className="policy-note">
                위 국외 처리는 서비스 계약 이행에 필요한 호스팅·인증·알림
                처리위탁 또는 보관으로서 개인정보 보호법 제28조의8 제1항
                제3호 가목에 따른 정보를 공개합니다. Railway 국외 처리를
                원하지 않으면 아래 문의처로 요청하거나 서비스 이용을 중단할
                수 있으며, 기사 수신·검색·계정처럼 서버가 필요한 기능은
                제한될 수 있습니다. APNs 국외 처리는 앱 설정에서
                <strong> 새 editorial 알림</strong>을 끄고 iOS 설정에서
                알림 권한을 철회해 거부할 수 있습니다. 이 경우 새 editorial
                알림만 제한되며 기사 읽기와 검색은 계속 이용할 수 있습니다.
              </p>
            </section>

            <section id="rights">
              <p className="policy-number">05</p>
              <h2>이용자의 권리와 삭제 방법</h2>
              <p>
                이용자는 자신의 개인정보에 대해 열람, 정정, 삭제, 처리정지와
                동의 철회를 요청할 수 있습니다. 정당한 대리인을 통해서도
                요청할 수 있으며, 필요한 범위에서 본인 또는 대리 관계를
                확인할 수 있습니다.
              </p>
              <ol className="rights-steps">
                <li>
                  <span>01</span>
                  <div>
                    <h3>계정 삭제</h3>
                    <p>
                      앱의 설정 → 계정 → 계정 삭제에서 요청합니다. 계정,
                      세션, 암호화된 Apple 갱신 토큰과 계정에 연결된 기사
                      의견·계정 읽기 상태·검색 및 기사 열람 행동 원장·APNs
                      알림 등록·동의 영수증 연결과 기기의 해당 계정 캐시·전송
                      대기 이벤트를 삭제하고 Apple 토큰 해제를 시도합니다.
                      로그인하지 않고 만든 비회원 기록은 남습니다.
                      계정과 분리된 설치 단위 동의 기록의 삭제는 아래 문의처로
                      별도 요청할 수 있습니다.
                    </p>
                  </div>
                </li>
                <li>
                  <span>02</span>
                  <div>
                    <h3>알림 철회와 device token 삭제</h3>
                    <p>
                      앱 설정에서 새 editorial 알림을 끄면 서버의 APNs
                      device token 등록을 삭제합니다. iOS 설정에서도 알림
                      권한을 언제든 철회할 수 있습니다. 알림을 거부해도 기사
                      읽기와 검색에는 영향이 없습니다.
                    </p>
                  </div>
                </li>
                <li>
                  <span>03</span>
                  <div>
                    <h3>설치·단말 정보 또는 기타 권리 요청</h3>
                    <p>
                      <a href="mailto:contact@example.com">
                        contact@example.com
                      </a>
                      으로 요청해 주세요. 요청을 확인한 뒤 지체 없이
                      처리하고 결과를 안내합니다.
                    </p>
                  </div>
                </li>
                <li>
                  <span>04</span>
                  <div>
                    <h3>기기 안의 비회원 기록·캐시 삭제</h3>
                    <p>
                      iOS 설정에서 결의 앱 데이터를 삭제하거나 앱을
                      삭제합니다. 이 동작은 서버의 계정 읽기 상태를 삭제하지
                      않으므로 서버 정보도 지우려면 앱 삭제 전에 계정 삭제를
                      완료해 주세요.
                    </p>
                  </div>
                </li>
              </ol>
            </section>

            <section id="security">
              <p className="policy-number">06</p>
              <h2>안전성 확보 조치</h2>
              <ul className="policy-bullets">
                <li>앱과 서버 사이의 정보는 TLS로 암호화해 전송합니다.</li>
                <li>
                  로그인 세션 원문은 기기의 Keychain에 보관하고, 서버에는
                  복구할 수 없는 SHA-256 해시를 저장합니다.
                </li>
                <li>
                  Apple 갱신 토큰은 서버에서 암호화해 저장하고 계정 삭제 시
                  함께 삭제합니다.
                </li>
                <li>
                  APNs device token은 서버에서 암호화하고 SHA-256 해시로
                  중복을 방지하며, token 원문·알림 payload·인증 key를 로그에
                  남기지 않습니다.
                </li>
                <li>
                  APNs에는 알림 전달에 필요한 최소 항목만 TLS로 전송하며
                  계정 ID·email·기사 문서 전체·읽기 기록은 알림 payload에
                  포함하지 않습니다.
                </li>
                <li>
                  Apple App Attest로 정식 앱 설치인지 확인하고 재전송·변조된
                  요청을 차단합니다.
                </li>
                <li>
                  개인정보 접근 권한을 서비스 운영에 필요한 범위로 제한하고
                  보안 기록을 짧게 보관합니다.
                </li>
              </ul>
            </section>

            <section id="website">
              <p className="policy-number">07</p>
              <h2>웹사이트의 쿠키와 분석</h2>
              <p>
                이 공식 사이트에는 Universis.Inc가 운영하는 회원가입 양식,
                광고, 방문자 분석 도구 또는 추적 쿠키가 없습니다. 문의는
                사용자의 email 앱을 여는 `mailto` 링크로만 제공합니다.
                호스팅 사업자는 사이트 제공과 보안을 위해 IP 주소와 요청
                기록을 최대 7일 동안 처리할 수 있습니다.
              </p>
            </section>

            <section id="changes">
              <p className="policy-number">08</p>
              <h2>방침의 변경</h2>
              <p>
                처리 항목, 목적, 수탁자 또는 이용자 권리에 중요한 변경이
                생기면 실제 기능을 적용하기 전에 이 페이지의 버전과 시행일을
                갱신하고 앱 또는 사이트에서 알립니다. 이번 1.5.0 개정은
                회원·비회원의 익명 기사 조회 집계와 로그인 계정의 최근
                검색·읽기 활동을 이용한 Home·일반 다음 기사 개인화 추천,
                요청 중 비영속 프로필과 계정별 기기 캐시 경계를 반영했습니다.
              </p>
              <dl className="policy-definition policy-definition--compact">
                <div>
                  <dt>현재 버전</dt>
                  <dd>1.5.0</dd>
                </div>
                <div>
                  <dt>공고·시행일</dt>
                  <dd>2026년 7월 26일</dd>
                </div>
                <div>
                  <dt>주요 변경</dt>
                  <dd>익명 기사 조회 집계와 계정 개인화 추천 경계 추가</dd>
                </div>
              </dl>
            </section>

            <section id="contact" className="contact-section">
              <p className="policy-number">09</p>
              <h2>개인정보 문의</h2>
              <p>
                개인정보 처리, 권리 행사 또는 이 방침에 관한 질문을 보내
                주세요.
              </p>
              <a className="button button--primary" href="mailto:contact@example.com">
                이메일로 문의하기 <span aria-hidden="true">↗</span>
              </a>
              <p className="contact-section__detail">
                개인정보 보호 담당 · Universis.Inc
                <br />
                contact@example.com
              </p>
            </section>
          </article>
        </div>
      </main>
      <SiteFooter />
    </>
  );
}
