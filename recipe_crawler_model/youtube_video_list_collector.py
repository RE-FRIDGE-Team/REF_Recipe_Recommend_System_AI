"""
1·2단계 — 영상 목록 수집.

수집 경로 두 갈래:
  (1) 지정 채널의 업로드 재생목록 전체 순회  → playlistItems (1 unit/호출, 저렴)
  (2) 키워드 검색                            → search.list (100 units/호출, 비쌈)

쿼터 메모:
  YouTube Data API 기본 할당량은 하루 10,000 units.
  search.list 는 호출당 100 units 이라 하루 100회가 상한임.
  반면 채널 업로드 목록은 playlistItems(1 unit) 로 훨씬 싸므로,
  채널 기반 수집을 먼저 돌리고 검색은 보충용으로 쓰는 게 유리함.

중복 제거는 video_id 기준. 채널 수집분과 검색 수집분이 겹쳐도 한 번만 남김.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import crawler_config as C

logger = logging.getLogger(__name__)

_ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _client():
    """YouTube Data API 클라이언트 생성."""
    if not C.YOUTUBE_API_KEY:
        raise RuntimeError("환경변수 YOUTUBE_API_KEY 미설정")
    return build("youtube", "v3", developerKey=C.YOUTUBE_API_KEY, cache_discovery=False)


def _parse_duration(iso: str) -> int:
    """ISO8601 재생시간(PT1M30S)을 초 단위로 변환."""
    m = _ISO_DUR.fullmatch(iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def resolve_channel_id(yt, handle_or_id: str) -> str | None:
    """
    핸들(@xxx) 또는 채널ID 를 채널ID(UC...)로 정규화함.

    핸들은 channels.list(forHandle=...) 로 조회 가능 (1 unit).
    """
    if handle_or_id.startswith("UC"):
        return handle_or_id
    handle = handle_or_id.lstrip("@")
    try:
        res = yt.channels().list(part="id", forHandle=handle).execute()
        items = res.get("items", [])
        if items:
            return items[0]["id"]
        # forHandle 로 못 찾으면 검색으로 폴백 (100 units 소모)
        res = yt.search().list(part="snippet", q=handle, type="channel", maxResults=1).execute()
        items = res.get("items", [])
        return items[0]["snippet"]["channelId"] if items else None
    except HttpError as e:
        logger.warning("채널 해석 실패 %s: %s", handle_or_id, e)
        return None


def fetch_channel_videos(yt, channel_id: str, limit: int) -> list[dict]:
    """
    채널 업로드 재생목록을 순회해 영상 기본정보 수집 (playlistItems, 1 unit/페이지).
    """
    try:
        ch = yt.channels().list(part="contentDetails,snippet", id=channel_id).execute()
        items = ch.get("items", [])
        if not items:
            return []
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        ch_name = items[0]["snippet"]["title"]
    except HttpError as e:
        logger.warning("채널 조회 실패 %s: %s", channel_id, e)
        return []

    out, token = [], None
    while len(out) < limit:
        try:
            res = yt.playlistItems().list(
                part="snippet", playlistId=uploads,
                maxResults=min(50, limit - len(out)), pageToken=token,
            ).execute()
        except HttpError as e:
            logger.warning("업로드 목록 조회 실패: %s", e)
            break
        for it in res.get("items", []):
            sn = it["snippet"]
            vid = sn.get("resourceId", {}).get("videoId")
            if not vid:
                continue
            out.append({
                "video_id": vid,
                "video_title": sn.get("title", ""),
                "channel_name": ch_name,
                "channel_id": channel_id,
                "published_at": sn.get("publishedAt", ""),
                "source": "channel",
            })
        token = res.get("nextPageToken")
        if not token:
            break
    logger.info("채널 %s — %d개 수집", ch_name, len(out))
    return out


def search_videos(yt, query: str, limit: int) -> list[dict]:
    """
    키워드 검색으로 영상 수집 (search.list, 100 units/호출 — 아껴 쓸 것).
    """
    out, token = [], None
    while len(out) < limit:
        try:
            res = yt.search().list(
                part="snippet", q=query, type="video",
                maxResults=min(50, limit - len(out)),
                pageToken=token, relevanceLanguage="ko", regionCode="KR",
            ).execute()
        except HttpError as e:
            logger.warning("검색 실패 '%s': %s", query, e)
            break
        for it in res.get("items", []):
            sn = it["snippet"]
            out.append({
                "video_id": it["id"]["videoId"],
                "video_title": sn.get("title", ""),
                "channel_name": sn.get("channelTitle", ""),
                "channel_id": sn.get("channelId", ""),
                "published_at": sn.get("publishedAt", ""),
                "source": f"search:{query}",
            })
        token = res.get("nextPageToken")
        if not token:
            break
    logger.info("검색 '%s' — %d개 수집", query, len(out))
    return out


def enrich_and_filter(yt, videos: list[dict]) -> list[dict]:
    """
    videos.list 로 재생시간·조회수·설명을 채우고 필터 적용 (50개씩 배치, 1 unit).

    설명(description)을 여기서 받아두는 이유:
    레시피 전문이 고정댓글보다 설명란에 있는 경우가 훨씬 많아 1순위 추출원으로 씀.
    """
    enriched = []
    ids = [v["video_id"] for v in videos]
    by_id = {v["video_id"]: v for v in videos}

    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            res = yt.videos().list(
                part="contentDetails,statistics,snippet", id=",".join(chunk)
            ).execute()
        except HttpError as e:
            logger.warning("영상 상세 조회 실패: %s", e)
            continue
        for it in res.get("items", []):
            vid = it["id"]
            base = by_id.get(vid, {})
            dur = _parse_duration(it["contentDetails"].get("duration", ""))
            title = it["snippet"].get("title", base.get("video_title", ""))

            # 길이 필터 — 너무 짧거나 긴 영상 제외
            if not (C.MIN_DURATION_SEC <= dur <= C.MAX_DURATION_SEC):
                continue
            # 제목 키워드 필터 — 먹방/브이로그 등 레시피 아닌 영상 제외
            low = title.lower()
            if any(k.lower() in low for k in C.EXCLUDE_TITLE_KEYWORDS):
                continue

            base.update({
                "video_title": title,
                "channel_name": it["snippet"].get("channelTitle", base.get("channel_name", "")),
                "channel_id": it["snippet"].get("channelId", base.get("channel_id", "")),
                "published_at": it["snippet"].get("publishedAt", ""),
                "description": it["snippet"].get("description", ""),
                "duration_sec": dur,
                "view_count": int(it.get("statistics", {}).get("viewCount", 0) or 0),
                "video_url": f"https://www.youtube.com/watch?v={vid}",
            })
            enriched.append(base)

    logger.info("필터 통과 %d/%d", len(enriched), len(videos))
    return enriched


def fetch_pinned_comment(yt, video_id: str) -> tuple[str, bool]:
    """
    고정댓글 추정 텍스트와 '검증 여부'를 반환함.

    주의(중요):
      YouTube Data API v3 에는 isPinned 필드가 없음.
      따라서 order=relevance 의 첫 댓글이 고정댓글일 가능성이 높다는 휴리스틱을 씀.
      작성자가 채널 주인과 같으면 신뢰도가 높다고 보고 verified=True 로 표시함.
      (verified=False 인 값은 일반 댓글일 수 있으니 추출 시 신뢰도 낮게 취급)

    Returns:
        (댓글 텍스트, 채널주인 작성 여부)
    """
    try:
        res = yt.commentThreads().list(
            part="snippet", videoId=video_id, order="relevance",
            maxResults=5, textFormat="plainText",
        ).execute()
    except HttpError as e:
        logger.debug("댓글 조회 실패 %s: %s", video_id, e)
        return "", False

    items = res.get("items", [])
    if not items:
        return "", False

    # 영상 업로더 채널ID 확보 → 댓글 작성자와 대조
    owner_id = ""
    try:
        v = yt.videos().list(part="snippet", id=video_id).execute()
        if v.get("items"):
            owner_id = v["items"][0]["snippet"].get("channelId", "")
    except HttpError:
        pass

    first = items[0]["snippet"]["topLevelComment"]["snippet"]
    first_text = first.get("textOriginal", "")
    first_author = (first.get("authorChannelId") or {}).get("value", "")
    if owner_id and first_author == owner_id:
        return first_text, True

    # 첫 댓글이 업로더가 아니면, 상위 댓글 중 업로더 작성분을 탐색
    for it in items:
        sn = it["snippet"]["topLevelComment"]["snippet"]
        author = (sn.get("authorChannelId") or {}).get("value", "")
        if owner_id and author == owner_id:
            return sn.get("textOriginal", ""), True

    return first_text, False


def collect_all() -> list[dict]:
    """1·2단계 통합 실행 — 채널 수집 + 검색 수집 + video_id 중복 제거."""
    yt = _client()
    collected: dict[str, dict] = {}     # video_id → 레코드 (dict 자체가 중복 제거기)

    # (1) 채널 기반 — 저렴하므로 먼저
    for handle in C.TARGET_CHANNELS:
        cid = resolve_channel_id(yt, handle)
        if not cid:
            logger.warning("채널 해석 실패, 건너뜀: %s", handle)
            continue
        for v in fetch_channel_videos(yt, cid, C.MAX_VIDEOS_PER_CHANNEL):
            collected.setdefault(v["video_id"], v)     # 먼저 들어온 것 우선

    n_ch = len(collected)
    logger.info("채널 수집 소계: %d개", n_ch)

    # (2) 검색 기반 — 이미 있는 video_id 는 setdefault 로 자동 스킵
    for q in C.SEARCH_QUERIES:
        for v in search_videos(yt, q, C.MAX_RESULTS_PER_QUERY):
            collected.setdefault(v["video_id"], v)

    logger.info("검색 추가분: %d개 (총 %d개, 중복 제거됨)", len(collected) - n_ch, len(collected))

    # 상세 정보 보강 + 필터
    videos = enrich_and_filter(yt, list(collected.values()))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for v in videos:
        v["collected_at"] = now
    return videos
