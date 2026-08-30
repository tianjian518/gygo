"""光鸭云盘 —— 分享链接解析、列举、转存。

分享相关的「只读」接口（查分享信息、换 token、列目录）全部免登录，
只有最后一步 restore_share 需要用户自己的登录态。

两个易错点：
  1. 分享列表 page 从 1 开始（个人盘从 0 开始）
  2. 分享根目录 parentId 是空字符串 ""（个人盘根目录是 "0"）
"""

import re
import time
import urllib.error
import urllib.request

from guangya import (API_RES, CLIENT_ID, WEB_ORIGIN, UA, ApiError,
                     generate_did, generate_traceparent)

VIDEO_EXT = {
    "mp4", "mkv", "avi", "mov", "ts", "m2ts", "wmv", "flv", "webm",
    "rmvb", "rm", "mpg", "mpeg", "3gp", "m4v", "vob", "iso", "img",
}

MAX_PAGE = 200       # 单目录最多翻 200 页
PAGE_SIZE = 50
MAX_DEPTH = 6        # 递归子目录最大深度，防止异常链接把程序拖死
BATCH_SIZE = 100     # 单次转存文件数上限


class ShareError(Exception):
    """分享相关错误。fatal=True 表示链接已作废，不该再重试。"""

    def __init__(self, message, fatal=False):
        Exception.__init__(self, message)
        self.message = message
        self.fatal = fatal


# ------------------------------------------------------------------ 链接解析

