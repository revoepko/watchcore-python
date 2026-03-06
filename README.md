# watchcore-python

본 저장소는 codex (GPT-5.3-codex)를 활용하여 작성하였습니다.

Core Keeper Dedicated Server 상태를 주기적으로 확인하고 Discord Webhook으로 알림을 보내는 Python 기반 모니터링 도구입니다.

기존 한글 설명은 아래 링크에서도 볼 수 있습니다.

- https://ssoobaakbaaa.tistory.com/6

이 저장소는 두 가지 실행 방식을 제공합니다.

- `watchcore`: `systemd`와 `journalctl` 기반으로 호스트에서 직접 실행하는 스크립트
- `container/watchcore.py`: Docker 컨테이너 상태와 로그를 읽는 컨테이너 실행 방식

## 구성 파일

```text
.
├── watchcore
├── watchcore.service
├── requirements.txt
└── container
    ├── Dockerfile.watchcore
    ├── install
    └── watchcore.py
```

## 요구 사항

### 호스트 실행 방식

- Python 3
- `systemd`
- `journalctl`
- Discord Webhook URL

### 컨테이너 실행 방식

- Docker
- Discord Webhook URL
- 모니터링 대상 컨테이너 이름 또는 ID
- Docker 소켓 접근 권한

## 1. 호스트에서 실행하기

### 1.1 의존성 설치

```bash
pip3 install -r requirements.txt
```

### 1.2 설정

`watchcore` 파일에서 다음 값을 환경에 맞게 조정합니다.

- `WEBHOOK_URL`: Discord Webhook 주소
- `SERVICE_CK`: 모니터링할 Core Keeper 서비스 이름
- `SERVICE_WC`: watchcore 서비스 이름

환경 변수로 주입하려면 예를 들어 아래처럼 실행할 수 있습니다.

```bash
WEBHOOK_URL="<YOUR_DISCORD_WEBHOOK_URL>" SERVICE_CK="corekeeper" ./watchcore
```

### 1.3 실행 권한 부여

```bash
chmod +x watchcore
```

### 1.4 systemd 서비스 예시

아래 예시는 공개 저장소용 샘플입니다. 사용자명과 경로는 운영 환경에 맞게 바꿔야 합니다.

```ini
[Unit]
Description=Watchcore Service
After=network.target

[Service]
Type=simple
User=watchcore
WorkingDirectory=/opt/watchcore
ExecStartPre=/bin/sleep 5
ExecStart=/opt/watchcore/watchcore
KillMode=process
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

배포 시에는 저장소의 `watchcore.service` 파일을 복사해 수정하면 됩니다.

## 2. 컨테이너로 실행하기

### 2.1 설정

`container/watchcore.py`는 아래 환경 변수를 우선 사용합니다.

- `WEBHOOK_URL`: Discord Webhook 주소
- `TARGET_CONTAINER`: 모니터링 대상 Core Keeper 컨테이너 이름 또는 ID
- `WATCHCORE_CONTAINER`: watchcore 컨테이너 이름 또는 ID
- `GAME_ID_PATH`: 대상 컨테이너 내부의 `GameID.txt` 경로

기본값은 공개 저장소용 예시이므로 실제 운영 환경에서는 반드시 조정하는 편이 안전합니다.

### 2.2 이미지 빌드

```bash
cd container
docker build -t watchcore:latest -f Dockerfile.watchcore .
```

### 2.3 컨테이너 실행

```bash
docker run -d \
  --name watchcore \
  --restart unless-stopped \
  -e WEBHOOK_URL="<YOUR_DISCORD_WEBHOOK_URL>" \
  -e TARGET_CONTAINER="corekeeper" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  watchcore:latest
```

## 3. 동작 방식

### 호스트 실행 방식

1. `systemctl`로 Core Keeper 서비스 상태를 확인합니다.
2. `journalctl`에서 서버 ID와 최근 시작 시간을 읽습니다.
3. Discord Webhook 메시지를 생성하거나 기존 메시지를 갱신합니다.
4. watchcore가 종료되면 종료 상태를 다시 전송합니다.

### 컨테이너 실행 방식

1. Docker SDK로 대상 컨테이너 상태를 조회합니다.
2. 가능하면 `GameID.txt`를 직접 읽고, 실패하면 컨테이너 로그에서 대체 정보를 찾습니다.
3. Discord 전송 실패 횟수와 최근 오류를 요약해 로그에 남깁니다.
4. 상태가 바뀐 경우에만 메시지를 갱신합니다.

## 4. 모니터링 메시지

Discord로 전송되는 기본 정보는 다음과 같습니다.

- 서버 ID
- 서버 상태
- 최근 서버 시작 시간
- Watchcore 상태
- 마지막 조회 시간

## 5. 주의 사항

- Discord Webhook URL은 코드에 하드코딩하지 말고 환경 변수 또는 배포 전 치환 방식으로 관리하는 편이 안전합니다.
- 공개 저장소로 전환할 경우 기존 Git 히스토리에 비밀값이 남아 있는지 반드시 확인해야 합니다.
- Docker 방식은 `/var/run/docker.sock` 접근 권한이 필요하므로 운영 환경에서 권한 범위를 신중히 관리해야 합니다.
