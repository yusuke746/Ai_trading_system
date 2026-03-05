"""
core/broker_time.py — XMTサーバー時間管理

システム全体の datetime を XMTサーバー時間（Europe/Athens = EET/EEST）で統一する。
冬時間: GMT+2 (EET)  / 夏時間: GMT+3 (EEST)
欧州夏時間ルール: 3月最終日曜 → 10月最終日曜
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# XMTサーバー時間 = EET (Europe/Athens)
XMT_TZ = ZoneInfo("Europe/Athens")
UTC_TZ = timezone.utc

# セッション定義（XMT時間基準）
SESSIONS = [
    (0, 7, "SYDNEY_TOKYO"),
    (7, 9, "TOKYO_LONDON_OVERLAP"),
    (9, 12, "LONDON"),
    (12, 16, "LONDON_NY_OVERLAP"),
    (16, 22, "NEW_YORK"),
    (22, 24, "DEAD_ZONE"),
]


class BrokerTime:
    """XMTサーバー時間のユーティリティクラス（全メソッドstaticmethod）"""

    @staticmethod
    def now() -> datetime:
        """現在のXMT時刻を返す（タイムゾーン情報付き）"""
        return datetime.now(XMT_TZ)

    @staticmethod
    def now_str() -> str:
        """ログ用文字列（タイムゾーン表記付き）"""
        now = BrokerTime.now()
        offset = now.strftime("%z")  # +0200 or +0300
        tz_label = f"XMT{offset[:3]}"
        return now.strftime(f"%Y-%m-%d %H:%M:%S {tz_label}")

    @staticmethod
    def is_dst() -> bool:
        """現在夏時間かどうか"""
        now = BrokerTime.now()
        return bool(now.dst())

    @staticmethod
    def get_session() -> str:
        """現在の市場セッションを返す"""
        hour = BrokerTime.now().hour
        for start, end, name in SESSIONS:
            if start <= hour < end:
                return name
        return "UNKNOWN"

    @staticmethod
    def from_utc(utc_dt: datetime) -> datetime:
        """UTC → XMT変換"""
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=UTC_TZ)
        return utc_dt.astimezone(XMT_TZ)

    @staticmethod
    def to_utc(xmt_dt: datetime) -> datetime:
        """XMT → UTC変換（MT5 API用）"""
        if xmt_dt.tzinfo is None:
            xmt_dt = xmt_dt.replace(tzinfo=XMT_TZ)
        return xmt_dt.astimezone(UTC_TZ)

    @staticmethod
    def today_start() -> datetime:
        """今日のXMT 00:00:00 を返す（日次PnL計算用）"""
        now = BrokerTime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def is_market_open(symbol: str) -> bool:
        """銘柄ごとの取引可能判定"""
        from config import CONFIG

        now = BrokerTime.now()
        weekday = now.weekday()  # 0=月曜, 6=日曜

        # 土曜日は常にクローズ
        if weekday == 5:
            return False

        # 日曜日は weekly_open 以降のみ
        if weekday == 6:
            hours_cfg = CONFIG.MARKET_HOURS.get(symbol, {})
            open_str = hours_cfg.get("weekly_open", "23:05")
            open_h, open_m = map(int, open_str.split(":"))
            if now.hour < open_h or (now.hour == open_h and now.minute < open_m):
                return False

        # 日次クローズ時間帯チェック
        hours_cfg = CONFIG.MARKET_HOURS.get(symbol, {})
        close_start = hours_cfg.get("daily_close_start", "23:55")
        close_end = hours_cfg.get("daily_close_end", "00:05")
        cs_h, cs_m = map(int, close_start.split(":"))
        ce_h, ce_m = map(int, close_end.split(":"))

        current_minutes = now.hour * 60 + now.minute
        close_start_min = cs_h * 60 + cs_m
        close_end_min = ce_h * 60 + ce_m

        if close_end_min < close_start_min:
            # 日をまたぐ場合 (例: 23:55 - 00:05)
            if current_minutes >= close_start_min or current_minutes < close_end_min:
                return False
        else:
            if close_start_min <= current_minutes < close_end_min:
                return False

        return True

    @staticmethod
    def is_friday_cutoff() -> bool:
        """金曜エントリー停止時間か"""
        from config import CONFIG
        now = BrokerTime.now()
        return now.weekday() == 4 and now.hour >= CONFIG.FRIDAY_ENTRY_CUTOFF_HOUR

    @staticmethod
    def is_weekend() -> bool:
        """土日（市場クローズ中）か"""
        now = BrokerTime.now()
        weekday = now.weekday()
        if weekday == 5:  # 土曜
            return True
        if weekday == 6:  # 日曜
            # 23:05以降はオープン
            if now.hour < 23 or (now.hour == 23 and now.minute < 5):
                return True
        return False

    @staticmethod
    def is_near_daily_close(symbol: str) -> bool:
        """日次クローズ直前か（30分前から）"""
        from config import CONFIG

        now = BrokerTime.now()
        hours_cfg = CONFIG.MARKET_HOURS.get(symbol, {})
        close_start = hours_cfg.get("daily_close_start", "23:55")
        cs_h, cs_m = map(int, close_start.split(":"))

        current_minutes = now.hour * 60 + now.minute
        close_start_min = cs_h * 60 + cs_m

        diff = close_start_min - current_minutes
        if diff < 0:
            diff += 24 * 60

        return 0 <= diff <= 30

    @staticmethod
    def minutes_to_weekly_close() -> int:
        """週末クローズまでの残り分数（金曜以外は大きな値を返す）"""
        now = BrokerTime.now()
        if now.weekday() != 4:
            return 99999

        # 金曜 23:55 XMT がクローズ → 残り分数
        close_minutes = 23 * 60 + 55
        current_minutes = now.hour * 60 + now.minute
        return max(0, close_minutes - current_minutes)

    @staticmethod
    def is_holiday() -> bool:
        """祝日チェック（年末年始等）"""
        now = BrokerTime.now()
        month, day = now.month, now.day
        # クリスマス・元旦
        if (month == 12 and day == 25) or (month == 1 and day == 1):
            return True
        return False

    @staticmethod
    def is_dead_zone() -> bool:
        """DEAD_ZONE（XMT 22:00-23:59）か"""
        hour = BrokerTime.now().hour
        return hour >= 22
