# 퇴근 알림 (FCM 웹 푸시, #63)

퇴근 10분 전에 서버가 오늘의 카드를 미리 만들고(#59), 등록된 브라우저에 알림을 보낸다.
알림을 누르면 `FRONTEND_URL` 이 열리고, 카드는 이미 만들어져 있어 바로 뜬다.

## 흐름

```
프론트: 알림 권한 → Firebase getToken → PUT /users/me/push-tokens
서버:   1분마다 퇴근 10분 안 동선 → 카드 생성(없으면) → FCM 발송(하루 1회) → today_cards.notified_at
```

- 서버는 Firebase Admin SDK 없이 HTTP v1 API 를 직접 부른다 (`app/clients/fcm.py`)
- FCM 이 404(UNREGISTERED)로 답한 토큰은 지운다
- `FCM_SERVICE_ACCOUNT_JSON` 이 없으면 알림만 건너뛰고 카드는 그대로 만든다
- 루프는 운영(`APP_ENV=prod`)에서만 돈다. 로컬에서는 `POST /users/me/push-tokens/test` 로 확인한다

## 1. Firebase 프로젝트 (한 번)

1. https://console.firebase.google.com 에서 프로젝트를 만든다
2. 프로젝트 설정 › 일반 › 내 앱 › 웹 앱 추가. 나오는 `firebaseConfig` 는 프론트가 쓴다
3. 프로젝트 설정 › 클라우드 메시징 › 웹 푸시 인증서 › 키 쌍 생성. 이 **VAPID 공개 키**는 프론트가 쓴다
4. 프로젝트 설정 › 서비스 계정 › 새 비공개 키 생성. 내려받은 JSON 은 **서버 비밀**이다.
   레포·채팅·노션에 올리지 않고 비밀번호 관리자로 전달한다

## 2. 서버 환경변수 (Railway Variables)

| 키 | 값 |
| --- | --- |
| `FCM_SERVICE_ACCOUNT_JSON` | 위 4번 JSON 을 **한 줄로** 붙여넣는다 (`jq -c . key.json` 의 출력) |
| `FRONTEND_URL` | 알림을 누르면 열 주소. `https://` 만 쓴다 (FCM 이 http 링크를 받지 않는다) |

## 3. 프론트 (Next.js 예시)

`public/firebase-messaging-sw.js` (서비스 워커. 백그라운드 알림을 띄운다)

```js
importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js");
firebase.initializeApp({ /* firebaseConfig */ });
firebase.messaging();
```

로그인 뒤 알림 켜기 버튼에서:

```ts
import { getMessaging, getToken } from "firebase/messaging";

const permission = await Notification.requestPermission();
if (permission === "granted") {
  const token = await getToken(getMessaging(app), { vapidKey: VAPID_PUBLIC_KEY });
  await fetch(`${API}/users/me/push-tokens`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
}
```

로그아웃 때는 같은 본문으로 `DELETE /users/me/push-tokens` 를 부른다.

## 4. 시연 전 확인

1. 시연 기기 브라우저에서 알림 권한을 허용하고 토큰을 등록한다
2. Swagger 에서 `POST /users/me/push-tokens/test` → `{"sent": 1}` 과 함께 알림이 떠야 한다
3. iOS 는 사파리에서 **홈 화면에 추가한 웹앱**에서만 웹 푸시를 받는다 (iOS 16.4 이상)
4. 데모 계정의 퇴근 시각 10분 전에 서버가 알림을 보낸다. 시각을 시연 시간에 맞추려면
   `data/demo_commute.csv` 의 `leave_time` 을 바꾸고 `seed_demo` 를 다시 돌린다
