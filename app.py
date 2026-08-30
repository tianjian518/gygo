"""gygo —— 光鸭云盘分享链接追更监控。

纯标准库 Web 服务，浏览器里用鼠标操作，不需要命令行。
默认端口 5099（避开 casgen 的 5000 和 139cas 的 5244）。
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import monitor
import monitor_store
from guangya import ApiError, GuangyaClient, TokenExpired
from share_gy import ShareError

PORT = int(os.environ.get("PORT") or os.environ.get("GYGO_PORT") or 5099)
HERE = os.path.dirname(os.path.abspath(__file__))

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


def act_add_monitor(share_url, target_path, interval_min):
    if CLIENT is None or AUTH_EXPIRED:
        raise ApiError("请先登录光鸭云盘")
    mon = monitor.add_and_baseline(share_url, target_path, interval_min)
    return {"ok": True, "monitor": mon}


def act_scan(mid):
    if CLIENT is None or AUTH_EXPIRED:
        raise ApiError("请先登录光鸭云盘")
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
                r = act_add_monitor((data.get("share_url") or "").strip(),
                                    (data.get("target_path") or "").strip(),
                                    int(data.get("interval_min") or monitor_store.MIN_INTERVAL))
                return self._send(200, r)

            if path.startswith("/api/monitors/") and path.endswith("/scan"):
                mid = int(path.split("/")[3])
                return self._send(200, act_scan(mid))

            if path.startswith("/api/monitors/") and path.endswith("/toggle"):
                mid = int(path.split("/")[3])
                m = monitor_store.get(mid)
                if not m:
                    return self._send(404, {"ok": False, "msg": "监控项不存在"})
                monitor_store.update(mid, enabled=not m.get("enabled", True))
                return self._send(200, {"ok": True, "monitor": monitor_store.get(mid)})
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
    print("gygo 已启动 -> http://0.0.0.0:%d" % PORT)
    if CLIENT:
        print("已恢复上次登录态（%s），监控运行中" % (CLIENT.phone or "已登录"))
    else:
        print("尚未登录，请打开网页用手机号短信登录")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
