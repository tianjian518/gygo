"""分享链接定时监控 —— 调度与差异检测。

单个监控项的一轮扫描：
  1. 免登录列举分享里的当前文件
  2. 与上次记录的 fid 集合取差集 → 只拿到新增的剧集
  3. 转存新增文件到目标目录
  4. 回写新的 fid 集合作为基线

失效处理：
  - 分享链接失效（过期/被取消）→ status=invalid，不再扫描，不影响其它项
  - 登录态失效 → status=paused，全部暂停，重新登录后自动恢复
  - 网络等临时错误 → status=error，下个周期自动重试
"""

import threading
import time

import monitor_store
from monitor_store import MIN_INTERVAL
from guangya import ApiError, TokenExpired
from share_gy import (ShareError, list_share_files, parse_share_input,
                      transfer_share_files)

# 与 app.py 解耦：启动时由 app 把自己的模块绑进来，避免循环 import
_CLIENT_GETTER = None
_EXPIRED_GETTER = None
_EXPIRE_SETTER = None


def bind(app_module, expired_setter=None):
    global _CLIENT_GETTER, _EXPIRED_GETTER, _EXPIRE_SETTER
    _CLIENT_GETTER = lambda: getattr(app_module, "CLIENT", None)
    _EXPIRED_GETTER = lambda: getattr(app_module, "AUTH_EXPIRED", False)
    _EXPIRE_SETTER = expired_setter


def _client():
    return _CLIENT_GETTER() if _CLIENT_GETTER else None


def _expired():
    return bool(_EXPIRED_GETTER()) if _EXPIRED_GETTER else False


def _mark_expired():
    if _EXPIRE_SETTER:
        try:
            _EXPIRE_SETTER(True)
        except Exception:
            pass


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def scan_one(monitor, on_phase=None):
    """扫描单个监控项一次。"""
    def _phase(ph, **kw):
        if callable(on_phase):
            try:
                on_phase(ph, kw)
            except Exception:
                pass

    mid = monitor["id"]
    client = _client()
    if client is None:
        return {"status": "error", "msg": "尚未登录"}
    if _expired():
        monitor_store.update(mid, status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}

    # 1) 解析目标目录（没有就沿途创建）
    _phase("解析目标目录")
    try:
        target_fid = client.resolve_path(monitor.get("target_path") or "")
    except TokenExpired:
        _mark_expired()
        monitor_store.update(mid, status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}
    except ApiError as e:
        return {"status": "error", "msg": "目录解析失败：%s" % e}

    # 2) 列举分享当前文件（免登录接口，登录过期也能扫）
    _phase("列举分享文件")
    try:
        files, _name = list_share_files(
            monitor["share_id"], monitor.get("passcode") or "",
            monitor.get("pdir_fid") or "", only_video=True)
    except ShareError as e:
        if e.fatal:
            monitor_store.update(mid, status="invalid",
                                 last_result="链接失效：" + e.message, last_scan=_now())
            return {"status": "invalid", "msg": e.message}
        monitor_store.update(mid, status="error",
                             last_result="列举失败：" + e.message, last_scan=_now())
        return {"status": "error", "msg": e.message}

    # 3) 差异检测
    _phase("差异检测")
    cur_ids = [f["fid"] for f in files if f.get("fid")]
    prev = set(monitor.get("last_files") or [])
    new_files = [f for f in files if f.get("fid") and f["fid"] not in prev]

    added = 0
    if new_files:
        _phase("转存新增", count=len(new_files))
        if _expired():
            monitor_store.update(mid, status="paused")
            return {"status": "paused", "msg": "登录已失效，暂停监控"}
        try:
            added = transfer_share_files(
                monitor["share_id"], monitor.get("passcode") or "",
                new_files, target_fid, client)
        except ShareError as e:
            if e.fatal:
                monitor_store.update(mid, status="invalid",
                                     last_result="转存失败(链接失效)：" + e.message,
                                     last_scan=_now())
                return {"status": "invalid", "msg": e.message}
            monitor_store.update(mid, status="error",
                                 last_result="转存失败：" + e.message, last_scan=_now())
            return {"status": "error", "msg": e.message}
        except TokenExpired:
            _mark_expired()
            monitor_store.update(mid, status="paused")
            return {"status": "paused", "msg": "登录已失效，暂停监控"}
        except ApiError as e:
            monitor_store.update(mid, status="error",
                                 last_result="转存失败：%s" % e, last_scan=_now())
            return {"status": "error", "msg": str(e)}

    summary = ("%s 扫描：新增 %d 个视频，已转存（共 %d）" % (_now(), added, len(files))
               if added else ("%s 扫描：无新增（共 %d）" % (_now(), len(files))))
    monitor_store.update(mid, last_files=cur_ids, last_scan=_now(),
                         status="ok", last_result=summary)
    _phase("完成", added=added)
    return {"status": "ok", "added": added, "total": len(files)}


def add_and_baseline(share_url, target_path, interval_min, pdir_fid=""):
    """新增监控项，并立刻拉一次链接建立基线（已存在的文件不会被转存）。"""
    share_id, passcode, parsed_pdir = parse_share_input(share_url)
    if not pdir_fid:
        pdir_fid = parsed_pdir

    mon = monitor_store.add({
        "share_url": share_url,
        "share_id": share_id,
        "passcode": passcode,
        "pdir_fid": pdir_fid or "",
        "target_path": target_path or "",
        "interval_min": interval_min,
    })

    # 建立基线：列一次分享，把当前所有文件记下来
    link_name = ""
    try:
        files, link_name = list_share_files(share_id, passcode, pdir_fid or "", only_video=True)
        monitor_store.update(mon["id"],
                             last_files=[f["fid"] for f in files if f.get("fid")],
                             link_name=link_name, last_scan=_now(), status="ok",
                             last_result="已添加，基线 %d 个视频（这些不会被转存）" % len(files))
    except ShareError as e:
        monitor_store.update(mon["id"], status="error" if not e.fatal else "invalid",
                             last_result="建立基线失败：" + e.message)
    except Exception as e:
        monitor_store.update(mon["id"], status="error",
                             last_result="建立基线失败：%s" % e)
    return monitor_store.get(mon["id"])


class MonitorScheduler(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            time.sleep(60)
            if _expired():
                continue
            now = time.time()
            for m in monitor_store.list_all():
                if not m.get("enabled"):
                    continue
                if m.get("status") == "invalid":
                    continue
                iv = max(MIN_INTERVAL, int(m.get("interval_min") or MIN_INTERVAL)) * 60
                last = m.get("last_scan")
                if not last:
                    due = True
                else:
                    try:
                        lt = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
                        due = (now - lt) >= iv
                    except Exception:
                        due = True
                if due:
                    try:
                        scan_one(m)
                    except Exception as e:
                        monitor_store.update(m["id"], status="error",
                                             last_result="扫描异常：%s" % e)

    def stop(self):
        self._stop.set()


_scheduler = None


def start_scheduler():
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = MonitorScheduler()
        _scheduler.start()
    return _scheduler
