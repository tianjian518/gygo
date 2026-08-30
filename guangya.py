"""光鸭云盘客户端 —— 登录、目录操作、token 自动续期。

纯 Python 标准库，零第三方依赖。

接口约定（逆向自公开实现，非官方文档，可能随官方调整而失效）：
    account : https://account.guangyapan.com/v1    登录 / 刷新
    res     : https://api.guangyapan.com/nd.bizuserres.s/v1   网盘业务

短信登录四步：
    /shield/captcha/init -> /auth/verification -> /auth/verification/verify -> /auth/signin
"""

import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request

CLIENT_ID = "aMe-8VSlkrbQXpUR"
API_ACCOUNT = "https://account.guangyapan.com/v1"
API_RES = "https://api.guangyapan.com/nd.bizuserres.s/v1"
WEB_ORIGIN = "https://www.guangyapan.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

ROOT_FID = "0"          # 个人盘根目录
ROOT_SHARE_PARENT = ""  # 分享列表根目录用空字符串，注意别混

TIMEOUT = 30


def generate_did():
    """设备 ID：md5(随机16字节hex)，32 位。"""
    return hashlib.md5(secrets.token_hex(16).encode("utf-8")).hexdigest()


def generate_traceparent():
    """链路追踪头：00-<32hex>-<16hex>-01"""
    return "00-%s-%s-01" % (secrets.token_hex(16), secrets.token_hex(8))


class TokenExpired(Exception):
    """登录态失效且无法自动刷新，需要用户重新短信登录。"""


class ApiError(Exception):
    """接口返回错误。"""


