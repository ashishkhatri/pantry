"""Expiry reminders: push notifications to every household phone that turned them on.

A background thread wakes every few minutes. Once a day, after the reminder hour
(Settings), it sends one notification listing items that will expire within the
warning window or have just expired. Each item is announced at most once per
stage ("expiring soon", then "expired"), so you don't hear about the same yogurt
every morning. Once a week it also sends a digest of everything still past its
date, as a nudge to check and toss or update.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import List, Optional

from . import db as dbm
from . import inventory
from .webpush import PushError, Vapid, send

log = logging.getLogger("pantry.notify")


def _names(items: List[dict], n: int = 4) -> str:
    names = [i["name"] for i in items]
    if len(names) <= n:
        return ", ".join(names)
    return ", ".join(names[:n]) + f" +{len(names) - n} more"


def push_all(con, vapid: Vapid, data: dict, user_id: Optional[int] = None) -> dict:
    """Send to every subscription (or one user's). Drops subscriptions the push service says are gone."""
    sql, args = "SELECT * FROM push_subs", []
    if user_id:
        sql += " WHERE user_id = ?"
        args.append(user_id)
    sent, failed, errors = 0, 0, []
    for s in con.execute(sql, args).fetchall():
        try:
            send(vapid, dict(s), data)
            sent += 1
            con.execute("UPDATE push_subs SET fail_count = 0 WHERE id = ?", (s["id"],))
        except PushError as e:
            failed += 1
            errors.append(str(e))
            if e.gone or s["fail_count"] >= 9:
                con.execute("DELETE FROM push_subs WHERE id = ?", (s["id"],))
            else:
                con.execute("UPDATE push_subs SET fail_count = fail_count + 1 WHERE id = ?", (s["id"],))
            log.warning("push to subscription %s failed: %s", s["id"], e)
    con.commit()
    return {"sent": sent, "failed": failed, "errors": errors[:3]}


def daily_message(con, ref: Optional[date] = None) -> Optional[dict]:
    """Build today's notification and mark the items as announced. None if there's nothing new."""
    ref = ref or date.today()
    wd = inventory.warn_days(con)
    items = inventory.list_items(con)
    expired = [i for i in items if i["expires_on"] and i["expires_on"] < ref.isoformat() and i["notified_stage"] < 2]
    soon_cut = (ref + timedelta(days=wd)).isoformat()
    soon = [i for i in items if i["expires_on"] and ref.isoformat() <= i["expires_on"] <= soon_cut
            and i["notified_stage"] < 1]
    if not expired and not soon:
        return None
    parts = []
    if soon:
        parts.append(f"Use soon: {_names(soon)}")
    if expired:
        parts.append(f"Past date: {_names(expired)}")
    title = (f"{len(soon)} item{'s' if len(soon) != 1 else ''} expiring soon" if soon else
             f"{len(expired)} item{'s' if len(expired) != 1 else ''} past date")
    if soon and expired:
        title += f", {len(expired)} past date"
    for i in soon:
        con.execute("UPDATE items SET notified_stage = 1 WHERE id = ?", (i["id"],))
    for i in expired:
        con.execute("UPDATE items SET notified_stage = 2 WHERE id = ?", (i["id"],))
    con.commit()
    return {"title": title, "body": ". ".join(parts), "url": "/#expiring", "tag": "pantry-daily"}


def weekly_message(con, ref: Optional[date] = None) -> Optional[dict]:
    ref = ref or date.today()
    expired = [i for i in inventory.list_items(con) if i["expires_on"] and i["expires_on"] < ref.isoformat()]
    if not expired:
        return None
    return {"title": f"{len(expired)} item{'s' if len(expired) != 1 else ''} still past date",
            "body": f"Check, then toss or update the date: {_names(expired, 5)}", "url": "/#expiring",
            "tag": "pantry-weekly"}


def tick(db_path: str, vapid: Vapid, now: Optional[datetime] = None) -> List[str]:
    """Run whatever is due. Returns what it did (for logs and tests)."""
    now = now or datetime.now()
    did = []
    con = dbm.connect(db_path)
    try:
        hour = int(dbm.get_setting(con, "notify_hour") or 9)
        today = now.date().isoformat()
        if now.hour >= hour and dbm.get_setting(con, "last_daily_run") != today:
            dbm.set_setting(con, "last_daily_run", today)
            con.commit()
            msg = daily_message(con, now.date())
            if msg:
                r = push_all(con, vapid, msg)
                did.append(f"daily: {msg['title']} ({r['sent']} sent)")
            else:
                did.append("daily: nothing new")
            weekly_day = int(dbm.get_setting(con, "notify_weekly_day") or 6)
            if now.weekday() == weekly_day and dbm.get_setting(con, "last_weekly_run") != today:
                dbm.set_setting(con, "last_weekly_run", today)
                con.commit()
                wmsg = weekly_message(con, now.date())
                if wmsg:
                    r = push_all(con, vapid, wmsg)
                    did.append(f"weekly: {wmsg['title']} ({r['sent']} sent)")
    finally:
        con.close()
    return did


def start(db_path: str, backup_dir: str, vapid: Vapid, interval: int = 300) -> threading.Thread:
    def loop():
        while True:
            try:
                for line in tick(db_path, vapid):
                    log.info(line)
                dbm.auto_backup(db_path, backup_dir)
            except Exception:
                log.exception("reminder check failed")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="pantry-notify", daemon=True)
    t.start()
    return t
