import json
import os
import re
import sys
from pathlib import Path

import requests

API_BASE = "https://www.googleapis.com/youtube/v3"
API_KEY = os.environ["YOUTUBE_API_KEY"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCQmcltnre6aG9SkDRYZqFIg")
STATE_PATH = Path("state.json")
THRESHOLD = 100_000

# 키워드 자동 판별은 편리하지만 완벽하지 않습니다.
# 누락된 커버 영상 ID는 force_include.json에, 제외할 영상 ID는
# force_exclude.json에 추가할 수 있습니다.
COVER_KEYWORDS = [
    "cover", "커버", "歌ってみた", "불러보았다", "불러봤습니다",
    "불러봤어요", "노래 커버", "covered by", "vocal cover"
]
EXCLUDE_KEYWORDS = [
    "#shorts", "shorts", "잡담", "다시보기", "생방송", "라이브 다시보기",
    "full stream", "just chatting", "gameplay", "게임 플레이"
]

def api_get(endpoint, params):
    params = dict(params)
    params["key"] = API_KEY
    response = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()

def get_upload_playlist_id():
    data = api_get("channels", {
        "part": "contentDetails",
        "id": CHANNEL_ID
    })
    items = data.get("items", [])
    if not items:
        raise RuntimeError(
            "채널 ID를 확인할 수 없습니다. YOUTUBE_CHANNEL_ID 설정을 확인하세요."
        )
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

def get_all_upload_ids():
    playlist_id = get_upload_playlist_id()
    ids = []
    page_token = None

    while True:
        params = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50
        }
        if page_token:
            params["pageToken"] = page_token
        data = api_get("playlistItems", params)

        for item in data.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                ids.append(video_id)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return ids

def get_video_details(video_ids):
    videos = []
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start:start + 50]
        data = api_get("videos", {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(batch),
            "maxResults": 50
        })
        videos.extend(data.get("items", []))
    return videos

def duration_seconds(value):
    match = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        value or ""
    )
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0)

def read_id_set(filename):
    path = Path(filename)
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")))

def is_cover(video, force_include, force_exclude):
    video_id = video["id"]
    if video_id in force_exclude:
        return False
    if video_id in force_include:
        return True

    snippet = video.get("snippet", {})
    title = snippet.get("title", "")
    description = snippet.get("description", "")
    text = f"{title}\n{description}".casefold()

    if any(word.casefold() in text for word in EXCLUDE_KEYWORDS):
        return False
    if not any(word.casefold() in text for word in COVER_KEYWORDS):
        return False

    # Shorts 형식으로 흔히 쓰이는 60초 이하 영상 제외
    if duration_seconds(video.get("contentDetails", {}).get("duration")) <= 60:
        return False

    # 현재 진행 중인 라이브는 제외
    if snippet.get("liveBroadcastContent") == "live":
        return False

    return True

def load_state():
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))

def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8"
    )

def send_discord(title, views, milestone, previous, video_id):
    message = (
        "🎉 **아오쿠모 린 커버곡 조회수 달성!**\n"
        f"**곡 제목:** {title}\n"
        f"**현재 조회수:** {views:,}회\n"
        f"**달성 구간:** {milestone:,}회\n"
        f"**이전 조회수:** {previous:,}회\n"
        f"**유튜브 링크:** https://www.youtube.com/watch?v={video_id}"
    )
    response = requests.post(
        DISCORD_WEBHOOK,
        json={
            "username": "아오쿠모 린 조회수 알림",
            "content": message,
            "allowed_mentions": {"parse": []}
        },
        timeout=30
    )
    response.raise_for_status()

def main():
    force_include = read_id_set("force_include.json")
    force_exclude = read_id_set("force_exclude.json")

    state = load_state()
    video_ids = get_all_upload_ids()
    videos = get_video_details(video_ids)

    cover_count = 0
    baseline_count = 0
    alert_count = 0

    for video in videos:
        if not is_cover(video, force_include, force_exclude):
            continue

        video_id = video["id"]
        title = video.get("snippet", {}).get("title", "(제목 없음)")
        stats = video.get("statistics", {})
        if "viewCount" not in stats:
            print(f"[미확인] 조회수를 가져올 수 없음: {title} ({video_id})")
            continue

        views = int(stats["viewCount"])
        cover_count += 1

        # 최초 실행 또는 새로 발견한 커버곡은 현재값을 기준으로만 저장
        if video_id not in state:
            state[video_id] = {
                "title": title,
                "views": views,
                "alerted_through": views // THRESHOLD
            }
            baseline_count += 1
            print(f"[기준 등록] {title}: {views:,}회")
            continue

        record = state[video_id]
        previous = int(record.get("views", views))
        alerted = int(record.get("alerted_through", previous // THRESHOLD))

        # 조회수가 상승했을 때 새로 통과한 모든 10만 단위 구간을 알림
        if views > previous:
            current_milestone = views // THRESHOLD
            while alerted < current_milestone:
                alerted += 1
                milestone = alerted * THRESHOLD
                send_discord(title, views, milestone, previous, video_id)
                alert_count += 1
                print(f"[알림 발송] {title}: {milestone:,}회")

        # 조회수 하락 시에도 이미 알린 구간은 되돌리지 않음
        record["title"] = title
        record["views"] = views
        record["alerted_through"] = alerted
        state[video_id] = record

    save_state(state)
    print(
        f"완료: 커버 후보 {cover_count}개 / "
        f"신규 기준 등록 {baseline_count}개 / 알림 {alert_count}건"
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"실행 실패: {exc}", file=sys.stderr)
        raise
