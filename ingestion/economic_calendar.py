"""
ingestion/economic_calendar.py — 経済指標カレンダー

Forex FactoryのRSSから高重要度（★★★）の指標を取得。
エントリー停止フラグの管理に使用。
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from xml.etree import ElementTree

import httpx

from core.broker_time import BrokerTime, XMT_TZ

logger = logging.getLogger(__name__)

RSS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
TARGET_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "XAU"}
CACHE_TTL_SEC = 3600  # 1時間

_events_cache: list[dict] = []
_cache_updated_at: Optional[datetime] = None


class EconomicCalendar:
    """経済指標カレンダー管理"""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self):
        self._client = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        if self._client:
            await self._client.aclose()

    async def _fetch_events(self) -> list[dict]:
        """Forex FactoryのRSSから指標を取得してパース"""
        global _events_cache, _cache_updated_at

        now = datetime.utcnow()
        if (
            _cache_updated_at
            and (now - _cache_updated_at).total_seconds() < CACHE_TTL_SEC
            and _events_cache
        ):
            return _events_cache

        try:
            if not self._client:
                await self.start()

            response = await self._client.get(RSS_URL)
            response.raise_for_status()

            root = ElementTree.fromstring(response.text)
            events = []

            for event_el in root.findall(".//event"):
                try:
                    title = event_el.findtext("title", "")
                    country = event_el.findtext("country", "")
                    impact = event_el.findtext("impact", "")
                    date_str = event_el.findtext("date", "")
                    time_str = event_el.findtext("time", "")

                    # 高重要度（High）のみ
                    if impact.lower() != "high":
                        continue

                    # 対象通貨のみ
                    if country.upper() not in TARGET_CURRENCIES:
                        continue

                    # 時間パース（EST → XMT変換）
                    event_time = self._parse_event_time(date_str, time_str)

                    events.append({
                        "title": title,
                        "currency": country.upper(),
                        "impact": impact,
                        "time_xmt": event_time,
                        "time_str": event_time.strftime("%H:%M") if event_time else "TBD",
                    })
                except Exception as e:
                    logger.debug(f"イベントパースエラー: {e}")
                    continue

            _events_cache = events
            _cache_updated_at = now
            logger.info(f"経済指標取得完了: {len(events)}件の高重要度イベント")
            return events

        except Exception as e:
            logger.error(f"経済指標取得失敗: {e}")
            return _events_cache  # キャッシュがあればそれを返す

    def _parse_event_time(self, date_str: str, time_str: str) -> Optional[datetime]:
        """EST時間をXMT時間に変換"""
        try:
            if not date_str or not time_str or time_str.lower() in ("", "all day", "tentative"):
                return None

            # Forex Factory は "MM-DD-YYYY" + "HH:MMam/pm" (EST)
            from zoneinfo import ZoneInfo

            dt_str = f"{date_str} {time_str}"
            # 複数フォーマットを試行
            for fmt in ("%m-%d-%Y %I:%M%p", "%m-%d-%Y %H:%M"):
                try:
                    est_dt = datetime.strptime(dt_str, fmt)
                    est_tz = ZoneInfo("America/New_York")
                    est_dt = est_dt.replace(tzinfo=est_tz)
                    xmt_dt = est_dt.astimezone(XMT_TZ)
                    return xmt_dt
                except ValueError:
                    continue

            return None
        except Exception:
            return None

    async def is_high_impact_event_soon(
        self, minutes_ahead: int = 30
    ) -> tuple[bool, str]:
        """今から指定分以内に重要指標があるか"""
        try:
            events = await self._fetch_events()
            now = BrokerTime.now()

            for event in events:
                event_time = event.get("time_xmt")
                if event_time is None:
                    continue

                # タイムゾーンが付いていない場合付与
                if event_time.tzinfo is None:
                    event_time = event_time.replace(tzinfo=XMT_TZ)

                diff = (event_time - now).total_seconds()
                if 0 <= diff <= minutes_ahead * 60:
                    desc = f"{event['title']} ({event['currency']}) {event['time_str']} XMT"
                    return True, desc

            return False, ""
        except Exception as e:
            logger.error(f"指標チェックエラー: {e}")
            return False, ""  # エラー時は安全側にFalse

    async def get_todays_events(self) -> list[dict]:
        """今日の重要指標一覧（日次レポート用）"""
        try:
            events = await self._fetch_events()
            now = BrokerTime.now()
            today = now.date()

            todays = []
            for event in events:
                event_time = event.get("time_xmt")
                if event_time and event_time.date() == today:
                    todays.append(event)

            return todays
        except Exception as e:
            logger.error(f"今日の指標取得エラー: {e}")
            return []
