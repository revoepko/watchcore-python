import docker
import requests
import schedule
import time
import logging
import signal
import sys
import os
import re
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 실행 설정
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "<YOUR_DISCORD_WEBHOOK_URL>"
TARGET_CONTAINER = os.getenv("TARGET_CONTAINER") or "corekeeper"
WATCHCORE_CONTAINER = os.getenv("WATCHCORE_CONTAINER") or os.getenv("HOSTNAME")
TARGET_STRINGS = ("Game ID:", "GameID:")
SESSION_STRINGS = ("Started session with info:",)
GAME_ID_PATH = os.getenv("GAME_ID_PATH") or "/home/steam/GameID.txt"
MESSAGE_ID = None
LAST_STATUS_MESSAGE = None
GAME_ID_MTIME = None
GAME_ID_CACHE = None
PENDING_STATUS_MESSAGE = None
LAST_SENT_AT = 0.0
CHECK_INTERVAL_SECONDS = 1
SEND_INTERVAL_SECONDS = 1
NEXT_SEND_AT = 0.0
SUMMARY_INTERVAL_SECONDS = 60
DISCORD_STATS = {
    "success": 0,
    "fail": 0,
    "reasons": {},
    "last_error": None,
}

# 로그 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# Docker SDK 초기화
try:
    client = docker.from_env()
except Exception as e:
    logging.error(f"Docker SDK 초기화 실패: {e}")
    sys.exit(1)

# 네트워크 재시도 설정
RETRY_STRATEGY = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "POST", "PATCH"]
)
HTTP_ADAPTER = HTTPAdapter(max_retries=RETRY_STRATEGY)
SESSION = requests.Session()
SESSION.mount("http://", HTTP_ADAPTER)
SESSION.mount("https://", HTTP_ADAPTER)
REQUEST_TIMEOUT = 10


def get_container(name_or_id):
    """이름 또는 ID로 컨테이너를 조회한다."""
    if not name_or_id:
        return None
    try:
        return client.containers.get(name_or_id)
    except docker.errors.NotFound:
        return None


def get_watchcore_container():
    """현재 watchcore 컨테이너 객체를 반환한다."""
    return get_container(WATCHCORE_CONTAINER)


def get_container_logs(container, tail=200):
    """컨테이너 로그 일부를 UTF-8 텍스트 줄 목록으로 반환한다."""
    try:
        raw = container.logs(tail=tail)
        return raw.decode("utf-8", errors="replace").splitlines()
    except Exception as e:
        logging.error(f"로그 파싱 중 오류: {e}")
        return []


def extract_message_id_from_log(log_line):
    """로그 한 줄에서 Discord 메시지 ID를 찾는다."""
    match = re.search(r"ID:\s*(\d+)", log_line)
    if match:
        return match.group(1)
    return None


def get_last_message_id_from_logs():
    """watchcore 컨테이너 로그에서 마지막 메시지 ID를 복원한다."""
    container = get_watchcore_container()
    if not container:
        return None
    lines = get_container_logs(container, tail=200)
    for line in reversed(lines):
        message_id = extract_message_id_from_log(line)
        if message_id:
            return message_id
    return None


def initialize_message_id():
    """기존 메시지 ID를 복원해 중복 전송을 줄인다."""
    global MESSAGE_ID
    if MESSAGE_ID is None:
        MESSAGE_ID = get_last_message_id_from_logs()
        if MESSAGE_ID:
            logging.info(f"기존 메시지 ID 복원: {MESSAGE_ID}")
        else:
            logging.info("기존 메시지 ID를 찾지 못했습니다.")


def is_message_id_valid(message_id):
    """현재 저장된 메시지 ID가 Discord 상에서 유효한지 확인한다."""
    try:
        response = SESSION.get(
            f"{WEBHOOK_URL}/messages/{message_id}", timeout=REQUEST_TIMEOUT
        )
        return response.status_code == 200
    except Exception as e:
        logging.error(f"메시지 유효성 확인 에러: {e}")
        return False