def parse_share_input(text):
    """从一段文字里解析出 (share_id, passcode, pdir_fid)。

    容错范围：全角 ？＆、已 URL 编码的链接、带 # 的前端路由、
    以及「提取码：xxxx」「访问码：xxxx」这类中文提示。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ShareError("分享内容为空", fatal=True)

    s = raw.replace("？", "?").replace("＆", "&")
    s = re.sub(r"\s+", "", s)

    # 1) 提取码：先摘出来，避免干扰后续解析
    passcode = ""
    m = re.search(r"(?:提取码|访问码|密码|提取密码)\s*[:：]\s*([0-9A-Za-z]{4,8})", s)
    if m:
        passcode = m.group(1)
        s = s.replace(m.group(0), "")

    # 2) share_id
    share_id = ""
    m = re.search(r"[?&](?:shareId|share_id|id|sid)=([0-9A-Za-z_-]+)", s, re.I)
    if m:
        share_id = m.group(1)
    if not share_id:
        m = re.search(r"/(?:share|s|link|download)/([0-9A-Za-z_-]{6,})", s)
        if m:
            share_id = m.group(1)

    # 3) 提取码也可能挂在 query 上
    if not passcode:
        m = re.search(r"[?&](?:pwd|code|passcode|accessCode)=([0-9A-Za-z]{4,8})", s, re.I)
        if m:
            passcode = m.group(1)

    # 4) pdir_fid：先看 query，再看 # 后面的前端路由
    pdir_fid = ""
    m = re.search(r"[?&](?:parentId|parent_id|pdir_fid|fid|fileId)=([0-9A-Za-z_-]+)", s, re.I)
    if m:
        pdir_fid = m.group(1)
    if not pdir_fid and "#" in s:
        frag = s.split("#", 1)[1]
        m = re.search(r"[?&](?:parentId|parent_id|pdir_fid|fid|fileId)=([0-9A-Za-z_-]+)", frag, re.I)
        if m:
            pdir_fid = m.group(1)
        if not pdir_fid:
            m = re.search(r"/(?:list/)?share/[0-9A-Za-z_-]+(?:/([0-9A-Za-z_-]+))?", frag)
            if m and m.group(1):
                pdir_fid = m.group(1)

    if not share_id:
        raise ShareError("没能从内容里认出分享 ID，请检查链接是否完整", fatal=True)
    return share_id, passcode, pdir_fid or ""


# ------------------------------------------------------------------ 底层请求

def _public_post(path, body):
    """分享公开接口：不需要登录，靠请求体里的 accessToken 或 shareId 鉴权。"""
    url = API_RES + "/" + path.lstrip("/")
    data = _json_dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/",
        "User-Agent": UA,
        "Accept-Language": "zh-CN",
        "did": generate_did(),
        "dt": "4",
        "traceparent": generate_traceparent(),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ShareError("HTTP %s" % e.code)
    except Exception as e:
        raise ShareError("网络错误：%s" % e)
    try:
        import json
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def _json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def _is_fail(payload):
    code = payload.get("code")
    if code not in (None, "", 0, 200, "0", "200"):
        return True
    msg = str(payload.get("message") or payload.get("msg") or "").lower()
    return msg in ("error", "fail", "failed")


def _msg(payload, default="接口未返回错误信息"):
    for k in ("message", "msg", "errorMessage", "error", "error_description"):
        v = payload.get(k)
        if v:
            return str(v)
    return default


def _extract_list(payload):
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("list", "files", "items", "records", "rows", "fileList"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


# ------------------------------------------------------------------ 分享三步走

def get_share_token(share_id, passcode=""):
    """查分享是否有效，并换取分享维度的 access_token。

    返回 (access_token, share_name)。
    失效判定按官方错误码：404 不存在 / 400 缺提取码 / 403 提取码错 —— 都属于 fatal。
    """
    payload = _public_post("get_share_summary", {"shareId": share_id})
    if _is_fail(payload):
        raise ShareError("分享不存在或已失效：" + _msg(payload), fatal=True)
    # 分享标题在 summary 的 data.title 里
    _sum = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    share_name = str(_sum.get("title") or _sum.get("shareName") or _sum.get("name") or "")

    payload = _public_post("get_share_access_token",
                           {"shareId": share_id, "code": passcode or ""})
    token = (payload.get("access_token")
             or (payload.get("data") or {}).get("access_token")
             or (payload.get("data") or {}).get("accessToken")
             or (payload.get("data") or {}).get("token"))
    if not token:
        code = str(payload.get("code") or "")
        if code in ("403", "400"):
            raise ShareError("提取码错误或分享不可访问", fatal=True)
        raise ShareError("提取码错误或分享不可访问（%s）" % _msg(payload), fatal=True)

    return token, share_name


def list_share_files(share_id, passcode="", pdir_fid="", only_video=True):
    """递归列出分享里的所有文件。

    返回 [{"fid", "name", "path", "size", "dir"}]
    path 是相对分享根的路径，用于展示和排重。
    """
    token, share_name = get_share_token(share_id, passcode)
    root = "" if pdir_fid in ("", "0", "/", "root") else pdir_fid
    out = []
    _walk(token, root, "", out, 0, only_video)
    return out, share_name


def _walk(token, parent_id, prefix, out, depth, only_video):
    if depth > MAX_DEPTH or len(out) > 10000:
        return
    for page in range(1, MAX_PAGE + 1):
        payload = _public_post("get_share_page_files_list", {
            "accessToken": token,
            "parentId": parent_id,
            "page": page,
            "pageSize": PAGE_SIZE,
            "orderBy": 0,
            "sortType": 0,
        })
        if _is_fail(payload):
            # 链接中途失效：往上抛，让监控标记为 invalid
            raise ShareError(_msg(payload, "列举分享目录失败"), fatal=True)
        items = _extract_list(payload)
        if not items:
            return
        for it in items:
            item = _norm_share_item(it, prefix)
            if item["dir"]:
                _walk(token, item["fid"], item["path"], out, depth + 1, only_video)
            elif (not only_video) or item["is_video"]:
                out.append(item)
        if len(items) < PAGE_SIZE:
            return


def _norm_share_item(it, prefix):
    fid = ""
    for k in ("fid", "fileId", "id", "resId"):
        if it.get(k):
            fid = str(it[k])
            break
    name = ""
    for k in ("file_name", "fileName", "name", "title"):
        if it.get(k):
            name = str(it[k])
            break
    # 类型判断：光鸭分享接口用 resType（2=目录），个人盘接口用 type/dirType。
    # 显式的布尔字段优先，其次按 type > resType > fileType > dirType 取值。
    is_dir = None
    for k in ("dir", "isDir", "is_dir", "isFolder"):
        if it.get(k) is not None:
            is_dir = bool(it[k])
            break
    if is_dir is None:
        raw_type = None
        for k in ("type", "resType", "fileType", "dirType"):
            if it.get(k) is not None:
                raw_type = it[k]
                break
        is_dir = str(raw_type) in ("2", "dir", "folder") if raw_type is not None else False
    try:
        size = int(it.get("size") or it.get("fileSize") or it.get("file_size") or 0)
    except (TypeError, ValueError):
        size = 0
    path = (prefix + "/" + name) if prefix else name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "fid": fid,
        "name": name,
        "path": path,
        "dir": is_dir,
        "size": size,
        "is_video": ext in VIDEO_EXT,
    }


# ------------------------------------------------------------------ 转存

def transfer_share_files(share_id, passcode, files, target_fid, client):
    """把分享里的指定文件转存到自己的 target_fid 目录。

    client 必须已登录：restore_share 的请求头要用户 Bearer token，
    请求体里放的是分享的 accessToken —— 两个 token 含义不同，别混。
    """
    if not files:
        return 0
    token, _ = get_share_token(share_id, passcode)
    fids = [f["fid"] for f in files if f.get("fid")]
    if not fids:
        return 0

    done = 0
    for i in range(0, len(fids), BATCH_SIZE):
        batch = fids[i:i + BATCH_SIZE]
        payload = client._http(
            "POST", API_RES + "/restore_share",
            body={
                "accessToken": token,           # 分享的 token
                "fileIds": batch,
                "parentId": "" if target_fid in ("0", "") else target_fid,
            },
            headers=client._res_headers(),      # 用户的 Bearer token
        )
        if client._is_fail(payload):
            raise ShareError("转存失败：" + client._msg(payload))
        task_id = (payload.get("taskId") or payload.get("task_id")
                   or (payload.get("data") or {}).get("taskId")
                   or (payload.get("data") or {}).get("task_id")
                   or (payload.get("data") or {}).get("id"))
        if task_id:
            _wait_task(client, task_id)
        done += len(batch)
    return done


def _wait_task(client, task_id, max_poll=20, interval=1):
    """轮询转存任务。超时不报错——任务可能还在后台跑，下轮扫描会发现文件已到位。"""
    for _ in range(max_poll):
        try:
            payload = client._http(
                "POST", API_RES + "/get_task_status",
                body={"taskId": task_id},
                headers=client._res_headers(),
            )
        except ApiError:
            return False
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        status = data.get("status")
        if status is None:
            status = data.get("taskStatus")
        if status is None:
            status = data.get("state")
        text = str(status or "").lower()
        msg = str(data.get("message") or data.get("msg") or "").lower()
        if status in (2, 3, 4, "2", "3", "4") or text in (
                "done", "success", "completed", "finish", "finished"):
            return True
        if status in (5, -1, "5", "-1") or text in ("failed", "error") \
                or "失败" in msg or "failed" in msg or "error" in msg:
            return False
        time.sleep(interval)
    return False
