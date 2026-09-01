# -*- coding: utf-8 -*-
"""gygo 核心逻辑自测 —— 不需要登录，不需要网络。

    python3 tests/test_core.py

覆盖：目录结构保持、过滤规则、单次转存限流、失败重试、基线推进。
真实接口相关的验证请用 selftest.py（需网络，但无需登录）。
"""
import os
import sys
import time

import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="gygo-test-")
os.environ["GYGO_DATA_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor
import monitor_store
from share_gy import _rel_dir_of
from guangya import normalize_phone

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [OK]   %-42s -> %r" % (name, got))
    else:
        FAIL += 1
        print("  [FAIL] %-42s -> %r (期望 %r)" % (name, got, want))


print("=" * 66)
print("A. 手机号规范化 normalize_phone（修复 captcha_invalid）")
print("=" * 66)
for raw, want in [
    ("18155958936", "+8618155958936"),
    ("8618155958936", "+8618155958936"),
    ("+86 181 5595 8936", "+8618155958936"),
    ("+8618155958936", "+8618155958936"),
    ("abc123", "abc123"),          # 非标准原样返回
    ("", ""),
]:
    check("normalize(%r)" % raw, normalize_phone(raw), want)

print("=" * 66)
print("B. 目录结构保持 _rel_dir_of")
print("=" * 66)
cases = [
    # (path, share_name, keep_tree, 期望)  —— 外层文件夹一律保留，不剥离
    ("醒来（2026）/S01E01.mp4", "醒来（2026）", True, "醒来（2026）"),
    ("醒来（2026）/Season 01/S01E01.mp4", "醒来（2026）", True, "醒来（2026）/Season 01"),
    ("剧名/Season 01/S01E01.mp4", "别的名字", True, "剧名/Season 01"),
    ("S01E01.mp4", "醒来（2026）", True, ""),                        # 根目录文件
    ("醒来（2026）/S01E01.mp4", "醒来（2026）", False, ""),           # 关掉保持结构
    ("剧名/Season 01/S01E01.mp4", "x", False, ""),
    ("醒来（2026）[tmdbid=1]/S01E01.mp4", "醒来（2026）[tmdbid=1]", True, "醒来（2026）[tmdbid=1]"),
    # 同名外层文件夹包视频：整层都要保留
    ("Charlies.Angels.2019.2160p/movie.mkv", "Charlies.Angels.2019.2160p", True, "Charlies.Angels.2019.2160p"),
    ("A/B:S01/C/S01E01.mp4", "A", True, "A/B_S01/C"),                # 非法字符清洗
]
for path, sname, keep, want in cases:
    item = {"path": path, "name": path.split("/")[-1]}
    check("%s | keep=%s" % (path, keep), _rel_dir_of(item, sname, keep), want)

print("\n" + "=" * 66)
print("C. 过滤规则 _apply_filters")
print("=" * 66)


def f(name, size_mb=500):
    return {"fid": name, "name": name, "path": name,
            "size": int(size_mb * 1024 * 1024)}


files = [f("S01E01.mp4"), f("S01E02.mp4"), f("预告片.mp4"), f("花絮.mkv", 30),
         f("S01E01.chs.srt", 1)]

m = {"include_kw": "S01", "exclude_kw": "", "min_size_mb": 0}
keep, drop = monitor._apply_filters(files, m)
check("只留含 S01 的", [x["name"] for x in keep], ["S01E01.mp4", "S01E02.mp4", "S01E01.chs.srt"])

m = {"include_kw": "S01", "exclude_kw": "srt", "min_size_mb": 0}
keep, drop = monitor._apply_filters(files, m)
check("再排除 srt", [x["name"] for x in keep], ["S01E01.mp4", "S01E02.mp4"])

m = {"include_kw": "", "exclude_kw": "预告,花絮", "min_size_mb": 0}
keep, drop = monitor._apply_filters(files, m)
check("排除预告花絮", [x["name"] for x in keep],
      ["S01E01.mp4", "S01E02.mp4", "S01E01.chs.srt"])

m = {"include_kw": "", "exclude_kw": "", "min_size_mb": 100}
keep, drop = monitor._apply_filters(files, m)
check("最小 100MB", [x["name"] for x in keep],
      ["S01E01.mp4", "S01E02.mp4", "预告片.mp4"])

m = {"include_kw": "S01E0*", "exclude_kw": "", "min_size_mb": 0}
keep, drop = monitor._apply_filters(files, m)
check("通配 S01E0*", [x["name"] for x in keep], ["S01E01.mp4", "S01E02.mp4", "S01E01.chs.srt"])

print("\n" + "=" * 66)
print("C. 限流：一次新增 40 个，上限 20")
print("=" * 66)

# 造一个假 client + 假扫描环境
class FakeClient(object):
    def __init__(self):
        self.calls = []
        self.fail_next = False

    def resolve_path(self, path, root_fid=""):
        return "fid_root"

    def list_dir(self, fid, **kw):
        return []


FC = FakeClient()
CALLS = []


def fake_list(sid, pwd, pdir, only_video=True):
    return [{"fid": "fid%02d" % i, "name": "S01E%02d.mp4" % i,
             "path": "剧/S01E%02d.mp4" % i, "size": 100} for i in range(1, 61)], "测试剧"


def fake_transfer(sid, pwd, files, tgt, client, keep_tree=True, share_name=""):
    CALLS.append([x["name"] for x in files])
    if FC.fail_next:
        FC.fail_next = False
        return {"submitted": len(files), "ok": 0, "fail": 1, "timeout": 0,
                "detail": ["模拟失败"]}
    return {"submitted": len(files), "ok": 1, "fail": 0, "timeout": 0, "detail": ["完成"]}


monitor.list_share_files = fake_list
monitor.transfer_share_files = fake_transfer

monitor.bind(type("A", (), {"CLIENT": FC, "AUTH_EXPIRED": False}), lambda v: None)

# 添加即转存：已登录 + transfer_existing 默认 True，应把 60 集全部转存并建基线
mon = monitor.add_and_baseline("https://www.guangyapan.com/s/xxx_yyy", "影视/测试", 60,
                               client=FC)
check("添加即转存 60 集（3 批）", len(CALLS), 3)
check("建基线 60 条", len(mon["last_files"]), 60)

# 追更模式：基线已有内容（fid99 是之前已转的旧集），分享里又冒出 60 集新文件。
# 此时应受 20/轮 限速，一轮一轮补，而不是一次灌 60 个触发风控 / 打满额度。
CALLS.clear()
monitor_store.update(mon["id"], last_files=["fid99"])
r1 = monitor.scan_one(monitor_store.get(mon["id"]))
check("追更第一轮转存数", r1.get("added"), 20)
check("追更第一轮剩余排队", r1.get("pending"), 40)
m1 = monitor_store.get(mon["id"])
check("追更基线只推进 20（共 21）", len(m1["last_files"]), 21)

r2 = monitor.scan_one(monitor_store.get(mon["id"]))
check("追更第二轮转存数", r2.get("added"), 20)
check("追更第二轮剩余排队", r2.get("pending"), 20)

r3 = monitor.scan_one(monitor_store.get(mon["id"]))
check("追更第三轮转存数", r3.get("added"), 20)
check("追更第三轮剩余排队", r3.get("pending"), 0)

r4 = monitor.scan_one(monitor_store.get(mon["id"]))
check("追更第四轮无新增", r4.get("added"), 0)
check("追更累计转存 60 集", sum(len(c) for c in CALLS), 60)

print("\n" + "=" * 66)
print("C2. 首次全量回填：基线为空 → 一次性全转 60 集（不受 20/轮 限制）")
print("=" * 66)
monitor_store.update(mon["id"], last_files=[])
CALLS.clear()
rb = monitor.scan_one(monitor_store.get(mon["id"]))
check("回填一次性转存 60", rb.get("added"), 60)
check("回填无剩余排队", rb.get("pending"), 0)
check("回填后基线 60", len(monitor_store.get(mon["id"])["last_files"]), 60)
rbb = monitor.scan_one(monitor_store.get(mon["id"]))
check("回填后再扫无新增", rbb.get("added"), 0)

print("\n" + "=" * 66)
print("D. 转存失败：基线不推进，下轮重试（回填场景一次性全转）")
print("=" * 66)
monitor_store.update(mon["id"], last_files=[])   # 首次回填场景：基线空
CALLS.clear()
FC.fail_next = True
r = monitor.scan_one(monitor_store.get(mon["id"]))
check("失败时状态", r.get("status"), "error")
check("失败后基线仍为空", len(monitor_store.get(mon["id"])["last_files"]), 0)

r = monitor.scan_one(monitor_store.get(mon["id"]))
check("下轮自动重试并一次性全转 60", r.get("added"), 60)
check("重试后基线推进 60", len(monitor_store.get(mon["id"])["last_files"]), 60)

print("\n" + "=" * 66)
print("E. 被过滤的文件也会进基线（不会每轮重复判定）")
print("=" * 66)
monitor_store.remove(mon["id"])
mon2 = monitor.add_and_baseline("https://www.guangyapan.com/s/xxx_yyy", "影视/测试", 60,
                                include_kw="不存在的关键词", client=FC)
check("被过滤的也进基线（全量 60）", len(mon2["last_files"]), 60)
monitor_store.update(mon2["id"], last_files=[])
CALLS.clear()
r = monitor.scan_one(monitor_store.get(mon2["id"]))
check("全被过滤，转存 0", r.get("added"), 0)
check("过滤数 60", r.get("filtered"), 60)
check("但基线已推进 60", len(monitor_store.get(mon2["id"])["last_files"]), 60)
r = monitor.scan_one(monitor_store.get(mon2["id"]))
check("下轮不再有新增", r.get("added"), 0)

print("\n" + "=" * 66)
print("F. 添加时不转存的两种情形")
print("=" * 66)
# 未登录添加：基线留空，登录后首次扫描会把全部当新增转存
CALLS.clear()
m_nologin = monitor.add_and_baseline(
    "https://www.guangyapan.com/s/1111111111111111_aBcDeFgHiJkLmNoP", "影视/X", 60)
check("未登录→空基线", len(m_nologin["last_files"]), 0)
check("未登录→不转存", len(CALLS), 0)

# 已登录但 transfer_existing=False：建完整基线，不转已有（只追新）
CALLS.clear()
m_trackonly = monitor.add_and_baseline(
    "https://www.guangyapan.com/s/1111111111111111_aBcDeFgHiJkLmNoP", "影视/X", 60,
    transfer_existing=False, client=FC)
check("只追新→建基线 60 条", len(m_trackonly["last_files"]), 60)
check("只追新→不转存", len(CALLS), 0)

print("\n" + "=" * 66)
print("G. 添加/登录后即时扫描 scan_now")
print("=" * 66)

# 准备 scan_now 的后台环境（用假调度器，不启动真实线程/扫描）
monitor._CLIENT_GETTER = lambda: FC
monitor._EXPIRED_GETTER = lambda: False
class _FakeSch:
    _busy = set()
    def is_alive(self):
        return True
monitor._scheduler = _FakeSch()

# 把 scan_one 换成记录调用的假函数，验证 scan_now 真的在后台调了它
hits = []
def _fake_scan_one(m):
    hits.append(m["id"])
    return {"status": "ok", "added": 0, "total": 0}
_orig_scan_one = monitor.scan_one
monitor.scan_one = _fake_scan_one
try:
    mg = monitor.add_and_baseline(
        "https://www.guangyapan.com/s/1111111111111111_aBcDeFgHiJkLmNoP", "影视/X", 60,
        transfer_existing=False, client=FC)
    ok = monitor.scan_now(mg["id"])
    time.sleep(0.5)  # 等后台线程跑完
    check("scan_now 返回 True", ok, True)
    check("scan_now 真的后台调了 scan_one", len(hits), 1)
    check("scan_now 扫的是目标监控项", (hits and hits[0]), mg["id"])
    monitor_store.remove(mg["id"])
finally:
    monitor.scan_one = _orig_scan_one
    monitor._scheduler = None
    monitor._CLIENT_GETTER = None
    monitor._EXPIRED_GETTER = None

print("\n" + "=" * 66)
print("H. 修复：添加即转存期间不重复（基线先建 + _busy 互斥挡住抢跑扫描）")
print("=" * 66)

# 重置环境：复用假 list（60 集），用会"在转存时检查 _busy 并试探 scan_now"的假转存
monitor._CLIENT_GETTER = lambda: FC
monitor._EXPIRED_GETTER = lambda: False
monitor._scheduler = _FakeSch()
CALLS.clear()

captured = []   # [占用期间的 mid, scan_now 返回值]
_orig_tr = monitor.transfer_share_files
def fake_transfer_check(sid, pwd, files, tgt, client, keep_tree=True, share_name=""):
    busy = list(monitor._scheduler._busy)
    if busy and not captured:
        # 模拟"添加转存途中有人点了扫描 / 触发了即时扫描"
        captured.append(busy[0])                    # 当前正被占用的监控项 id
        captured.append(monitor.scan_now(busy[0]))  # 修复后应被挡，返回 False
    for x in files:
        CALLS.append(x["fid"])
    return {"submitted": len(files), "ok": 1, "fail": 0, "timeout": 0, "detail": ["完成"]}
monitor.transfer_share_files = fake_transfer_check
try:
    mh = monitor.add_and_baseline(
        "https://www.guangyapan.com/s/1111111111111111_aBcDeFgHiJkLmNoP", "影视/H", 60,
        client=FC)
    check("添加即转存 60 集（3 批）", len(CALLS), 60)
    check("60 个 fid 各只转一次（无副本）", len(set(CALLS)), 60)
    check("建基线 60 条", len(mh["last_files"]), 60)
    check("转存期间确实占用了 _busy", len(captured) >= 1, True)
    if len(captured) >= 2:
        check("占用期间 scan_now 被挡（不重复转存）", captured[1], False)
        check("被挡的正是当前监控项", captured[0], mh["id"])
finally:
    monitor.transfer_share_files = _orig_tr
    monitor._scheduler = None
    monitor._CLIENT_GETTER = None
    monitor._EXPIRED_GETTER = None

print("\n" + "=" * 66)
print("I. 手动扫描 act_scan 与定时扫描互斥（防并发重复）")
print("=" * 66)
import app as appmod
appmod.CLIENT = FC
appmod.AUTH_EXPIRED = False
monitor._scheduler = _FakeSch()
cnt = {"n": 0}
_orig_so = monitor.scan_one
def _count_scan(m, on_phase=None):
    cnt["n"] += 1
    return {"status": "ok", "added": 0, "total": 0}
monitor.scan_one = _count_scan
try:
    mi = monitor.add_and_baseline(
        "https://www.guangyapan.com/s/1111111111111111_aBcDeFgHiJkLmNoP", "影视/I", 60,
        transfer_existing=False, client=FC)
    # 模拟该链接正在被定时扫描占用
    monitor._scheduler._busy.add(mi["id"])
    r = appmod.act_scan(mi["id"])
    check("扫描占用时手动扫描被拦截", r.get("ok"), False)
    check("拦截时未运行 scan_one（无重复）", cnt["n"], 0)
finally:
    monitor._scheduler._busy.discard(mi["id"])
    monitor.scan_one = _orig_so
    monitor._scheduler = None

print("\n" + "=" * 66)
print("结果：通过 %d 项，失败 %d 项" % (PASS, FAIL))
print("=" * 66)
sys.exit(1 if FAIL else 0)