def get_server_id(container):
    """가능하면 파일에서, 실패하면 로그에서 서버 ID를 추출한다."""
    try:
        global GAME_ID_MTIME, GAME_ID_CACHE

        stat_result = container.exec_run(
            ["/bin/sh", "-lc", f"stat -c %Y {GAME_ID_PATH}"],
            user="steam"
        )
        if stat_result and stat_result.exit_code == 0:
            mtime = stat_result.output.decode("utf-8", errors="replace").strip()
            if mtime and mtime != GAME_ID_MTIME:
                exec_result = container.exec_run(
                    ["/bin/sh", "-lc", f"cat {GAME_ID_PATH}"],
                    user="steam"
                )
                if exec_result and exec_result.exit_code == 0:
                    value = exec_result.output.decode("utf-8", errors="replace").strip()
                    if value:
                        GAME_ID_MTIME = mtime
                        GAME_ID_CACHE = value
                        return value
            if GAME_ID_CACHE:
                return GAME_ID_CACHE

        logs = container.logs(tail=100).decode("utf-8", errors="replace")
        for line in reversed(logs.splitlines()):
            for token in SESSION_STRINGS:
                if token in line:
                    return line.split(token, 1)[1].strip()
            for token in TARGET_STRINGS:
                if token in line:
                    return line.split(token, 1)[1].strip()
        return None
    except Exception as e:
        logging.error(f"서버 ID 추출 중 오류: {e}")
        return None


def send_or_update_discord_message(content):
    """Discord 메시지를 생성하거나 갱신한다."""
    global MESSAGE_ID, NEXT_SEND_AT
    data = {"content": content}

    try:
        if MESSAGE_ID is None:
            initialize_message_id()

        if MESSAGE_ID and not is_message_id_valid(MESSAGE_ID):
            logging.warning("기존 메시지 ID가 유효하지 않아 새 메시지를 생성합니다.")
            MESSAGE_ID = None

        if MESSAGE_ID:
            response = SESSION.patch(
                f"{WEBHOOK_URL}/messages/{MESSAGE_ID}",
                json=data,
                timeout=REQUEST_TIMEOUT
            )
        else:
            response = SESSION.post(
                f"{WEBHOOK_URL}?wait=1",
                json=data,
                timeout=REQUEST_TIMEOUT
            )

        if response is not None:
            remaining = response.headers.get("X-RateLimit-Remaining")
            should_log = response.status_code >= 400
            try:
                if remaining is not None and int(remaining) <= 1:
                    should_log = True
            except ValueError:
                pass

            if should_log:
                rl_headers = {
                    "X-RateLimit-Limit": response.headers.get("X-RateLimit-Limit"),
                    "X-RateLimit-Remaining": remaining,
                    "X-RateLimit-Reset": response.headers.get("X-RateLimit-Reset"),
                    "X-RateLimit-Reset-After": response.headers.get("X-RateLimit-Reset-After"),
                    "X-RateLimit-Bucket": response.headers.get("X-RateLimit-Bucket"),
                    "Retry-After": response.headers.get("Retry-After"),
                    "X-RateLimit-Global": response.headers.get("X-RateLimit-Global"),
                }
                logging.info(f"Discord rate limit 헤더: {rl_headers}")

        if response.status_code in {200, 204}:
            if not MESSAGE_ID:
                MESSAGE_ID = response.json().get("id")
                logging.info(f"메시지 생성 성공 ID: {MESSAGE_ID}")
            DISCORD_STATS["success"] += 1
            return True

        if response.status_code == 404:
            MESSAGE_ID = None

        retry_after = response.headers.get("Retry-After")
        reset_after = response.headers.get("X-RateLimit-Reset-After")
        remaining = response.headers.get("X-RateLimit-Remaining")
        if response.status_code == 429 or (remaining is not None and remaining.strip() == "0"):
            wait_value = retry_after or reset_after
            if wait_value is not None:
                try:
                    wait_value = float(wait_value)
                    NEXT_SEND_AT = time.monotonic() + wait_value
                    logging.warning(f"Discord rate limit 대기: {wait_value}초")
                except ValueError:
                    pass

        reason = _map_discord_error_reason(response.status_code)
        _record_discord_failure(reason, f"HTTP {response.status_code}")
        logging.warning(f"Discord 통신 실패: {response.status_code}")
        return False
    except Exception as e:
        _record_discord_failure("네트워크/예외", str(e))
        logging.error(f"Discord 전송 에러: {e}")
        return False


def _record_discord_failure(reason, detail):
    """Discord 전송 실패 통계를 누적한다."""
    DISCORD_STATS["fail"] += 1
    DISCORD_STATS["reasons"][reason] = DISCORD_STATS["reasons"].get(reason, 0) + 1
    DISCORD_STATS["last_error"] = detail


def _map_discord_error_reason(status_code):
    """HTTP 상태 코드를 사람이 읽기 쉬운 사유로 변환한다."""
    if status_code == 429:
        return "API 한도 초과"
    if status_code == 404:
        return "웹훅 또는 메시지 없음"
    if status_code in (401, 403):
        return "인증 또는 권한 문제"
    if 500 <= status_code <= 599:
        return "Discord 서버 오류"
    return "기타 오류"


