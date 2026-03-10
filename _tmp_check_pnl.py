from datetime import timedelta, timezone, datetime
import MetaTrader5 as mt5
from config import CONFIG
from core.broker_time import BrokerTime

ok = mt5.initialize(login=CONFIG.MT5_LOGIN, server=CONFIG.MT5_SERVER, password=CONFIG.MT5_PASSWORD)
print('init', ok)
if not ok:
    print('last_error', mt5.last_error())
    raise SystemExit

now_xmt = BrokerTime.now()
today_start = BrokerTime.today_start()
from_utc = (now_xmt - timedelta(days=3)).astimezone(timezone.utc)
to_utc = (now_xmt + timedelta(minutes=5)).astimezone(timezone.utc)
deals = mt5.history_deals_get(from_utc.replace(tzinfo=None), to_utc.replace(tzinfo=None))
print('deals_count', 0 if deals is None else len(deals))
realized = 0.0
if deals:
    for d in deals[-12:]:
        ts_xmt = BrokerTime.from_utc(datetime.fromtimestamp(d.time, timezone.utc))
        pnl = d.profit + d.swap + d.commission
        print('tail', d.ticket, d.symbol, d.entry, ts_xmt.strftime('%m-%d %H:%M:%S'), round(pnl,2))
    for d in deals:
        if d.entry not in (1,2):
            continue
        ts_xmt = BrokerTime.from_utc(datetime.fromtimestamp(d.time, timezone.utc))
        if today_start <= ts_xmt <= now_xmt + timedelta(minutes=5):
            realized += d.profit + d.swap + d.commission
print('today_start', today_start)
print('now_xmt', now_xmt)
print('realized_today', round(realized,2))
mt5.shutdown()
