"""
collectors.py — 영상 목록 수집 (1·2단계).

확장 구조:
    BaseCollector 를 상속해 새 수집 경로(재생목록, 다른 플랫폼 등)를 추가할 수 있음.
    COLLECTOR_REGISTRY 에 등록하면 파이프라인이 자동으로 인식함.

쿼터 메모:
    YouTube Data API 기본 할당량은 하루 10,000 units.
    search.list 는 호출당 100 units(하루 100회 상한), playlistItems 는 1 unit.
    그래서 채널 업로드 목록을 먼저 훑고 검색은 보충용으로 씀.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import settings as S

logger = logging.getLogger(__name__)

_ISO_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def build_client():
    """YouTube Data API 클라이언트 생성."""
    if not S.YOUTUBE_API_KEY:
        raise RuntimeError(
            "YOUTUBE_API_KEY 미설정. .env 파일이나 환경변수로 주입할 것."
        )
    return build("youtube", "v3", developerKey=S.YOUTUBE_API_KEY,
                 cache_discovery=False)


def parse_duration(iso: str) -> int:
    """ISO8601 재생시간(PT1M30S) → 초."""
    m = _ISO_DURATION.fullmatch(iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


# ══════════════════════════════════════════════════════════════════
# 수집기 인터페이스
# ══════════════════════════════════════════════════════════════════
class BaseCollector(ABC):
    """
    영상 목록 수집기 공통 인터페이스.

    새 수집 경로를 만들 때 이 클래스를 상속하고 collect() 만 구현하면 됨.
    반환 형식은 아래 키를 가진 dict 리스트로 통일함:
        video_id, video_title, channel_name, channel_id, published_at, source
    """

    name: str = "base"

    def __init__(self, client) -> None:
        self.client = client

    @abstractmethod
    def collect(self) -> list[dict]:
        """영상 기본정보 리스트 반환."""
        raise NotImplementedError


class ChannelCollector(BaseCollector):
    """지정 채널의 업로드 재생목록을 순회 (1 unit/페이지, 저렴)."""

    name = "channel"

    def __init__(self, client, handles: list[str], limit_per_channel: int) -> None:
        super().__init__(client)
        self.handles = handles
        self.limit = limit_per_channel

    def _resolve_channel_id(self, handle_or_id: str) -> str | None:
        """핸들(@xxx) → 채널ID(UC...) 정규화."""
        if handle_or_id.startswith("UC"):
            return handle_or_id
        handle = handle_or_id.lstrip("@")
        try:
            res = self.client.channels().list(part="id", forHandle=handle).execute()
            items = res.get("items", [])
            if items:
                return items[0]["id"]
            # forHandle 로 못 찾으면 검색 폴백 (100 units 소모)
            res = self.client.search().list(
                part="snippet", q=handle, type="channel", maxResults=1
            ).execute()
            items = res.get("items", [])
            return items[0]["snippet"]["channelId"] if items else None
        except HttpError as e:
            logger.warning("채널 해석 실패 %s: %s", handle_or_id, e)
            return None

    def _fetch_uploads(self, channel_id: str) -> list[dict]:
        try:
            ch = self.client.channels().list(
                part="contentDetails,snippet", id=channel_id
            ).execute()
            items = ch.get("items", [])
            if not items:
                return []
            uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
            ch_name = items[0]["snippet"]["title"]
        except HttpError as e:
            logger.warning("채널 조회 실패 %s: %s", channel_id, e)
            return []

        out: list[dict] = []
        token = None
        while len(out) < self.limit:
            try:
                res = self.client.playlistItems().list(
                    part="snippet", playlistId=uploads,
                    maxResults=min(50, self.limit - len(out)), pageToken=token,
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
        logger.info("채널 %s — %d개", ch_name, len(out))
        return out

    def collect(self) -> list[dict]:
        results: list[dict] = []
        for handle in self.handles:
            cid = self._resolve_channel_id(handle)
            if not cid:
                logger.warning("채널 해석 실패, 건너뜀: %s", handle)
                continue
            results.extend(self._fetch_uploads(cid))
        return results


class SearchCollector(BaseCollector):
    """키워드 검색 (100 units/호출 — 아껴 쓸 것)."""

    name = "search"

    def __init__(self, client, queries: list[str], limit_per_query: int) -> None:
        super().__init__(client)
        self.queries = queries
        self.limit = limit_per_query

    def _search(self, query: str) -> list[dict]:
        out: list[dict] = []
        token = None
        while len(out) < self.limit:
            try:
                res = self.client.search().list(
                    part="snippet", q=query, type="video",
                    maxResults=min(50, self.limit - len(out)),
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
        logger.info("검색 '%s' — %d개", query, len(out))
        return out

    def collect(self) -> list[dict]:
        results: list[dict] = []
        for q in self.queries:
            results.extend(self._search(q))
        return results


# 새 수집기를 만들면 여기 등록 → 파이프라인이 자동 사용
COLLECTOR_REGISTRY: dict[str, type[BaseCollector]] = {
    ChannelCollector.name: ChannelCollector,
    SearchCollector.name: SearchCollector,
}


# ══════════════════════════════════════════════════════════════════
# 보강 및 필터
# ══════════════════════════════════════════════════════════════════
def enrich_and_filter(client, videos: list[dict]) -> list[dict]:
    """
    videos.list 로 재생시간·조회수·설명란을 채우고 필터 적용 (50개 배치, 1 unit).

    설명란(description)을 이 단계에서 받아두는 이유:
    레시피 정리본이 설명란에 있는 경우가 가장 많아 1순위 추출원이기 때문.
    별도 API 호출 없이 여기서 같이 확보됨.
    """
    by_id = {v["video_id"]: v for v in videos}
    ids = list(by_id)
    enriched: list[dict] = []

    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            res = client.videos().list(
                part="contentDetails,statistics,snippet", id=",".join(chunk)
            ).execute()
        except HttpError as e:
            logger.warning("영상 상세 조회 실패: %s", e)
            continue

        for it in res.get("items", []):
            vid = it["id"]
            base = by_id.get(vid, {})
            sn = it["snippet"]
            dur = parse_duration(it["contentDetails"].get("duration", ""))
            title = sn.get("title", base.get("video_title", ""))

            if not (S.MIN_DURATION_SEC <= dur <= S.MAX_DURATION_SEC):
                continue
            low = title.lower()
            if any(k.lower() in low for k in S.EXCLUDE_TITLE_KEYWORDS):
                continue

            base.update({
                "video_title": title,
                "channel_name": sn.get("channelTitle", base.get("channel_name", "")),
                "channel_id": sn.get("channelId", base.get("channel_id", "")),
                "published_at": sn.get("publishedAt", ""),
                "recipe_desc": sn.get("description", ""),
                "duration_sec": dur,
                "view_count": int(it.get("statistics", {}).get("viewCount", 0) or 0),
                "video_url": f"https://www.youtube.com/watch?v={vid}",
            })
            enriched.append(base)

    logger.info("필터 통과 %d/%d", len(enriched), len(videos))
    return enriched


def collect_videos(config) -> list[dict]:
    """
    1·2단계 통합 — 채널 수집 + 검색 수집 + video_id 중복 제거 + 상세 보강.

    중복 제거는 dict 키(video_id)로 처리하므로 채널·검색 결과가 겹쳐도
    한 번만 남음. 먼저 들어온 쪽(채널)이 우선됨.
    """
    client = build_client()
    merged: dict[str, dict] = {}

    # 저렴한 채널 수집 먼저
    channel_collector = ChannelCollector(
        client, config.channels, S.MAX_VIDEOS_PER_CHANNEL
    )
    for v in channel_collector.collect():
        merged.setdefault(v["video_id"], v)
    n_channel = len(merged)
    logger.info("채널 수집 소계: %d개", n_channel)

    # 검색 보충 (겹치면 setdefault 가 자동 스킵)
    search_collector = SearchCollector(
        client, config.queries, S.MAX_RESULTS_PER_QUERY
    )
    for v in search_collector.collect():
        merged.setdefault(v["video_id"], v)
    logger.info("검색 추가분: %d개 (총 %d개)", len(merged) - n_channel, len(merged))

    videos = enrich_and_filter(client, list(merged.values()))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for v in videos:
        v["collected_at"] = now
    return videos


def fetch_pinned_comment(client, video_id: str, owner_id: str = "") -> tuple[str, bool]:
    """
    고정댓글 추정 텍스트와 업로더 작성 여부를 반환.

    주의:
        YouTube Data API v3 에는 isPinned 필드가 없음.
        order=relevance 의 첫 댓글이 고정댓글일 가능성이 높다는 휴리스틱을 쓰고,
        작성자가 채널 주인과 일치하면 신뢰도가 높다고 보아 True 로 표시함.
        False 인 값은 일반 댓글일 수 있으니 추출 시 신뢰도를 낮춰 취급할 것.
    """
    try:
        res = client.commentThreads().list(
            part="snippet", videoId=video_id, order="relevance",
            maxResults=5, textFormat="plainText",
        ).execute()
    except HttpError as e:
        logger.debug("댓글 조회 실패 %s: %s", video_id, e)
        return "", False

    items = res.get("items", [])
    if not items:
        return "", False

    # 업로더 작성 댓글 우선 탐색
    for it in items:
        sn = it["snippet"]["topLevelComment"]["snippet"]
        author = (sn.get("authorChannelId") or {}).get("value", "")
        if owner_id and author == owner_id:
            return sn.get("textOriginal", ""), True

    # 업로더 댓글이 없으면 첫 댓글을 미검증 상태로 반환
    first = items[0]["snippet"]["topLevelComment"]["snippet"]
    return first.get("textOriginal", ""), False