def check_service_status():
    """대상 컨테이너 상태를 읽어 Discord 상태 메시지를 만든다."""
    global LAST_STATUS_MESSAGE, PENDING_STATUS_MESSAGE

    try:
        container = client.containers.get(TARGET_CONTAINER)
        status = container.status

        if status == "running":
            server_id = get_server_id(container) or "시작 중..."
            corekeeper_status = "✅ Core Keeper 서버가 활성 상태입니다."
            start_time_raw = container.attrs["State"]["StartedAt"][:19]
            last_start_time = start_time_raw.replace("T", " ")
        else:
            server_id = "알 수 없음"
            corekeeper_status = "⛔ Core Keeper 서버가 비활성 상태입니다."
            last_start_time = "알 수 없음"

        watchcore_status = "Watchcore가 실행 중입니다."

        new_status_message = f"""**서버 ID**
```{server_id}``` 
**서버 상태**
```{corekeeper_status}``` 
**최근 서버 시작 시간**
```{last_start_time}``` 
**Watchcore 상태**
```{watchcore_status}```
**마지막 조회 시간**
```{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}```"""

        if new_status_message != LAST_STATUS_MESSAGE:
            PENDING_STATUS_MESSAGE = new_status_message
    except docker.errors.NotFound:
        logging.warning(f"컨테이너 '{TARGET_CONTAINER}'를 찾을 수 없습니다.")
        PENDING_STATUS_MESSAGE = f"🚨 **경고**: 컨테이너 '{TARGET_CONTAINER}'를 찾을 수 없습니다!"
    except Exception as e:
        logging.error(f"상태 확인 중 오류: {e}")


def flush_status_message():
    """보류 중인 상태 메시지를 전송 주기에 맞춰 Discord로 보낸다."""
    global LAST_STATUS_MESSAGE, PENDING_STATUS_MESSAGE, LAST_SENT_AT, NEXT_SEND_AT

    if not PENDING_STATUS_MESSAGE:
        return

    now = time.monotonic()
    if now < NEXT_SEND_AT:
        return
    if now - LAST_SENT_AT < SEND_INTERVAL_SECONDS:
        return

    if send_or_update_discord_message(PENDING_STATUS_MESSAGE):
        LAST_STATUS_MESSAGE = PENDING_STATUS_MESSAGE
        PENDING_STATUS_MESSAGE = None
        LAST_SENT_AT = now


def log_discord_summary():
    """최근 Discord 전송 성공/실패 통계를 로그로 남긴다."""
    reasons = DISCORD_STATS["reasons"]
    if reasons:
        reasons_text = ", ".join([f"{k} {v}회" for k, v in reasons.items()])
        reasons_text = f" (사유: {reasons_text})"
    else:
        reasons_text = ""

    last_error = DISCORD_STATS["last_error"]
    if last_error:
        last_error_text = f" / 마지막 오류: {last_error}"
    else:
        last_error_text = ""

    logging.info(
        "Discord 전송 요약(최근 60초): 성공 %d회 / 실패 %d회%s%s",
        DISCORD_STATS["success"],
        DISCORD_STATS["fail"],
        reasons_text,
        last_error_text,
    )

    DISCORD_STATS["success"] = 0
    DISCORD_STATS["fail"] = 0
    DISCORD_STATS["reasons"].clear()
    DISCORD_STATS["last_error"] = None


def signal_handler(sig, frame):
    """종료 신호를 받으면 종료 상태를 Discord에 남긴다."""
    logging.info("Watchcore 종료 중...")
    send_or_update_discord_message("**Watchcore 상태**\n```Watchcore가 종료되었습니다.```")
    sys.exit(0)


def watchdog():
    """메인 실행 루프."""
    logging.info(f"Watchdog 시작. 대상 컨테이너: {TARGET_CONTAINER}")
    initialize_message_id()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    schedule.every(CHECK_INTERVAL_SECONDS).seconds.do(check_service_status)
    schedule.every(SEND_INTERVAL_SECONDS).seconds.do(flush_status_message)
    schedule.every(SUMMARY_INTERVAL_SECONDS).seconds.do(log_discord_summary)
    check_service_status()
    flush_status_message()

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            logging.error(f"메인 루프 오류: {e}")
            time.sleep(5)


if __name__ == "__main__":
    watchdog()
