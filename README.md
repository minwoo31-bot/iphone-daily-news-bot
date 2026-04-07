# iPhone Daily News Telegram Bot

매일 자동으로 뉴스 10개를 뽑아 한국어 요약을 만든 뒤 텔레그램으로 보내는 시스템입니다.  
아이폰에서는 텔레그램 앱 알림으로 바로 받습니다.

## Why this is stable

- 아이폰 단축어 자동화에 의존하지 않고, 서버 측 스케줄(GitHub Actions)로 매일 고정 실행
- Telegram Bot API는 단순/안정적
- Gemini 요약 실패 시에도 제목+링크 fallback 전송

## 1) Telegram Bot 만들기

1. 텔레그램에서 `@BotFather` 검색
2. `/newbot` 실행 후 봇 생성
3. 발급된 `TELEGRAM_BOT_TOKEN` 저장
4. 생성한 봇과 대화창을 열고 `시작`(또는 `/start`) 한 번 누르기

`TELEGRAM_CHAT_ID` 찾기:

1. 아래 URL에서 `YOUR_BOT_TOKEN` 교체 후 브라우저 열기  
   `https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates`
2. 결과 JSON에서 `"chat":{"id": ...}` 값을 복사

## 2) Gemini API Key 만들기

1. Google AI Studio에서 API 키 생성
2. `GEMINI_API_KEY`로 저장

## 3) GitHub에 업로드

이 폴더를 새 GitHub 저장소로 푸시하세요.

## 4) GitHub Secrets/Variables 설정

저장소 `Settings > Secrets and variables > Actions` 에서 아래 추가:

Secrets:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GEMINI_API_KEY`

Variables (선택):
- `GEMINI_MODEL` = `gemini-2.0-flash`
- `MAX_NEWS` = `10`
- `RSS_FEEDS` = `https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko`

## 5) 동작 확인

1. `Actions` 탭
2. `Daily News Digest` 워크플로 선택
3. `Run workflow` 클릭
4. 텔레그램 메시지 수신 확인

## 6) 아이폰 적용

1. 아이폰에 텔레그램 앱 설치/로그인
2. 생성한 봇 채팅을 상단 고정
3. iOS 알림 허용

이후 매일 자동으로 요약이 푸시됩니다.

## 스케줄 시간 변경

파일: `.github/workflows/daily-news.yml`

- 현재 설정: 매일 `07:30 KST`
- `cron`은 UTC 기준

예: 매일 오전 8시 KST로 변경하려면 `0 23 * * *` (UTC 전날 23:00)
