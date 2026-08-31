"""gygo —— 光鸭云盘分享链接追更监控。

纯标准库 Web 服务，浏览器里用鼠标操作，不需要命令行。
默认端口 5099（避开 casgen 的 5000 和 139cas 的 5244）。
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gygo_log
import monitor
import monitor_store
from guangya import ApiError, GuangyaClient, TokenExpired, ROOT_FID
from share_gy import ShareError

PORT = int(os.environ.get("PORT") or os.environ.get("GYGO_PORT") or 5099)
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.2.0"

# 全局状态
CLIENT = None            # GuangyaClient 实例
AUTH_EXPIRED = False     # 登录态是否失效
AUTH_STATE = {}          # 短信登录过程的中间态（captcha_token / verification_id）
_lock = threading.Lock()


def _set_expired(val):
    global AUTH_EXPIRED
    AUTH_EXPIRED = bool(val)


def _client_from_auth():
    """启动时尝试用已保存的登录态恢复会话。"""
    auth = monitor_store.load_auth()
    if not auth.get("access_token"):
        return None
    c = GuangyaClient(access_token=auth.get("access_token") or "",
                      refresh_token=auth.get("refresh_token") or "",
                      device_id=auth.get("device_id") or "",
                      phone=auth.get("phone") or "")
    try:
        c.ensure_login()
        return c
    except Exception:
        # 探活失败就试着刷新一次
        if c.do_refresh():
            try:
                c.ensure_login()
                return c
            except Exception:
                pass
    return None


def _persist(c):
    monitor_store.save_auth(access_token=c.access_token,
                            refresh_token=c.refresh_token,
                            device_id=c.device_id,
                            phone=c.phone)


# ------------------------------------------------------------------ 业务动作

def act_sms_send(phone):
    """发送短信验证码，返回提示语。"""
    global CLIENT
    if not phone:
        raise ApiError("请填写手机号")
    c = CLIENT or GuangyaClient()
    captcha_token = c.sms_init(phone)
    vid = c.sms_send(phone, captcha_token)
    with _lock:
        AUTH_STATE["phone"] = phone
        AUTH_STATE["captcha_token"] = captcha_token
        AUTH_STATE["verification_id"] = vid
        if not CLIENT:
            AUTH_STATE["device_id"] = c.device_id
    return "验证码已发送到 %s" % phone


def act_sms_login(phone, code):
    """用短信验证码完成登录。"""
    global CLIENT, AUTH_EXPIRED
    st = dict(AUTH_STATE)
    if not st.get("verification_id") or not st.get("captcha_token"):
        raise ApiError("请先点击「发送验证码」")
    if st.get("phone") and phone and st["phone"] != phone:
        raise ApiError("手机号与发送验证码时不一致，请重新发送")

    c = CLIENT or GuangyaClient(device_id=st.get("device_id") or "")
    vtoken = c.sms_verify(st["verification_id"], code)
    c.sms_signin(st.get("phone") or phone, code, vtoken, st["captcha_token"])

    CLIENT = c
    AUTH_EXPIRED = False
    _persist(c)
    monitor.bind(sys.modules[__name__], _set_expired)
    monitor.start_scheduler()
    # 登录成功后，把之前因登录失效暂停的项恢复
    for m in monitor_store.list_all():
        if m.get("status") == "paused":
            monitor_store.update(m["id"], status="ok")
    return {"ok": True, "phone": c.phone}


def act_logout():
    global CLIENT, AUTH_EXPIRED
    CLIENT = None
    AUTH_EXPIRED = False
    with _lock:
        AUTH_STATE.clear()
    monitor_store.save_auth(access_token="", refresh_token="", phone="")
    return {"ok": True}


def _need_client():
    if CLIENT is None:
        raise ApiError("请先登录光鸭云盘")
    if AUTH_EXPIRED:
        raise ApiError("登录已失效，请重新登录")
    return CLIENT


def act_add_monitor(share_url, target_path, interval_min, **opts):
    if not share_url:
        raise ApiError("请填写分享链接")
    # 未登录也能添加（先攒链接，登录后首次扫描会自动转存）。transfer_existing
    # 默认 True：添加即把当前已有视频也转存进来；设为 False 则只追新不重转。
    transfer_existing = bool(opts.pop("transfer_existing", True))
    mon = monitor.add_and_baseline(
        share_url, target_path, interval_min,
        transfer_existing=transfer_existing, client=CLIENT, **opts)
    return {"ok": True, "monitor": mon}


EDITABLE = ("target_path", "interval_min", "keep_tree", "include_kw",
            "exclude_kw", "min_size_mb", "enabled")


def act_update_monitor(mid, patch):
    """改监控项配置。改目标路径不会重转已转存的文件（基线还在）。"""
    m = monitor_store.get(mid)
    if not m:
        raise ApiError("监控项不存在")
    clean = {}
    for k in EDITABLE:
        if k in patch and patch[k] is not None:
            clean[k] = patch[k]
    if "interval_min" in clean:
        try:
            clean["interval_min"] = max(monitor_store.MIN_INTERVAL,
                                        int(clean["interval_min"]))
        except (TypeError, ValueError):
            clean.pop("interval_min")
    if "min_size_mb" in clean:
        try:
            clean["min_size_mb"] = max(0, int(clean["min_size_mb"]))
        except (TypeError, ValueError):
            clean.pop("min_size_mb")
    if "keep_tree" in clean:
        clean["keep_tree"] = bool(clean["keep_tree"])
    if "enabled" in clean:
        clean["enabled"] = bool(clean["enabled"])
    monitor_store.update(mid, **clean)
    gygo_log.info("修改监控项", id=mid, fields=",".join(clean.keys()))
    return {"ok": True, "monitor": monitor_store.get(mid)}


def act_reset_baseline(mid):
    """清空基线。下一轮扫描会把分享里所有文件当成"新出的"重新转存。

    配合 MAX_PER_SCAN 分批，不会一次性灌爆。
    """
    m = monitor_store.get(mid)
    if not m:
        raise ApiError("监控项不存在")
    monitor_store.update(mid, last_files=[], status="ok",
                         last_result="基线已清空，下一轮将重新转存全部文件")
    gygo_log.warn("基线已重置，下一轮将全量重转", id=mid,
                  name=m.get("link_name") or "")
    return {"ok": True, "monitor": monitor_store.get(mid)}


def act_browse_dirs(fid):
    """浏览自己网盘的目录，用来选转存目标。"""
    c = _need_client()
    items = c.list_dir(fid if fid not in (None, "") else ROOT_FID)
    return {"ok": True, "fid": fid or ROOT_FID,
            "items": [{"fid": it.get("fid"), "name": it.get("name"),
                       "dir": bool(it.get("dir")), "size": it.get("size") or 0}
                      for it in items if it.get("dir")]}


def act_scan(mid):
    _need_client()
    m = monitor_store.get(mid)
    if not m:
        raise ApiError("监控项不存在")
    phases = []
    result = monitor.scan_one(m, on_phase=lambda ph, kw: phases.append(ph))
    return {"ok": result.get("status") in ("ok",), "result": result,
            "phases": phases, "monitor": monitor_store.get(mid)}


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "gygo/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("GYGO_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # -- routes

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(500, {"ok": False, "msg": "index.html 读取失败"})
            return

        if path == "/api/status":
            return self._send(200, {
                "ok": True,
                "logged": CLIENT is not None and not AUTH_EXPIRED,
                "expired": AUTH_EXPIRED,
                "phone": (getattr(CLIENT, "phone", "") or ""),
                "min_interval": monitor_store.MIN_INTERVAL,
            })

        if path == "/api/monitors":
            items = monitor_store.list_all()
            return self._send(200, {"ok": True, "monitors": items})

        if path == "/api/logs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            n = int((q.get("n") or ["200"])[0])
            lv = (q.get("level") or [None])[0]
            return self._send(200, {"ok": True, "logs": gygo_log.tail(n, lv)})

        if path == "/api/health":
            return self._send(200, {
                "ok": True,
                "version": VERSION,
                "logged": CLIENT is not None and not AUTH_EXPIRED,
                "expired": AUTH_EXPIRED,
                "monitors": len(monitor_store.list_all()),
                "running": sum(1 for m in monitor_store.list_all()
                               if m.get("enabled") and m.get("status") != "invalid"),
            })

        if path == "/api/dirs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fid = (q.get("fid") or [""])[0]
            try:
                return self._send(200, act_browse_dirs(fid))
            except ApiError as e:
                return self._send(200, {"ok": False, "msg": str(e)})

        return self._send(404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        data = self._body()
        try:
            if path == "/api/sms/send":
                msg = act_sms_send((data.get("phone") or "").strip())
                return self._send(200, {"ok": True, "msg": msg})

            if path == "/api/sms/login":
                r = act_sms_login((data.get("phone") or "").strip(),
                                  (data.get("code") or "").strip())
                return self._send(200, {"ok": True, "msg": "登录成功", **r})

            if path == "/api/logout":
                return self._send(200, {**act_logout(), "msg": "已退出"})

            if path == "/api/monitors":
                r = act_add_monitor(
                    (data.get("share_url") or "").strip(),
                    (data.get("target_path") or "").strip(),
                    int(data.get("interval_min") or monitor_store.MIN_INTERVAL),
                    keep_tree=data.get("keep_tree", True),
                    include_kw=(data.get("include_kw") or "").strip(),
                    exclude_kw=(data.get("exclude_kw") or "").strip(),
                    min_size_mb=int(data.get("min_size_mb") or 0),
                    transfer_existing=data.get("transfer_existing", True),
                )
                return self._send(200, r)

            if path.startswith("/api/monitors/") and path.endswith("/scan"):
                mid = int(path.split("/")[3])
                return self._send(200, act_scan(mid))

            if path.startswith("/api/monitors/") and path.endswith("/reset"):
                mid = int(path.split("/")[3])
                return self._send(200, act_reset_baseline(mid))

            if path.startswith("/api/monitors/") and path.endswith("/toggle"):
                mid = int(path.split("/")[3])
                m = monitor_store.get(mid)
                if not m:
                    return self._send(404, {"ok": False, "msg": "监控项不存在"})
                monitor_store.update(mid, enabled=not m.get("enabled", True))
                return self._send(200, {"ok": True, "monitor": monitor_store.get(mid)})

            # 编辑监控项：/api/monitors/{id}（只认三段，多的当未知路由）
            if path.startswith("/api/monitors/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    return self._send(404, {"ok": False, "msg": "not found"})
                try:
                    mid = int(parts[2])
                except ValueError:
                    return self._send(400, {"ok": False, "msg": "参数错误"})
                return self._send(200, act_update_monitor(mid, data))
        except ShareError as e:
            return self._send(200, {"ok": False, "msg": e.message})
        except TokenExpired:
            global AUTH_EXPIRED
            AUTH_EXPIRED = True
            for m in monitor_store.list_all():
                if m.get("status") == "ok":
                    monitor_store.update(m["id"], status="paused")
            return self._send(200, {"ok": False, "msg": "登录已失效，请重新登录"})
        except ApiError as e:
            return self._send(200, {"ok": False, "msg": str(e)})
        except Exception as e:
            return self._send(200, {"ok": False, "msg": "出错了：%s" % e})

        return self._send(404, {"ok": False, "msg": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/monitors/"):
            try:
                mid = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._send(400, {"ok": False, "msg": "参数错误"})
            ok = monitor_store.remove(mid)
            return self._send(200, {"ok": ok})
        return self._send(404, {"ok": False, "msg": "not found"})


def main():
    global CLIENT, AUTH_EXPIRED
    CLIENT = _client_from_auth()
    if CLIENT:
        AUTH_EXPIRED = False
    monitor.bind(sys.modules[__name__], _set_expired)
    monitor.start_scheduler()

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    def _shutdown(signum, _frame):
        gygo_log.info("收到退出信号 %s，正在停止", signum)
        try:
            srv.shutdown()
        except Exception:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    gygo_log.info("gygo v%s 已启动 -> http://0.0.0.0:%d", VERSION, PORT)
    print("gygo v%s 已启动 -> http://0.0.0.0:%d" % (VERSION, PORT))
    if CLIENT:
        gygo_log.info("已恢复上次登录态（%s）", CLIENT.phone or "已登录")
        print("已恢复上次登录态（%s），监控运行中" % (CLIENT.phone or "已登录"))
    else:
        print("尚未登录，请打开网页用手机号短信登录")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n已停止")
        gygo_log.info("已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