class GuangyaClient(object):
    """光鸭云盘客户端。

    access_token 过期时，若持有 refresh_token 会自动续期并重试一次；
    refresh 也失败则抛 TokenExpired，由上层暂停监控并提示重新登录。
    """

    def __init__(self, access_token="", refresh_token="", device_id="", phone=""):
        self.access_token = access_token or ""
        self.refresh_token = refresh_token or ""
        self.device_id = device_id or generate_did()
        self.phone = phone or ""
        self.expires_at = None

    # ------------------------------------------------------------------ 基础请求

    def _http(self, method, url, body=None, headers=None, retry_on_401=True):
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_on_401 and self.refresh_token:
                if self.do_refresh():
                    # 用新 token 重建鉴权头后重试一次
                    hdrs2 = dict(headers or {})
                    hdrs2["Authorization"] = "Bearer " + self.access_token
                    hdrs2["accessToken"] = self.access_token
                    req2 = urllib.request.Request(url, data=data, headers=hdrs2, method=method)
                    with urllib.request.urlopen(req2, timeout=TIMEOUT) as resp2:
                        raw = resp2.read().decode("utf-8", "replace")
                    return self._parse(raw)
            raise ApiError("HTTP %s: %s" % (e.code, _safe_read(e)))
        except Exception as e:
            raise ApiError("网络错误: %s" % e)
        return self._parse(raw)

    @staticmethod
    def _parse(raw):
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}

    def _account_headers(self, captcha_token=None, action_401=False):
        h = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/",
            "User-Agent": UA,
            "Accept-Language": "zh-CN",
            "x-client-id": CLIENT_ID,
            "x-client-version": "0.0.1",
            "x-device-id": self.device_id,
            "x-device-model": "chrome%2F147.0.0.0",
            "x-device-name": "PC-Chrome",
            "x-device-sign": "wdi10." + self.device_id + "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "x-os-version": "Win32",
            "x-platform-version": "1",
            "x-protocol-version": "301",
            "x-sdk-version": "9.0.2",
        }
        if captcha_token:
            h["x-captcha-token"] = captcha_token
        if action_401:
            h["x-action"] = "401"
        return h

    def _res_headers(self):
        """业务接口头：需要用户登录态。"""
        h = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/",
            "User-Agent": UA,
            "Accept-Language": "zh-CN",
            "Authorization": "Bearer " + self.access_token,
            "Did": self.device_id,
            "Dt": "4",
            "did": self.device_id,
            "dt": "4",
            "traceparent": generate_traceparent(),
        }
        if self.access_token:
            h["accessToken"] = self.access_token
        return h

    @staticmethod
    def _is_fail(payload):
        code = payload.get("code")
        if code not in (None, "", 0, 200, "0", "200"):
            return True
        msg = str(payload.get("message") or payload.get("msg") or payload.get("error") or "").lower()
        return msg in ("error", "fail", "failed")

    @staticmethod
    def _msg(payload, default="接口错误"):
        for k in ("message", "msg", "error", "errorMessage", "error_description"):
            v = payload.get(k)
            if v:
                return str(v)
        return default

    def ensure_login(self):
        if not self.access_token:
            raise TokenExpired("尚未登录")
        # 轻量探活：列根目录第一页
        try:
            self.list_dir(ROOT_FID, page_size=1)
        except ApiError:
            raise TokenExpired("登录态失效，请重新登录")

    # ------------------------------------------------------------------ 短信登录

    def sms_init(self, phone):
        """第一步：拿人机验证票据 captcha_token。"""
        payload = self._http(
            "POST", API_ACCOUNT + "/shield/captcha/init",
            body={
                "client_id": CLIENT_ID,
                "action": "POST:/v1/auth/verification",
                "device_id": self.device_id,
                "meta": {"phone_number": phone},
            },
            headers=self._account_headers(),
        )
        token = (payload.get("captcha_token") or payload.get("captchaToken")
                 or _dig(payload, "data", "captcha_token")
                 or _dig(payload, "data", "captchaToken"))
        if not token:
            if payload.get("url") or payload.get("captcha_url"):
                raise ApiError("需要完成人机验证，请在光鸭网页端操作后再试")
            raise ApiError("获取验证票据失败：" + self._msg(payload))
        return token

    def sms_send(self, phone, captcha_token):
        """第二步：发短信，返回 verification_id。"""
        payload = self._http(
            "POST", API_ACCOUNT + "/auth/verification",
            body={"phone_number": phone, "target": "ANY", "client_id": CLIENT_ID},
            headers=self._account_headers(captcha_token=captcha_token),
        )
        vid = (payload.get("verification_id") or payload.get("verificationId")
               or _dig(payload, "data", "verification_id")
               or _dig(payload, "data", "verificationId"))
        if not vid:
            raise ApiError("发送短信失败：" + self._msg(payload))
        return vid

    def sms_verify(self, verification_id, code):
        """第三步：校验验证码，返回 verification_token。"""
        payload = self._http(
            "POST", API_ACCOUNT + "/auth/verification/verify",
            body={
                "verification_id": verification_id,
                "verification_code": code,
                "client_id": CLIENT_ID,
            },
            headers=self._account_headers(),
        )
        vtoken = (payload.get("verification_token") or payload.get("verificationToken")
                  or _dig(payload, "data", "verification_token")
                  or _dig(payload, "data", "verificationToken"))
        if not vtoken:
            raise ApiError("验证码校验失败：" + self._msg(payload))
        return vtoken

    def sms_signin(self, phone, code, verification_token, captcha_token):
        """第四步：正式登录，拿到 access_token / refresh_token。"""
        payload = self._http(
            "POST", API_ACCOUNT + "/auth/signin",
            body={
                "verification_code": code,
                "verification_token": verification_token,
                "username": phone,
                "client_id": CLIENT_ID,
            },
            headers=self._account_headers(captcha_token=captcha_token),
        )
        at = payload.get("access_token") or _dig(payload, "data", "access_token")
        if not at:
            raise ApiError("登录失败：" + self._msg(payload))
        self.phone = phone
        self.access_token = at
        self.refresh_token = (payload.get("refresh_token")
                              or _dig(payload, "data", "refresh_token")
                              or self.refresh_token)
        try:
            self.expires_at = time.time() + float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            self.expires_at = None
        return True

    def do_refresh(self):
        """用 refresh_token 换新 access_token。成功返回 True。"""
        if not self.refresh_token:
            return False
        try:
            payload = self._http(
                "POST", API_ACCOUNT + "/auth/token",
                body={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
                headers=self._account_headers(action_401=True),
                retry_on_401=False,
            )
        except Exception:
            return False
        at = payload.get("access_token") or _dig(payload, "data", "access_token")
        if not at:
            return False
        self.access_token = at
        self.refresh_token = (payload.get("refresh_token")
                              or _dig(payload, "data", "refresh_token")
                              or self.refresh_token)
        return True

    # ------------------------------------------------------------------ 网盘业务

    def list_dir(self, parent_id, page_size=100, max_pages=200):
        """列出个人盘某目录下的文件/文件夹。page 从 0 开始。"""
        items = []
        for page in range(max_pages):
            payload = self._http(
                "POST", API_RES + "/file/get_file_list",
                body={
                    "parentId": parent_id,
                    "page": page,
                    "pageSize": page_size,
                    "orderBy": 3,
                    "sortType": 1,
                    "fileTypes": [],
                },
                headers=self._res_headers(),
            )
            if self._is_fail(payload):
                raise ApiError(self._msg(payload))
            batch = _extract_list(payload)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < page_size:
                break
        return [_norm_item(x) for x in items]

    def create_dir(self, parent_id, name):
        payload = self._http(
            "POST", API_RES + "/file/create_dir",
            body={"parentId": parent_id, "dirName": name, "failIfNameExist": False},
            headers=self._res_headers(),
        )
        if self._is_fail(payload):
            raise ApiError("创建目录失败：" + self._msg(payload))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for k in ("fileId", "fid", "id"):
            if data.get(k):
                return str(data[k])
        return None

    def find_or_create_dir(self, name, parent_id=ROOT_FID):
        """在 parent_id 下找同名目录，没有就建一个，返回其 fid。"""
        for it in self.list_dir(parent_id):
            if it.get("dir") and it.get("name") == name:
                return str(it.get("fid"))
        fid = self.create_dir(parent_id, name)
        if fid:
            return fid
        # 建完立刻查一次，兜住接口不返回 fid 的情况
        for it in self.list_dir(parent_id):
            if it.get("dir") and it.get("name") == name:
                return str(it.get("fid"))
        raise ApiError("目录创建后仍找不到：%s" % name)

    def resolve_path(self, path, root_fid=ROOT_FID):
        """把 '影视/美剧/某某剧' 这样的路径解析成 fid，沿途自动建目录。"""
        parts = [p for p in str(path or "").replace("\\", "/").split("/") if p.strip()]
        fid = root_fid
        for part in parts:
            fid = self.find_or_create_dir(part.strip(), fid)
        return fid


# ------------------------------------------------------------------ 工具函数

def _safe_read(http_error):
    try:
        return http_error.read().decode("utf-8", "replace")[:300]
    except Exception:
        return ""


def _dig(payload, *keys):
    """从 data 子结构里取值。"""
    node = payload
    for k in keys:
        if isinstance(node, dict):
            node = node.get(k)
        else:
            return None
    return node


def _extract_list(payload):
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("list", "files", "items", "records", "fileList", "infoList"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _norm_item(item):
    """归一化个人盘文件项。"""
    fid = _first(item, "fileId", "fid", "id", "resId") or "0"
    name = _first(item, "fileName", "name", "filename") or ""
    raw_type = _first(item, "type", "resType", "fileType", "dirType")
    is_dir = bool(item.get("isDir") or item.get("is_dir") or item.get("dir"))
    if isinstance(raw_type, (int, float)):
        is_dir = int(raw_type) == 2
    elif isinstance(raw_type, str):
        is_dir = raw_type in ("2", "dir", "folder")
    size = 0
    if not is_dir:
        try:
            size = int(_first(item, "fileSize", "size") or 0)
        except (TypeError, ValueError):
            size = 0
    return {"fid": str(fid), "name": name, "dir": is_dir, "size": size}
