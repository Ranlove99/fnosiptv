#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞牛IPTV —— 为飞牛影视提供 IPTV 直播源聚合与订阅分发。

设计要点：
  * 纯标准库，platform=all 单包通吃 x86 / ARM；
  * 端口模式（127.0.0.1:<port>，由 fnOS 转发），业务代码不必剥 /app/<name> 前缀；
  * 订阅地址按请求 Host 生成，避免把 127.0.0.1 写进 M3U（飞牛影视在电视端拉取会失效）；
  * 飞牛要求订阅地址保留 .m3u 后缀，故主入口固定为 /iptv.m3u。
"""

import html as htmlmod
import base64
import itertools
import json
import os
import re
import signal
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import iptvcore as core

APP_NAME = "fnosiptv"
PREFIX = "/app/%s" % APP_NAME
HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, "ui")
APP_VERSION = "1.9.3"

DATA_DIR = os.environ.get("TRIM_PKGVAR") or os.path.join(HERE, "..", "data")
DATA_DIR = os.path.abspath(DATA_DIR)
CONF_FILE = os.path.join(DATA_DIR, "config.json")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")
EPG_FILE = os.path.join(DATA_DIR, "epg.xml")
LOGO_DIR = os.path.join(DATA_DIR, "logos")
LOGO_CTYPE = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".svg": "image/svg+xml"}

# 台标全部失败时给一张 64x64 深色图，比 404 好：飞牛影视会显示这张而不是一直转圈
BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAUElEQVR42u3PQQkAAAgEsAvi2wT2z2YE38Jg"
    "BZbqeS0CAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICApcFHZtgasYTngsA"
    "AAAASUVORK5CYII=")

# 内置源。cn=True 表示「这是中文源」，其频道不受「仅保留中文频道」过滤，
# 否则 iptv-org 里写成英文名的中国地方台（Anhui TV、CETV-1）会被误杀
DEFAULT_SOURCES = [
    # 国内直连源，地址多为台方自有 CDN，实测可用率明显高于 iptv-org，
    # 所以优先级排在 iptv-org 前面，合并时优先占主地址位
    {"id": "vbskycn", "name": "vbskycn 国内直连", "kind": "url", "cn": True, "priority": 8,
     "value": "https://gh-proxy.com/raw.githubusercontent.com/vbskycn/iptv/refs/heads/master/tv/iptv4.txt"},
    {"name": "iptv-org 中国", "kind": "url", "cn": True, "priority": 10,
     "value": "https://iptv-org.github.io/iptv/countries/cn.m3u"},
    {"name": "iptv-org 中文", "kind": "url", "cn": True, "priority": 20,
     "value": "https://iptv-org.github.io/iptv/languages/zho.m3u"},
    {"name": "iptv-org 中国香港", "kind": "url", "cn": True, "priority": 30,
     "value": "https://iptv-org.github.io/iptv/countries/hk.m3u"},
    {"name": "iptv-org 中国台湾", "kind": "url", "cn": True, "priority": 31,
     "value": "https://iptv-org.github.io/iptv/countries/tw.m3u"},
    {"name": "iptv-org 中国澳门", "kind": "url", "cn": True, "priority": 32,
     "value": "https://iptv-org.github.io/iptv/countries/mo.m3u"},
    # 各省运营商单播地址，跨省多半不通，作备源用，优先级压低
    {"id": "cojs", "name": "cojs 全国 IPTV", "kind": "url", "cn": True, "priority": 40,
     "value": "https://iptv.cojs.cn/iptv"},
    {"name": "fanmingming IPv6", "kind": "url", "cn": True, "priority": 60,
     "value": "https://m3u.ibert.me/txt/fmml_ipv6.txt"},
    {"name": "iptv-org 体育", "kind": "url", "cn": False, "priority": 90,
     "value": "https://iptv-org.github.io/iptv/categories/sports.m3u"},
]

# 2 → 3：新增 vbskycn / cojs 两个国内源，老配置升级时自动补齐
BUILTIN_SEED = 3


DEFAULT_SETTINGS = {
    "unify_name": True,
    "dedupe": True,
    "refresh_minutes": 360,
    "timeout": 30,
    "epg_urls": "",
    "public_base": "",
    "probe_on_refresh": False,
    "hide_dead": False,
    "cn_only": True,
    "keep": [],
    "blocked": [],
    "blocked_meta": {},
    "hide_blocked": True,
    "only_alive": False,
    "hide_ad": False,
    "auto_correct": True,
    "local_logo": True,
    # 台标用公网 CDN：飞牛影视只认外网 tvg-logo，内网 /logo 地址一律不显示
    "logo_cdn": True,
    # M3U 里的 m3u8 频道统一走本机 /api/live：实时重取清单 + 源过期自动换源，
    # 专门治飞牛影视「能打开、播一段就停」（清单/切片签名过期、时移窗口播完）
    "m3u_proxy": True,
    # /api/live 代理时是否多源自动切换；关掉则只代理用户选中的主源
    "auto_renew": True,
    # 切片直连：清单仍经 NAS 代理（负责续期/换源），视频分片让播放器直接连源站，
    # NAS 流量从 GB 级降到 KB 级。源站校验 Referer/UA 导致播不动时关掉即可
    "seg_direct": True,
    # 分组维度：可以整组隐藏/排序/改名，不必逐个台去勾
    "hidden_groups": [],
    "group_order": [],
    "group_alias": {},
    # 频道维度：自定义顺序与显示名（按原名存，重抓后仍生效）
    "channel_order": [],
    "name_alias": {},
}

LOCK = threading.RLock()
STATE = {"refreshing": False, "last_error": "", "progress": "",
         "probe": {"running": False, "done": 0, "total": 0, "alive": 0, "dead": 0,
                   "started": 0, "finished": 0},
         "correct": {"running": False, "done": 0, "total": 0, "ad": 0, "doubt": 0,
                     "ok": 0, "started": 0, "finished": 0}}


# ---------------------------------------------------------------- 存储

def _load(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return fallback


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    with LOCK:
        conf = _load(CONF_FILE, None)
        if conf is None:
            conf = {
                "settings": dict(DEFAULT_SETTINGS),
                "sources": [
                    {"id": "s%d" % (i + 1), "enabled": True, "priority": 10 + i,
                     "last_status": "", "last_count": 0, "last_time": 0, **s}
                    for i, s in enumerate(DEFAULT_SOURCES)
                ],
            }
            _save(CONF_FILE, conf)
        conf["settings"] = {**DEFAULT_SETTINGS, **(conf.get("settings") or {})}
        conf.setdefault("sources", [])

        # 老版本升级：补上新增的内置源，已存在的（同 value 或同 name）不重复添加
        if int(conf.get("builtin_seed") or 1) < BUILTIN_SEED:
            # 老配置里的内置源没有 cn 标记，先按 value 补上，
            # 否则 iptv-org 体育（538 个海外台）会被当成中文源，过滤失效
            by_value = {x["value"]: x for x in DEFAULT_SOURCES}
            for src in conf["sources"]:
                if "cn" not in src and (src.get("value") or "").strip() in by_value:
                    src["cn"] = by_value[(src.get("value") or "").strip()]["cn"]
            have = {(s.get("value") or "").strip() for s in conf["sources"]}
            have |= {(s.get("name") or "").strip() for s in conf["sources"]}
            added = 0
            for i, src in enumerate(DEFAULT_SOURCES):
                if src["value"] in have or src["name"] in have:
                    continue
                conf["sources"].append({
                    "id": "seed%d" % i, "enabled": True,
                    "last_status": "", "last_count": 0, "last_time": 0, **src,
                })
                added += 1
            conf["builtin_seed"] = BUILTIN_SEED
            # 升级补进来的源还没抓过，缓存里是旧频道数：标记一下，启动后自动重抓一次，
            # 否则用户装完新版本看到的还是老列表，以为没生效
            if added:
                conf["pending_refresh"] = True
            _save(CONF_FILE, conf)

        # 1.2.0 起勾选改为黑名单。老配置里的 keep 是白名单，转成等价的 blocked，
        # 否则升级后「已选 31 个」会变成只有 31 个台有输出
        if conf["settings"].get("keep") and not conf.get("keep_migrated"):
            allnames = {c.get("name", "") for c in _load(CACHE_FILE, {}).get("channels", [])}
            if allnames:
                old_keep = {str(x) for x in conf["settings"]["keep"]}
                blocked = set(conf["settings"].get("blocked") or [])
                conf["settings"]["blocked"] = sorted(blocked | (allnames - old_keep))
                conf["settings"]["keep"] = []
            conf["keep_migrated"] = True
            _save(CONF_FILE, conf)
        return conf


def save_config(conf):
    with LOCK:
        _save(CONF_FILE, conf)


def load_cache():
    return _load(CACHE_FILE, {"updated_at": 0, "channels": [], "stats": {}})


def group_of(c):
    return c.get("group") or "其他"


def order_channels(chs, st):
    """分组按 group_order，组内按 channel_order；没排过的保持原顺序（排序稳定）。"""
    go = st.get("group_order") or []
    gi = {g: i for i, g in enumerate(go)}
    first = {}
    for i, c in enumerate(chs):
        first.setdefault(group_of(c), i)

    def gkey(c):
        g = group_of(c)
        return (gi.get(g, 10 ** 6), first.get(g, 0))

    ordered = sorted(chs, key=gkey)
    ci = {n: i for i, n in enumerate(st.get("channel_order") or [])}
    out = []
    for _g, it in itertools.groupby(ordered, key=group_of):
        out.extend(sorted(it, key=lambda c: ci.get(c.get("name"), 10 ** 6)))
    return out


def output_channels(cache, conf):
    """最终输出到 M3U/TXT/预览的频道：屏蔽 → 隐藏分组 → 只要可用 → 剔除广告 → 排序 → 别名。"""
    st = conf["settings"]
    chs = cache.get("channels", [])
    blocked = set(st.get("blocked") or [])
    if blocked:
        chs = [c for c in chs if c.get("name") not in blocked]
    hidden = set(st.get("hidden_groups") or [])
    if hidden:
        chs = [c for c in chs if group_of(c) not in hidden]
    if st.get("only_alive"):
        # alive 为 None 表示还没测过，保留；只有明确 False 才剔除
        chs = [c for c in chs if c.get("alive") is not False]
    if st.get("hide_ad"):
        chs = [c for c in chs if not (c.get("verdict") == "ad" or c.get("verdict2") == "ad")]
    chs = core.apply_keep(chs, st.get("keep"))
    chs = order_channels(chs, st)

    # 别名只作用于输出，缓存里始终保留原名：屏蔽、排序、改名才能对得上
    na = st.get("name_alias") or {}
    ga = st.get("group_alias") or {}
    if na or ga:
        out = []
        for c in chs:
            name, grp = c.get("name"), group_of(c)
            if name in na or grp in ga:
                d = dict(c)
                if name in na:
                    d["name"] = na[name]
                if grp in ga:
                    d["group"] = ga[grp]
                out.append(d)
            else:
                out.append(c)
        chs = out
    return chs


# ---------------------------------------------------------------- 频道筛选/屏蔽
# 「屏蔽」= 不进 M3U/TXT/EPG，也不在管理页出现（hide_blocked 打开时）；
# 「已屏蔽」列表就是撤销入口，所以屏蔽不会变成单向操作。

def filter_rows(rows, group="", alive="", ad="", kw=""):
    """按分组、可用性、判定结果、关键字筛选。管理页与批量屏蔽共用同一套口径。"""
    out = rows
    if group and group != "全部":
        out = [c for c in out if (c.get("group") or "其他") == group]
    if alive == "1":
        out = [c for c in out if c.get("alive")]
    elif alive == "0":
        out = [c for c in out if c.get("alive") is False]
    if ad in ("1", "ad"):
        out = [c for c in out if c.get("verdict") == "ad" or c.get("verdict2") == "ad"]
    elif ad == "doubt":
        out = [c for c in out if (c.get("verdict") == "doubt" or c.get("verdict2") == "doubt")
               and not (c.get("verdict") == "ad" or c.get("verdict2") == "ad")]
    if kw:
        kw = str(kw).lower()
        out = [c for c in out if kw in (c.get("name") or "").lower()
               or kw in (c.get("raw_name") or "").lower()]
    return out


def block_names(conf, names, why="手动"):
    """加入屏蔽名单，并记下原因与时间，管理页要显示「为什么被屏蔽」。"""
    st = conf["settings"]
    blocked = set(st.get("blocked") or [])
    meta = dict(st.get("blocked_meta") or {})
    ts = int(time.time())
    for n in names:
        n = str(n or "").strip()
        if not n:
            continue
        blocked.add(n)
        meta[n] = {"t": ts, "why": why}
    st["blocked"] = sorted(blocked)[:20000]
    st["blocked_meta"] = {k: v for k, v in meta.items() if k in blocked}
    return len(st["blocked"])


def unblock_names(conf, names):
    st = conf["settings"]
    blocked = set(st.get("blocked") or [])
    meta = dict(st.get("blocked_meta") or {})
    for n in names:
        n = str(n or "").strip()
        blocked.discard(n)
        meta.pop(n, None)
    st["blocked"] = sorted(blocked)
    st["blocked_meta"] = meta
    return len(st["blocked"])


# ---------------------------------------------------------------- 刷新

def refresh(conf=None, fetch_epg=True):
    conf = conf or load_config()
    with LOCK:
        if STATE["refreshing"]:
            return False
        STATE["refreshing"] = True
        STATE["progress"] = "开始刷新"
    try:
        settings = conf["settings"]
        sources = [s for s in conf["sources"] if s.get("enabled")]
        timeout = int(settings.get("timeout") or 15)
        entries, report = [], []

        def work(src):
            chans, fmt, err = core.load_source(src, timeout=timeout)
            # TLS 握手偶发超时（并发抓取时尤其明显），重试一次能救回大部分
            if err and src.get("kind", "url") == "url":
                time.sleep(1.5)
                chans2, fmt2, err2 = core.load_source(src, timeout=timeout)
                if not err2:
                    return src, chans2, fmt2, ""
            return src, chans, fmt, err

        with core.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(work, s) for s in sources]
            for fut in futures:
                src, chans, fmt, err = fut.result()
                src["last_time"] = core.now()
                src["last_status"] = ("ok:%s:%d" % (fmt, len(chans))) if not err else ("err:" + err)
                src["last_count"] = len(chans)
                report.append({"name": src.get("name"), "count": len(chans),
                               "format": fmt, "error": err})
                for ch in chans:
                    ch["_cn"] = bool(src.get("cn", True))
                    entries.append((src["id"], src.get("name", ""),
                                    int(src.get("priority") or 100), ch))

        STATE["progress"] = "整理频道"
        # 按源优先级排序再合并：同一频道多个源都有时，priority 小的源占主地址，
        # 其余进备源列表（测活时主地址挂了会自动顶上）
        entries.sort(key=lambda e: e[2])
        channels = core.build_channels(entries, settings)
        # 用户手选过的主地址优先恢复（换源后地址全变，不恢复等于白选）
        try:
            old_cache = load_cache()
            channels = apply_picked(channels, old_cache.get("picked") or {})
        except Exception:  # noqa: BLE001
            old_cache = {}

        if settings.get("probe_on_refresh") and channels:
            STATE["progress"] = "检测可用性"
            head = channels[:400]
            with core.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda c: core.probe(c["url"]), head))
            for ch, (ok, info) in zip(head, results):
                ch["alive"] = bool(ok)
                ch["probe"] = str(info)[:80]
            if settings.get("hide_dead"):
                channels = [c for c in channels if c.get("alive")]

        cache = {
            "updated_at": core.now(),
            "channels": channels,
            "report": report,
            "picked": old_cache.get("picked") or {},
            "srcprobe": old_cache.get("srcprobe") or {},
            "stats": {
                "total": len(channels),
                "groups": len({c["group"] for c in channels}),
                "sources": len(sources),
                "dupe_merged": sum(len(c.get("alts") or []) for c in channels),
            },
        }
        _save(CACHE_FILE, cache)
        conf.pop("pending_refresh", None)   # 抓成功了才清标记，失败留着下次启动再试
        save_config(conf)

        if fetch_epg and settings.get("epg_urls"):
            try:
                refresh_epg(settings)
            except Exception as exc:  # noqa: BLE001
                print("[epg] 刷新失败: %s" % exc, flush=True)

        if settings.get("local_logo", True):
            threading.Thread(target=prewarm_logos, args=(channels,), daemon=True).start()

        # 重抓后地址全换了，上次的校正结论已经作废，顺手重跑一遍（可在设置里关掉）
        if settings.get("auto_correct", True) and channels:
            threading.Thread(target=correct_all, daemon=True).start()

        STATE["last_error"] = ""
        STATE["progress"] = "完成"
        print("[refresh] 完成：%d 个频道 / %d 组" % (len(channels), cache["stats"]["groups"]), flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        STATE["last_error"] = str(exc)
        STATE["progress"] = "失败"
        print("[refresh] 失败: %s\n%s" % (exc, traceback.format_exc()), flush=True)
        return False
    finally:
        with LOCK:
            STATE["refreshing"] = False


def prewarm_logos(channels, workers=20, limit=160):
    """后台把台标抓到本地磁盘。

    只预热中文台/央视：台标库对「CCTV9」「湖南卫视」这类命名有收录，
    对英文地方台（AnhuiTV）几乎全 404，逐个等超时会把预热拖到十几分钟。
    剩下的等播放器真正请求 /logo 时再抓，一次也就几百毫秒。
    """
    try:
        os.makedirs(LOGO_DIR, exist_ok=True)
    except OSError:
        return

    cand = [c for c in channels if core.CN_HINT.search(c.get("name") or "")]
    seen = set()
    todo = []
    for ch in cand:
        slug = core.logo_slug(ch.get("name", ""))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        if os.path.exists(os.path.join(LOGO_DIR, slug + ".bin")):
            continue
        todo.append((slug, ch))
        if len(todo) >= limit:
            break

    def one(item):
        slug, ch = item
        data, ext = core.fetch_logo(ch)
        if not data:
            return
        try:
            with open(os.path.join(LOGO_DIR, slug + ".bin"), "wb") as fh:
                fh.write(data)
            _save(os.path.join(LOGO_DIR, slug + ".bin.json"),
                  {"ext": ext, "name": ch.get("name", "")})
        except OSError:
            pass

    if not todo:
        print("[logo] 台标已全部缓存", flush=True)
        return
    print("[logo] 预热 %d 个台标" % len(todo), flush=True)
    with core.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    print("[logo] 台标预热完成", flush=True)


def probe_all(conf=None, limit=0):
    """批量测活。主地址挂掉时试备源，活的那个换到主位。

    结果写回缓存（alive / probe / probed_at），前端轮询 /api/probe/status 看进度。
    """
    with LOCK:
        if STATE["probe"]["running"]:
            return False
        STATE["probe"] = {"running": True, "done": 0, "total": 0, "alive": 0,
                          "dead": 0, "started": core.now(), "finished": 0}

    try:
        cache = load_cache()
        channels = cache.get("channels", [])
        if limit:
            channels = channels[:limit]
        total = len(channels)
        with LOCK:
            STATE["probe"]["total"] = total

        def work(ch):
            ok, info = core.probe(ch["url"], timeout=6)
            if not ok:
                # 备源逐个试，命中就把地址换上来，等于顺手修了一批死链。
                # 试 5 个：cojs 这类源同一个台有 4~6 个运营商地址，只试 3 个容易漏
                for alt in (ch.get("alts") or [])[:5]:
                    ok2, info2 = core.probe(alt, timeout=6)
                    if ok2:
                        old = ch["url"]
                        ch["url"] = alt
                        alts = [a for a in (ch.get("alts") or []) if a != alt]
                        ch["alts"] = [old] + alts
                        return ch, True, "备源可用 " + str(info2)[:40]
            return ch, ok, str(info)[:60]

        alive = dead = 0
        with core.ThreadPoolExecutor(max_workers=16) as pool:
            futs = [pool.submit(work, c) for c in channels]
            for fut in core.as_completed(futs):
                try:
                    ch, ok, info = fut.result()
                except Exception:  # noqa: BLE001
                    continue
                ch["alive"] = bool(ok)
                ch["probe"] = info
                ch["probed_at"] = core.now()
                if ok:
                    alive += 1
                else:
                    dead += 1
                with LOCK:
                    STATE["probe"]["done"] += 1
                    STATE["probe"]["alive"] = alive
                    STATE["probe"]["dead"] = dead

        cache["channels"] = cache.get("channels", [])
        cache["probed_at"] = core.now()
        _save(CACHE_FILE, cache)
        print("[probe] 完成：可用 %d / 失效 %d / 共 %d" % (alive, dead, total), flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print("[probe] 失败: %s" % exc, flush=True)
        return False
    finally:
        with LOCK:
            STATE["probe"]["running"] = False
            STATE["probe"]["finished"] = core.now()


# 台名里的通用后缀：去掉之后还能对上，基本就是同一个台的不同写法
_SUFFIX_WORDS = ("新闻综合", "新闻", "综合", "生活", "都市", "影视", "公共", "科教",
                 "少儿", "经济", "法制", "法治", "文体", "卫视", "频道", "一套", "二套")


def _stem(name):
    s = (name or "").strip()
    for w in _SUFFIX_WORDS:
        if s.endswith(w) and len(s) > len(w):
            return s[:-len(w)]
    return s


def same_channel(a, b):
    """判断两个台名是不是同一路信号的不同写法（余姚综合 / 余姚新闻综合）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = _stem(a), _stem(b)
    if sa and sa == sb:
        return True
    if len(sa) < 2 or len(sb) < 2:
        return False
    # 余姚综合/余姚新闻综合、遂昌综合/浙江遂昌 这类前后缀关系都算同一台
    return sa.startswith(sb) or sb.startswith(sa) or sa in sb or sb in sa


def correct_all(conf=None):
    """源校正：逐个拉频道的 m3u8，识别「录播冒充直播」和「多个台共用一条流」。

    测活只能判断"能不能播"，判断不了"播的是不是这个台"。
    广告源最常见的手法就是拿一条循环宣传片冒充 CCTV-2，
    要么带 ENDLIST（点播文件），要么几个台指向同一条流。
    """
    with LOCK:
        if STATE["correct"]["running"]:
            return False
        STATE["correct"] = {"running": True, "done": 0, "total": 0, "ad": 0, "doubt": 0,
                            "ok": 0, "started": core.now(), "finished": 0}

    try:
        cache = load_cache()
        channels = cache.get("channels", [])
        total = len(channels)
        with LOCK:
            STATE["correct"]["total"] = total

        def work(ch):
            info = core.inspect(ch["url"], timeout=8)
            if (info.get("kind") in ("vod", "loop") or ch.get("verdict") == "ad") and ch.get("alts"):
                # 主地址是录播/循环片、或静态评分已判为广告时，先把备源试一遍：
                # 换到能正常直播的地址，比直接把整台判死有用得多
                for alt in ch["alts"][:3]:
                    info2 = core.inspect(alt, timeout=8)
                    if info2.get("kind") == "live":
                        old = ch["url"]
                        ch["url"] = alt
                        ch["alts"] = [old] + [a for a in ch["alts"] if a != alt]
                        info2["switched"] = True
                        return ch, info2
            return ch, info

        infos = []
        with core.ThreadPoolExecutor(max_workers=16) as pool:
            futs = [pool.submit(work, c) for c in channels]
            for fut in core.as_completed(futs):
                try:
                    infos.append(fut.result())
                except Exception:  # noqa: BLE001
                    continue
                with LOCK:
                    STATE["correct"]["done"] += 1

        # 片段指纹 → 台名集合：不同台指向同一条流，必有一方是错的
        by_fp = {}
        for ch, info in infos:
            fp = info.get("fp") or ""
            if fp and info.get("kind") in ("live", "vod", "loop"):
                by_fp.setdefault(fp, []).append(ch.get("name", ""))

        ad = doubt = ok = 0
        for ch, info in infos:
            kind = info.get("kind")
            note = ""
            level = "ok"
            if kind == "vod":
                level, note = "ad", "录播文件冒充直播"
            elif kind == "loop":
                level, note = "ad", "循环短片（%d 段）" % info.get("segs", 0)
            elif kind == "error":
                note = "拉流失败"
                level = "ok"          # 拉不动不代表是广告，交给测活判断
            if info.get("switched"):
                note = (note + "；" if note else "") + "已换到可用备源"
                ch["verdict"] = "ok"   # 静态判死的台已经被备源救回来，别再挂广告标
            fp = info.get("fp") or ""
            # 同一个台的别名（余姚综合 / 余姚新闻综合）共用一路信号是正常的，
            # 只有名字对不上的才算错标
            peers = [n for n in by_fp.get(fp, [])
                     if not same_channel(n, ch.get("name"))]
            if peers:
                if level != "ad":
                    level = "doubt"
                note = (note + "；" if note else "") + "与 %s 共用同一条流" % "、".join(peers[:3])
            ch["verdict2"] = level
            ch["verdict2_info"] = note
            ch["stream"] = {"kind": kind or "", "segs": info.get("segs", 0),
                            "fp": fp, "checked_at": core.now()}
            if level == "ad":
                ad += 1
            elif level == "doubt":
                doubt += 1
            else:
                ok += 1

        cache["corrected_at"] = core.now()
        _save(CACHE_FILE, cache)
        with LOCK:
            STATE["correct"].update({"ad": ad, "doubt": doubt, "ok": ok})
        print("[correct] 完成：广告 %d / 可疑 %d / 正常 %d" % (ad, doubt, ok), flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print("[correct] 失败: %s" % exc, flush=True)
        return False
    finally:
        with LOCK:
            STATE["correct"]["running"] = False
            STATE["correct"]["finished"] = core.now()


def refresh_epg(settings):
    urls = [u.strip() for u in (settings.get("epg_urls") or "").split(",") if u.strip()]
    texts = []
    for url in urls[:5]:
        try:
            _, raw = core.http_get(url, timeout=20)
            texts.append(core.decode(raw))
        except Exception as exc:  # noqa: BLE001
            print("[epg] %s 获取失败: %s" % (url, exc), flush=True)
            texts.append("")
    if not any(texts):
        return
    merged, used = core.merge_epg(texts, urls)
    with open(EPG_FILE, "w", encoding="utf-8") as fh:
        fh.write(merged)
    print("[epg] 合并 %d 个源，输出 %d 字节" % (len(used), len(merged)), flush=True)


def scheduler():
    while True:
        try:
            conf = load_config()
            mins = int(conf["settings"].get("refresh_minutes") or 0)
            if mins <= 0:
                time.sleep(300)
                continue
            time.sleep(max(60, mins * 60))
            print("[scheduler] 定时刷新", flush=True)
            refresh(load_config())
        except Exception:  # noqa: BLE001
            time.sleep(300)


# ---------------------------------------------------------------- 播放代理
# 浏览器播 HLS 有两个坑：
#   1. Chrome/Edge 不认 m3u8，得靠 hls.js 解；
#   2. 第三方源基本不给 CORS 头，页面直连会被浏览器拦掉。
# 所以播放地址一律走本机代理：m3u8 拉下来重写内部地址，分片原样转发。

STREAM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
_STREAM_SSL = ssl.create_default_context()
_STREAM_SSL.check_hostname = False
_STREAM_SSL.verify_mode = ssl.CERT_NONE
_URI_ATTR = re.compile(r'URI="([^"]*)"')


def e64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def d64(token):
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")


def stream_rel(url, src="", direct=False):
    """子清单地址。带 src 时把「当前用的是哪个源」一并传下去，切片失败才好定位换源。
    direct 透传给下一层，决定媒体清单里的切片是直连源站还是再过本机。"""
    rel = "stream?u=" + e64(url)
    if src:
        rel += "&s=" + e64(src)
    if direct:
        rel += "&d=1"
    return rel


def seg_rel(url, src=""):
    rel = "seg?u=" + e64(url)
    if src:
        rel += "&s=" + e64(src)
    return rel


def rewrite_playlist(text, base, src="", direct=False):
    """把 m3u8 里的子清单、分片、密钥地址换成经过本机代理的地址。

    direct=True 时切片保留源站绝对地址（手机直连，NAS 不转发大流量），
    子清单仍走本机（要逐层改写），密钥也仍走本机（体积极小，顺带带上 Referer 更稳）。
    """
    out = []
    after_stream_inf = False
    for line in text.splitlines():
        raw = line.rstrip("\r")
        s = raw.strip()
        if not s:
            out.append("")
            continue
        if s.startswith("#"):
            if _URI_ATTR.search(s):
                s = _URI_ATTR.sub(
                    lambda m: 'URI="%s"' % seg_rel(urllib.parse.urljoin(base, m.group(1)), src), s)
            out.append(s)
            after_stream_inf = s.startswith("#EXT-X-STREAM-INF")
            continue
        full = urllib.parse.urljoin(base, s)
        is_sub = after_stream_inf or full.split("?")[0].lower().endswith(".m3u8")
        if is_sub:
            out.append(stream_rel(full, src, direct))
        elif direct:
            out.append(full)              # 切片直连源站：这一行省掉一半 NAS 流量
        else:
            out.append(seg_rel(full, src))
        after_stream_inf = False
    return "\n".join(out) + "\n"


def open_stream(url, timeout=10, extra=None):
    """打开远端流。证书链有问题的源很多（播放器照播），这里降级重试一次。"""
    headers = {"User-Agent": STREAM_UA, "Accept": "*/*", "Referer": url}
    headers.update(extra or {})
    req = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), ssl.SSLError):
            return urllib.request.urlopen(req, timeout=timeout, context=_STREAM_SSL)
        raise


def reorder_sources(ch, chosen):
    """把 chosen 换到主位，其余按原顺序进备源。"""
    pairs = [(ch.get("url", ""), ch.get("source", ""))]
    alts = list(ch.get("alts") or [])
    srcs = list(ch.get("alt_sources") or [])
    for i, u in enumerate(alts):
        pairs.append((u, srcs[i] if i < len(srcs) else ""))
    pairs = [p for p in pairs if p[0]]
    head = [p for p in pairs if p[0] == chosen]
    if not head:
        return False
    ch["url"], ch["source"] = head[0]
    rest = [p for p in pairs if p[0] != chosen]
    ch["alts"] = [p[0] for p in rest]
    ch["alt_sources"] = [p[1] for p in rest]
    return True


def apply_picked(channels, picked):
    """重抓后把用户手选的主地址找回来。地址若已从源里消失就忽略，不至于把频道弄没。"""
    if not picked:
        return channels
    for ch in channels:
        want = picked.get(ch.get("name"))
        if want and reorder_sources(ch, want):
            ch["picked"] = True
    return channels


# ---------------------------------------------------------------- 续期 / 失效换源
# 「能打开、播一段就停」的根子：清单或切片带时效签名，播一会儿就 403/404；
# 或者拿到的是时移流，切片窗口播完就没了。
# 这里给每个源记一个冷却期：切片失败即判该源过期，下次取清单时自动跳过、换下一个源。

SRC_COOL = {}                    # 源地址 -> 冷却截止时间戳
COOL_LOCK = threading.Lock()
COOL_SECONDS = 90                # 过期源冷却多久不再优先尝试


def mark_cool(url, secs=COOL_SECONDS):
    if not url:
        return
    with COOL_LOCK:
        SRC_COOL[url] = time.time() + secs


def is_cool(url):
    with COOL_LOCK:
        until = SRC_COOL.get(url)
        if not until:
            return False
        if until > time.time():
            return True
        SRC_COOL.pop(url, None)
        return False


def cool_state():
    """给状态页看：还有多少秒解冻。"""
    now = time.time()
    with COOL_LOCK:
        return {u: int(until - now) for u, until in SRC_COOL.items() if until > now}


def find_channel_by_url(cache, url):
    """用主源或任一备源地址反查频道，播放入口只带地址即可。"""
    if not url:
        return None
    for ch in cache.get("channels", []):
        if ch.get("url") == url or url in (ch.get("alts") or []):
            return ch
    return None


def channel_sources(ch, prefer=None):
    """主源 + 备源去重；prefer 提到最前；冷却中的源排到最后（不删，全部过期时仍能兜底）。"""
    pairs = [(ch.get("url", ""), ch.get("source", ""))]
    alts = ch.get("alts") or []
    srcs = ch.get("alt_sources") or []
    for i, u in enumerate(alts):
        pairs.append((u, srcs[i] if i < len(srcs) else ""))
    urls, seen = [], set()
    for u, _ in pairs:
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    if prefer and prefer in urls:
        urls.remove(prefer)
        urls.insert(0, prefer)
    if len(urls) > 1:
        urls.sort(key=lambda u: 1 if is_cool(u) else 0)   # 稳定排序：正常源在前
    return urls


# ---------------------------------------------------------------- HTTP

class QuietServer(ThreadingHTTPServer):
    """播放和台标请求会被浏览器随时掐断（切频道、滑走页面），
    这是正常现象，不该在日志里刷一屏 traceback。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "fnosiptv/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 辅助
    def base_url(self):
        """返回对外可用的绝对地址前缀。

        飞牛影视导入直播源时只认完整 URL，相对路径会直接拉不到内容，
        所以这里必须始终产出 http(s)://host[:port]，不能退化成空串。
        """
        conf = load_config()
        fixed = (conf["settings"].get("public_base") or "").strip().rstrip("/")
        if fixed:
            if not fixed.lower().startswith(("http://", "https://")):
                fixed = "http://" + fixed
            return fixed

        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").strip()
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip() or "http"
        if not host:
            # 少数代理会剥掉 Host，退到本机监听地址，至少保证地址可用
            try:
                ip, port = self.server.server_address[0], self.server.server_address[1]
                if ip in ("0.0.0.0", "::", ""):
                    ip = "127.0.0.1"
                host = "%s:%d" % (ip, port)
            except Exception:  # noqa: BLE001
                host = "127.0.0.1"

        # 经飞牛网关访问时路径带 /app/<name>，复制出去的地址也必须带，否则 404
        raw = getattr(self, "raw_path", "")
        if raw == PREFIX or raw.startswith(PREFIX + "/"):
            return "%s://%s%s" % (proto, host, PREFIX)
        return "%s://%s" % (proto, host)

    def strip_prefix(self):
        if self.path == PREFIX:
            self.path = "/"
        elif self.path.startswith(PREFIX + "/"):
            self.path = self.path[len(PREFIX):]

    def send_body(self, body, ctype="application/json; charset=utf-8", status=200, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        extra = dict(extra or {})
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, obj, status=200):
        self.send_body(json.dumps(obj, ensure_ascii=False), status=status)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def log_message(self, fmt, *args):
        # 播放时每秒好几个分片请求，全打进日志会把有用信息淹掉
        p = self.path or ""
        if "/api/seg" in p or "/api/stream" in p or "/api/live" in p:
            return
        addr = self.client_address[0] if isinstance(self.client_address, tuple) else "-"
        print("[web] %s %s" % (addr, fmt % args), flush=True)

    # ---- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.raw_path = self.path
        self.strip_prefix()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/admin", "/admin/"):
            return self.serve_ui()
        if path == "/health":
            return self.send_json({"status": "ok"})
        if path == "/api/state":
            return self.api_state()
        if path in ("/iptv.m3u", "/d1/iptv.m3u", "/d0/iptv.m3u"):
            # 路径写法等价于 ?d=1 / ?d=0：个别播放器会校验「地址必须以 .m3u 结尾」，
            # 带 query 就被判不合规，那就用 /d1/iptv.m3u 这种纯路径形式。
            q = dict(query)
            if path == "/d1/iptv.m3u":
                q["d"] = ["1"]
            elif path == "/d0/iptv.m3u":
                q["d"] = ["0"]
            return self.serve_m3u(q)
        if path == "/preview":
            return self.serve_preview()
        if path == "/iptv.txt":
            return self.serve_txt()
        if path == "/epg.xml":
            return self.serve_epg()
        if path.startswith("/logo/"):
            return self.serve_logo(path[6:])
        if path in ("/hls.min.js", "/hls.js"):
            return self.serve_static(os.path.basename(path))
        if path == "/api/channels":
            return self.api_channels(query)
        if path == "/api/groups":
            return self.api_groups()
        if path == "/api/channel":
            return self.api_channel(query)
        if path == "/api/play":
            return self.api_play(query)
        if path in ("/api/stream", "/api/seg"):
            return self.proxy_stream(path, query)
        if path == "/api/live":
            return self.api_live(query)
        if path == "/api/probe/status":
            with LOCK:
                return self.send_json({"probe": dict(STATE["probe"])})
        if path == "/api/log":
            return self.send_json({"log": read_log_tail(int(query.get("n", ["80"])[0]))})
        return self.send_json({"error": "not found"}, 404)

    # ---- POST
    def do_POST(self):
        self.raw_path = self.path
        self.strip_prefix()
        path = urllib.parse.urlparse(self.path).path
        payload = self.read_json()

        if path == "/api/refresh":
            threading.Thread(target=refresh, kwargs={"conf": load_config()}, daemon=True).start()
            return self.send_json({"ok": True, "message": "刷新已开始"})
        if path == "/api/source":
            return self.api_source(payload)
        if path == "/api/settings":
            return self.api_settings(payload)
        if path == "/api/selection":
            return self.api_selection(payload)
        if path == "/api/probe":
            limit = int((payload or {}).get("limit") or 0)
            threading.Thread(target=probe_all, kwargs={"limit": limit}, daemon=True).start()
            return self.send_json({"ok": True, "message": "开始测活"})
        if path == "/api/logos/prewarm":
            chs = load_cache().get("channels", [])
            threading.Thread(target=prewarm_logos, args=(chs,), daemon=True).start()
            return self.send_json({"ok": True, "queued": len(chs)})
        if path == "/api/probe/prune":
            return self.api_prune(payload)
        if path == "/api/correct":
            threading.Thread(target=correct_all, daemon=True).start()
            return self.send_json({"ok": True, "message": "开始源校正"})
        if path == "/api/correct/prune":
            return self.api_prune_ad(payload)
        if path == "/api/manage":
            return self.api_manage(payload)
        if path == "/api/probe/reset":
            conf = load_config()
            conf["settings"]["blocked"] = []
            conf["settings"]["blocked_meta"] = {}
            conf["settings"]["only_alive"] = False
            save_config(conf)
            return self.send_json({"ok": True})
        if path == "/api/channel/probe":
            return self.api_channel_probe(payload)
        if path == "/api/channel/pick":
            return self.api_channel_pick(payload)
        if path == "/api/group":
            return self.api_group(payload)
        if path == "/api/channel/order":
            return self.api_channel_order(payload)
        if path == "/api/channel/rename":
            return self.api_channel_rename(payload)
        return self.send_json({"error": "not found"}, 404)

    # ---- 业务
    def serve_ui(self):
        index = os.path.join(UI_DIR, "index.html")
        try:
            with open(index, "rb") as fh:
                return self.send_body(fh.read(), "text/html; charset=utf-8")
        except OSError:
            return self.send_body("UI 缺失：%s" % index, "text/plain; charset=utf-8", 500)

    def serve_static(self, name):
        ctype = {".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".png": "image/png", ".svg": "image/svg+xml"}.get(os.path.splitext(name)[1], "application/octet-stream")
        p = os.path.join(UI_DIR, os.path.basename(name))
        try:
            with open(p, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_body("not found", "text/plain; charset=utf-8", 404)
        return self.send_body(body, ctype, extra={"Cache-Control": "max-age=86400"})

    def api_state(self):
        conf = load_config()
        cache = load_cache()
        base = self.base_url()
        channels = cache.get("channels", [])
        keep = conf["settings"].get("keep") or []
        stats = dict(cache.get("stats", {}))
        stats["output"] = len(output_channels(cache, conf))
        stats["alive"] = sum(1 for c in channels if c.get("alive"))
        stats["dead"] = sum(1 for c in channels if c.get("alive") is False)
        stats["probed_at"] = cache.get("probed_at", 0)
        stats["ad"] = sum(1 for c in channels
                          if c.get("verdict") == "ad" or c.get("verdict2") == "ad")
        bset = set(conf["settings"].get("blocked") or [])
        stats["blocked"] = sum(1 for c in channels if c.get("name") in bset)
        stats["doubt"] = sum(1 for c in channels
                             if (c.get("verdict") == "doubt" or c.get("verdict2") == "doubt")
                             and not (c.get("verdict") == "ad" or c.get("verdict2") == "ad"))
        stats["corrected_at"] = cache.get("corrected_at", 0)
        stats["cool"] = len(cool_state())   # 因切片过期被暂时跳过的源数量
        return self.send_json({
            "app": "fnosiptv",
            "version": APP_VERSION,
            "data_dir": DATA_DIR,
            "settings": conf["settings"],
            "sources": conf["sources"],
            "keep": keep,
            "stats": stats,
            "report": cache.get("report", []),
            "updated_at": cache.get("updated_at", 0),
            "refreshing": STATE["refreshing"],
            "progress": STATE["progress"],
            "correct": STATE["correct"],
            "last_error": STATE["last_error"],
            "subscribe": {
                "m3u": base + "/iptv.m3u",
                "txt": base + "/iptv.txt",
                "epg": base + "/epg.xml",
                "preview": base + "/preview",
                "admin": base + "/admin",
            },
            "preview": channels[:300],
        })

    def api_prune(self, payload):
        """把测活为失效的频道加入屏蔽名单，之后重抓也不会再输出。"""
        conf = load_config()
        cache = load_cache()
        dead = [c["name"] for c in cache.get("channels", []) if c.get("alive") is False]
        if not dead:
            return self.send_json({"ok": False, "message": "还没有失效频道，先测活"})
        blocked = block_names(conf, dead, why="测活失效")
        save_config(conf)
        return self.send_json({"ok": True, "blocked": blocked, "removed": len(dead)})

    def api_prune_ad(self, payload):
        """把判定为广告/录播的频道加入屏蔽名单。"""
        conf = load_config()
        cache = load_cache()
        bad = [c["name"] for c in cache.get("channels", [])
               if c.get("verdict") == "ad" or c.get("verdict2") == "ad"]
        if not bad:
            return self.send_json({"ok": False, "message": "没有判定为广告的频道，先做源校正"})
        blocked = block_names(conf, bad, why="判定广告")
        save_config(conf)
        return self.send_json({"ok": True, "removed": len(bad), "blocked": blocked})

    def api_manage(self, payload):
        """频道管理统一入口：屏蔽 / 恢复 / 仅保留 / 按筛选批量屏蔽。

        全部基于「屏蔽名单」实现：批量操作在服务端跑完 649 个频道，
        不依赖前端分页有没有加载到，避免"只处理了当前 60 条"。
        """
        payload = payload or {}
        op = payload.get("op") or ""
        conf = load_config()
        cache = load_cache()
        allch = cache.get("channels", [])
        all_names = [c.get("name") for c in allch if c.get("name")]

        f = payload.get("filter") or {}
        def picked(include_blocked=False):
            rows = allch
            bs = set(conf["settings"].get("blocked") or [])
            if f.get("blk") == "1":
                rows = [c for c in rows if c.get("name") in bs]
            elif not include_blocked:
                # 「屏蔽这批」只处理当前可见的，避免把已屏蔽的重复计入
                rows = [c for c in rows if c.get("name") not in bs]
            return [c.get("name") for c in filter_rows(
                rows, f.get("group", ""), f.get("alive", ""), f.get("ad", ""), (f.get("q") or "").strip())]

        if op == "block":
            n = block_names(conf, payload.get("names") or [], why=payload.get("why") or "手动")
        elif op == "unblock":
            n = unblock_names(conf, payload.get("names") or [])
        elif op == "block_filter":
            names = picked()
            n = block_names(conf, names, why=payload.get("why") or "按筛选屏蔽")
            payload["affected"] = len(names)
        elif op == "unblock_filter":
            names = picked(include_blocked=True)
            n = unblock_names(conf, names)
            payload["affected"] = len(names)
        elif op == "only":
            keep = [str(x) for x in (payload.get("names") or []) if x]
            if not keep and payload.get("filter") is not None:
                keep = [x for x in picked(include_blocked=True) if x]
            if not keep:
                return self.send_json({"ok": False, "message": "至少要保留一个频道"}, 400)
            unblock_names(conf, keep)
            rest = [x for x in all_names if x not in set(keep)]
            n = block_names(conf, rest, why="仅保留")
            payload["affected"] = len(rest)
        elif op == "unblock_all":
            st = conf["settings"]
            st["blocked"] = []
            st["blocked_meta"] = {}
            n = 0
        else:
            return self.send_json({"ok": False, "message": "未知操作"}, 400)

        save_config(conf)
        conf = load_config()
        return self.send_json({
            "ok": True, "blocked": n, "affected": payload.get("affected", 0),
            "total": len(all_names), "output": len(output_channels(cache, conf)),
        })

    # ---- 分组与排序
    def api_groups(self):
        conf = load_config()
        cache = load_cache()
        st = conf["settings"]
        blocked = set(st.get("blocked") or [])
        hidden = set(st.get("hidden_groups") or [])
        ga = st.get("group_alias") or {}
        chs = [c for c in cache.get("channels", []) if c.get("name") not in blocked]

        agg = {}
        order = []
        for c in order_channels(chs, st):
            g = group_of(c)
            if g not in agg:
                agg[g] = {"key": g, "count": 0, "alive": 0, "dead": 0, "ad": 0}
                order.append(g)
            a = agg[g]
            a["count"] += 1
            if c.get("alive") is True:
                a["alive"] += 1
            elif c.get("alive") is False:
                a["dead"] += 1
            if c.get("verdict") == "ad" or c.get("verdict2") == "ad":
                a["ad"] += 1

        groups = []
        for g in order:
            a = agg[g]
            a["name"] = ga.get(g) or g
            a["hidden"] = g in hidden
            groups.append(a)
        return self.send_json({
            "groups": groups,
            "hidden": len([g for g in order if g in hidden]),
            "all": len(cache.get("channels", [])),
            "visible": len(chs),
            "total": len(chs),
            "output": len(output_channels(cache, conf)),
            "blocked": len(blocked),
        })

    def api_group(self, payload):
        payload = payload or {}
        action = payload.get("action")
        key = (payload.get("group") or "").strip()
        conf = load_config()
        cache = load_cache()
        st = conf["settings"]
        all_groups = []
        for c in order_channels(cache.get("channels", []), st):
            g = group_of(c)
            if g not in all_groups:
                all_groups.append(g)

        if action == "rename":
            if not key:
                return self.send_json({"error": "缺少分组"}, 400)
            alias = dict(st.get("group_alias") or {})
            new = (payload.get("name") or "").strip()
            if new and new != key:
                alias[key] = new[:40]
            else:
                alias.pop(key, None)
            st["group_alias"] = alias
        elif action in ("hide", "show"):
            hidden = set(st.get("hidden_groups") or [])
            if action == "hide":
                hidden.add(key)
            else:
                hidden.discard(key)
            st["hidden_groups"] = [g for g in all_groups if g in hidden]
        elif action == "only":
            st["hidden_groups"] = [g for g in all_groups if g != key]
        elif action == "show_all":
            st["hidden_groups"] = []
        elif action in ("move", "top", "bottom"):
            go = [g for g in (st.get("group_order") or []) if g in all_groups]
            for g in all_groups:
                if g not in go:
                    go.append(g)
            if key not in go:
                return self.send_json({"error": "分组不存在"}, 404)
            i = go.index(key)
            if action == "move":
                j = i + (1 if int(payload.get("delta") or 0) > 0 else -1)
                if 0 <= j < len(go):
                    go[i], go[j] = go[j], go[i]
            else:
                go.pop(i)
                go.insert(0 if action == "top" else len(go), key)
            st["group_order"] = go
        else:
            return self.send_json({"error": "未知操作"}, 400)

        save_config(conf)
        return self.send_json({"ok": True, "output": len(output_channels(cache, load_config()))})

    def api_channel_order(self, payload):
        """组内排序：上移/下移/置顶/置底。顺序写进 channel_order，重抓后仍有效。"""
        payload = payload or {}
        name = payload.get("name")
        if not name:
            return self.send_json({"error": "缺少频道"}, 400)
        conf = load_config()
        cache = load_cache()
        st = conf["settings"]
        chs = cache.get("channels", [])
        gmap = {c.get("name"): group_of(c) for c in chs}
        if name not in gmap:
            return self.send_json({"error": "频道不存在"}, 404)
        seq = [c.get("name") for c in order_channels(chs, st)]
        grp = gmap[name]
        pos = seq.index(name)
        todo = payload.get("to")

        if todo in ("top", "bottom"):
            seq.pop(pos)
            idxs = [i for i, n in enumerate(seq) if gmap.get(n) == grp]
            if todo == "top":
                at = min(idxs) if idxs else 0
            else:
                at = (max(idxs) + 1) if idxs else len(seq)
            seq.insert(at, name)
        else:
            j = pos + (1 if int(payload.get("delta") or 0) > 0 else -1)
            if 0 <= j < len(seq) and gmap.get(seq[j]) == grp:
                seq[pos], seq[j] = seq[j], seq[pos]
        st["channel_order"] = seq
        save_config(conf)
        return self.send_json({"ok": True})

    def api_channel_rename(self, payload):
        """自定义显示名。缓存里存原名，只改输出用的名字。"""
        payload = payload or {}
        name = payload.get("name")
        if not name:
            return self.send_json({"error": "缺少频道"}, 400)
        conf = load_config()
        alias = dict(conf["settings"].get("name_alias") or {})
        new = (payload.get("alias") or "").strip()
        if new and new != name:
            alias[name] = new[:40]
        else:
            alias.pop(name, None)
        conf["settings"]["name_alias"] = alias
        save_config(conf)
        return self.send_json({"ok": True, "alias": alias.get(name, "")})

    def api_channel(self, query):
        """单个频道的全部地址（主源 + 备源）及各自的测活状态。"""
        name = (query.get("name") or [""])[0]
        cache = load_cache()
        ch = next((c for c in cache.get("channels", []) if c.get("name") == name), None)
        if not ch:
            return self.send_json({"error": "频道不存在"}, 404)
        known = cache.get("srcprobe") or {}
        urls = [(ch.get("url", ""), ch.get("source", ""), True)]
        alts = ch.get("alts") or []
        srcs = ch.get("alt_sources") or []
        for i, u in enumerate(alts):
            urls.append((u, srcs[i] if i < len(srcs) else "", False))
        items = []
        for u, src, is_main in urls:
            p = known.get(u) or {}
            items.append({
                "url": u, "source": src, "main": is_main,
                "alive": p.get("alive"),
                "note": p.get("note", ""),
                "at": p.get("at", 0),
                "score": core.url_score(u, 0)[0],
            })
        return self.send_json({
            "name": ch.get("name"), "group": ch.get("group", ""),
            "alive": ch.get("alive"), "probe": ch.get("probe", ""),
            "picked": bool(ch.get("picked")),
            "sources": items,
        })

    def api_channel_probe(self, payload):
        """只测一个频道：主源 + 全部备源，逐个给出结论。"""
        name = (payload or {}).get("name")
        cache = load_cache()
        ch = next((c for c in cache.get("channels", []) if c.get("name") == name), None)
        if not ch:
            return self.send_json({"error": "频道不存在"}, 404)
        urls = [ch.get("url", "")] + list(ch.get("alts") or [])
        urls = [u for u in dict.fromkeys(urls) if u]

        def work(u):
            ok, note = core.probe(u, timeout=8)
            return u, bool(ok), str(note)[:80]

        results = {}
        with core.ThreadPoolExecutor(max_workers=8) as pool:
            for u, ok, note in pool.map(work, urls):
                results[u] = {"alive": ok, "note": note, "at": core.now()}

        with LOCK:
            fresh = load_cache()
            sp = fresh.get("srcprobe") or {}
            sp.update(results)
            if len(sp) > 6000:   # 别让这张表无限涨
                keep = sorted(sp.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)[:6000]
                sp = dict(keep)
            fresh["srcprobe"] = sp
            for c in fresh.get("channels", []):
                if c.get("name") == name:
                    main = results.get(c.get("url"))
                    if main:
                        c["alive"] = main["alive"]
                        c["probe"] = main["note"]
                        c["probed_at"] = main["at"]
                    break
            _save(CACHE_FILE, fresh)
        return self.send_json({"ok": True, "results": results})

    def api_channel_pick(self, payload):
        """把这个地址设为主源。选择会记住，重新抓取后自动恢复。"""
        payload = payload or {}
        name, url = payload.get("name"), payload.get("url")
        if not name or not url:
            return self.send_json({"error": "参数不全"}, 400)
        with LOCK:
            cache = load_cache()
            ch = next((c for c in cache.get("channels", []) if c.get("name") == name), None)
            if not ch:
                return self.send_json({"error": "频道不存在"}, 404)
            if not reorder_sources(ch, url):
                return self.send_json({"error": "这个地址不属于该频道"}, 400)
            picked = cache.get("picked") or {}
            picked[name] = url
            cache["picked"] = picked
            ch["picked"] = True
            ch["alive"] = None        # 换了地址，旧的可用性结论作废
            ch["probe"] = ""
            _save(CACHE_FILE, cache)
        return self.send_json({"ok": True, "url": url})

    def proxy_stream(self, path, query):
        """HLS 代理：/api/stream 返回重写后的清单，/api/seg 转发分片。

        这一层决定「能不能播」：
          · 302 跳转后必须用最终地址做相对路径基准，否则切片全是 404；
          · 切片失败重试一次，源站抖一下不该让画面卡住；
          · 上游 404 原样返回，播放器才能据此换源重试，而不是傻等。
        """
        token = (query.get("u") or [""])[0]
        if not token:
            return self.send_body("缺少 u 参数", "text/plain; charset=utf-8", 400)
        try:
            url = d64(token)
        except Exception:  # noqa: BLE001
            return self.send_body("地址解码失败", "text/plain; charset=utf-8", 400)
        if not url.lower().startswith(("http://", "https://")):
            return self.send_body("只支持 http(s) 地址", "text/plain; charset=utf-8", 400)

        src = ""
        stoken = (query.get("s") or [""])[0]
        if stoken:
            try:
                src = d64(stoken)
            except Exception:  # noqa: BLE001
                src = ""

        direct = (query.get("d") or [""])[0] == "1"
        if path == "/api/stream":
            return self.send_playlist(url, src, direct)

        # 分片：透传 Range，边读边写，不占内存
        extra = {"Accept-Encoding": "identity"}
        rng = self.headers.get("Range")
        if rng:
            extra["Range"] = rng

        resp, last = None, None
        for attempt in range(2):
            try:
                resp = open_stream(url, timeout=20, extra=extra)
                break
            except urllib.error.HTTPError as exc:
                # 403/404/410 多半是该源切片签名过期或时移窗口播完：标记该源冷却，
                # 并回 404 让播放器尽快重载清单 —— 重载时就会换到活源，画面接着走。
                if exc.code in (403, 404, 410):
                    mark_cool(src)
                    return self.send_body("分片不可用，稍后自动换源",
                                          "text/plain; charset=utf-8", 404)
                return self.send_body("分片返回 %d" % exc.code,
                                      "text/plain; charset=utf-8", 502)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt == 0:
                    time.sleep(0.2)
        if resp is None:
            return self.send_body("分片拉取失败：%s" % str(last)[:160],
                                  "text/plain; charset=utf-8", 504)

        try:
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            length = resp.headers.get("Content-Length")
            code = 206 if resp.status == 206 else 200
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length:
                self.send_header("Content-Length", length)
            for h in ("Content-Range", "Accept-Ranges", "ETag"):
                v = resp.headers.get(h)
                if v:
                    self.send_header(h, v)
            if not length:
                self.send_header("Connection", "close")
                self.close_connection = True
            # 切片内容不会再变：短缓存能让回看/拖动重复请求直接命中，少一次跨境往返
            self.send_header("Cache-Control", "private, max-age=20")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            # 上游中途断开：连接已经写出去了，只能放弃这一块，让播放器自己补
            print("[seg] 中断: %s" % str(exc)[:80], flush=True)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def send_playlist(self, url, src="", direct=False):
        """拉取并重写 m3u8。src 标明这份清单来自哪个源，传给切片以便失败时换源。
        direct=True 时切片直连源站（省 NAS 流量），来自 /api/live 链路才会置位。"""
        src = src or url
        try:
            resp = open_stream(url, timeout=8)
            raw = resp.read(4 * 1024 * 1024)
            final = resp.geturl() or url      # 跟随 302 后的真实地址
            resp.close()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                mark_cool(src)
            return self.send_body("播放列表返回 %d" % exc.code,
                                  "text/plain; charset=utf-8", 502)
        except Exception as exc:  # noqa: BLE001
            return self.send_body("拉取播放列表失败：%s" % str(exc)[:160],
                                  "text/plain; charset=utf-8", 504)
        text = core.decode(raw)
        if not text.lstrip().startswith("#EXTM3U"):
            # 不是 HLS：多半是直接给了 mp4/flv，交给播放器原生能力
            return self.send_body("这个地址返回的不是 HLS 播放列表，可能已失效",
                                  "text/plain; charset=utf-8", 415)
        body = rewrite_playlist(text, final, src, direct)
        return self.send_body(body, "application/vnd.apple.mpegurl",
                              extra={"Access-Control-Allow-Origin": "*",
                                     "Cache-Control": "no-cache"})

    def api_live(self, query):
        """频道稳定入口：/api/live?u=<base64(主源)>，M3U 里每个 m3u8 频道都指向这里。

        每次请求都实时重取清单；主源过期（签名失效 / 时移窗口播完）就自动换下一个备源，
        再把子清单与切片重写成带源标记的本机地址。切片一旦 403/404，标记该源冷却，
        下次取清单直接跳过 —— 播放器重载清单时就已经换到活源，画面接着走。
        """
        token = (query.get("u") or [""])[0]
        if not token:
            return self.send_body("缺少 u 参数", "text/plain; charset=utf-8", 400)
        try:
            url = d64(token)
        except Exception:  # noqa: BLE001
            return self.send_body("地址解码失败", "text/plain; charset=utf-8", 400)
        if not url.lower().startswith(("http://", "https://")):
            return self.send_body("只支持 http(s) 地址", "text/plain; charset=utf-8", 400)

        conf = load_config()
        ch = find_channel_by_url(load_cache(), url)
        if ch and conf["settings"].get("auto_renew", True):
            cands = channel_sources(ch, prefer=url)
        else:
            cands = [url]

        def grab(u):
            resp = open_stream(u, timeout=7)
            raw = resp.read(4 * 1024 * 1024)
            final = resp.geturl() or u
            resp.close()
            text = core.decode(raw)
            return (text, final) if text.lstrip().startswith("#EXTM3U") else (None, final)

        text = final = None
        used = ""
        last = "无可用地址"
        # 第一轮：跳过冷却中的源
        for u in cands:
            if len(cands) > 1 and is_cool(u):
                continue
            try:
                text, final = grab(u)
                if text:
                    used = u
                    break
                last = "不是 HLS 清单"
            except urllib.error.HTTPError as exc:
                last = "HTTP %d" % exc.code
                if exc.code in (403, 404, 410):
                    mark_cool(u)
            except Exception as exc:  # noqa: BLE001
                last = str(exc)[:120]
        # 第二轮：全部冷却/失败时兜底，宁可试一遍也不让频道黑屏
        if text is None:
            for u in cands:
                try:
                    text, final = grab(u)
                    if text:
                        used = u
                        break
                except Exception:  # noqa: BLE001
                    continue
        if text is None:
            return self.send_body("所有源暂时不可用：%s" % last,
                                  "text/plain; charset=utf-8", 502)

        # 切片策略由订阅地址带下来：d=0 走代理（网页端），d=1 直连（手机端省流量）。
        # 老订阅地址没有 d，回退到全局开关，行为和升级前一致。
        dparam = (query.get("d") or [""])[0]
        if dparam in ("0", "1"):
            direct = dparam == "1"
        else:
            direct = bool(conf["settings"].get("seg_direct", True))
        body = rewrite_playlist(text, final, used, direct)
        return self.send_body(body, "application/vnd.apple.mpegurl",
                              extra={"Access-Control-Allow-Origin": "*",
                                     "Cache-Control": "no-cache"})

    def api_play(self, query):
        """播放用的候选源顺序：可用 → 未测 → 失效；同档内主源优先、然后看信任分。

        前端按这个顺序逐个试，播不出来自动跳下一个，不用人守着点。
        """
        name = (query.get("name") or [""])[0]
        cache = load_cache()
        ch = next((c for c in cache.get("channels", []) if c.get("name") == name), None)
        if not ch:
            return self.send_json({"error": "频道不存在"}, 404)

        known = cache.get("srcprobe") or {}
        pairs = [(ch.get("url", ""), ch.get("source", ""), True)]
        alts = ch.get("alts") or []
        srcs = ch.get("alt_sources") or []
        for i, u in enumerate(alts):
            pairs.append((u, srcs[i] if i < len(srcs) else "", False))

        items, seen = [], set()
        for u, src, is_main in pairs:
            if not u or u in seen:
                continue
            seen.add(u)
            p = known.get(u) or {}
            items.append({
                "url": u, "source": src, "main": is_main,
                "alive": p.get("alive"), "note": p.get("note", ""),
                "score": core.url_score(u, 0)[0],
            })

        def rank(it):
            al = it["alive"]
            tier = 0 if al is True else (1 if al is None else 2)
            return (tier, 0 if it["main"] else 1, -it["score"])

        items.sort(key=rank)
        return self.send_json({
            "name": ch.get("name"), "group": ch.get("group", ""),
            "picked": bool(ch.get("picked")), "sources": items,
        })

    def api_channels(self, query):
        """频道分页接口。

        306 条一次性推给手机浏览器容易超时/白屏（这就是「显示不全」的根因），
        所以这里分页 + 支持按分组和可用性筛选。
        """
        def q1(key, default=""):
            v = query.get(key)
            return v[0] if v else default

        cache = load_cache()
        allch = cache.get("channels", [])
        conf = load_config()
        keep = conf["settings"].get("keep") or []
        blocked = conf["settings"].get("blocked") or []
        kept = set(keep) if keep else None

        group = q1("group")
        alive = q1("alive")
        ad = q1("ad")
        kw = q1("q").strip().lower()
        blk = q1("blk")
        blocked_set = set(blocked)
        hide_blocked = bool(conf["settings"].get("hide_blocked", True))

        rows = allch
        if blk == "1":
            rows = [c for c in rows if c.get("name") in blocked_set]
        elif blk == "0" or hide_blocked:
            rows = [c for c in rows if c.get("name") not in blocked_set]
        rows = filter_rows(rows, group, alive, ad, kw)
        # 与 M3U 用同一套顺序，管理页调完顺序能马上对上播放器里的排列
        rows = order_channels(rows, conf["settings"])

        total = len(rows)
        try:
            offset = max(0, int(q1("offset", "0") or 0))
            limit = int(q1("limit", "60") or 60)
        except ValueError:
            offset, limit = 0, 60
        limit = max(1, min(limit, 500))

        page = []
        picked = cache.get("picked") or {}
        meta = conf["settings"].get("blocked_meta") or {}
        alias = conf["settings"].get("name_alias") or {}
        galias = conf["settings"].get("group_alias") or {}
        for c in rows[offset:offset + limit]:
            item = dict(c)
            if kept is None:
                item["selected"] = c.get("name") not in blocked_set
            else:
                item["selected"] = c.get("name") in kept
            item["blocked"] = c.get("name") in blocked
            item["disp"] = alias.get(c.get("name")) or c.get("name")
            item["group_disp"] = galias.get(c.get("group") or "其他") or (c.get("group") or "其他")
            item["blocked_why"] = (meta.get(c.get("name")) or {}).get("why", "")
            item["blocked_at"] = (meta.get(c.get("name")) or {}).get("t", 0)
            item["picked"] = c.get("name") in picked
            page.append(item)

        # 分组计数按「当前视图可见集合」算，否则 tab 上写着 234、点进去只有 100
        if blk == "1":
            visible = [c for c in allch if c.get("name") in blocked_set]
        elif hide_blocked:
            visible = [c for c in allch if c.get("name") not in blocked_set]
        else:
            visible = allch
        groups = {}
        for c in visible:
            g = c.get("group") or "其他"
            groups[g] = groups.get(g, 0) + 1

        return self.send_json({
            "channels": page,
            "total": total,
            "all": len(allch),
            "visible": len(visible),
            "blocked": len(blocked_set),
            "output": len(output_channels(cache, conf)),
            "offset": offset,
            "limit": limit,
            "groups": groups,
            "alive": sum(1 for c in allch if c.get("alive")),
            "dead": sum(1 for c in allch if c.get("alive") is False),
            "probed_at": cache.get("probed_at", 0),
            "updated_at": cache.get("updated_at", 0),
        })

    def serve_logo(self, slug):
        """台标代理：飞牛影视拉不到 imgur 这类外链，统一由本机出图并落地缓存。"""
        slug = slug.split("?")[0]
        if slug.endswith(".png"):
            slug = slug[:-4]
        if not re.fullmatch(r"[0-9a-f]{2,400}", slug or ""):
            return self.send_body(BLANK_PNG, "image/png", 404)

        path = os.path.join(LOGO_DIR, slug + ".bin")
        meta = path + ".json"
        if os.path.exists(path):
            ext = (_load(meta, {}) or {}).get("ext", ".png")
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                return self.send_body(data, LOGO_CTYPE.get(ext, "image/png"),
                                      extra={"Cache-Control": "public, max-age=604800"})
            except OSError:
                pass

        name = core.slug_name(slug)
        ch = None
        for c in load_cache().get("channels", []):
            if core.logo_slug(c.get("name", "")) == slug:
                ch = c
                break
        if ch is None and name:
            ch = {"name": name, "logo": ""}

        data, ext = core.fetch_logo(ch) if ch else (None, "")
        if data:
            try:
                os.makedirs(LOGO_DIR, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
                _save(meta, {"ext": ext, "name": name})
            except OSError:
                pass
            return self.send_body(data, LOGO_CTYPE.get(ext, "image/png"),
                                  extra={"Cache-Control": "public, max-age=604800"})

        # 取不到就给占位图并短缓存：飞牛会显示它，而不是无穷转圈
        return self.send_body(BLANK_PNG, "image/png", 200,
                              extra={"Cache-Control": "public, max-age=86400"})

    def api_source(self, payload):
        action = payload.get("action")
        conf = load_config()
        sources = conf["sources"]

        if action == "add":
            value = (payload.get("value") or "").strip()
            if not value:
                return self.send_json({"ok": False, "message": "内容不能为空"}, 400)
            sid = "s%d" % (int(time.time() * 1000) % 10 ** 12)
            sources.append({
                "id": sid,
                "name": (payload.get("name") or "新源").strip()[:40],
                "kind": payload.get("kind", "url"),
                "value": value,
                "enabled": True,
                "priority": int(payload.get("priority") or 100),
                "last_status": "",
                "last_count": 0,
                "last_time": 0,
            })
        elif action == "delete":
            conf["sources"] = [s for s in sources if s["id"] != payload.get("id")]
        elif action == "toggle":
            for s in sources:
                if s["id"] == payload.get("id"):
                    s["enabled"] = bool(payload.get("enabled"))
        elif action == "update":
            for s in sources:
                if s["id"] == payload.get("id"):
                    for key in ("name", "value", "priority"):
                        if key in payload:
                            s[key] = payload[key]
        elif action == "move":
            delta = int(payload.get("delta") or 0)
            idx = next((i for i, s in enumerate(sources) if s["id"] == payload.get("id")), None)
            if idx is not None and 0 <= idx + delta < len(sources):
                sources[idx], sources[idx + delta] = sources[idx + delta], sources[idx]
        else:
            return self.send_json({"ok": False, "message": "未知操作"}, 400)

        save_config(conf)
        if action in ("add", "update", "toggle", "delete"):
            threading.Thread(target=refresh, kwargs={"conf": conf, "fetch_epg": False},
                             daemon=True).start()
        return self.send_json({"ok": True})

    def api_selection(self, payload):
        """勾选 = 是否输出。存黑名单（blocked）而不是白名单：
        频道有 300+ 个且分页展示，白名单一旦提交，没加载到的频道会被全部误删。
        """
        conf = load_config()
        st = conf["settings"]
        blocked = set(st.get("blocked") or [])

        if isinstance(payload.get("blocked"), list):
            blocked = {str(x) for x in payload["blocked"]}
        blocked |= {str(x) for x in (payload.get("add_blocked") or [])}
        blocked -= {str(x) for x in (payload.get("del_blocked") or [])}

        # 兼容旧版：keep 是白名单，转成等价的黑名单，然后清空
        keep = payload.get("keep")
        if isinstance(keep, list):
            allnames = {c.get("name", "") for c in load_cache().get("channels", [])}
            if keep:
                blocked |= (allnames - {str(x) for x in keep})
            st["keep"] = []

        st["blocked"] = sorted(blocked)[:20000]
        st["blocked_meta"] = {k: v for k, v in (st.get("blocked_meta") or {}).items()
                              if k in blocked}
        save_config(conf)

        cache = load_cache()
        total = len(cache.get("channels", []))
        return self.send_json({"ok": True, "total": total,
                               "blocked": len(st["blocked"]),
                               "output": len(output_channels(cache, conf))})

    def api_settings(self, payload):
        conf = load_config()
        for key, val in (payload or {}).items():
            if key in DEFAULT_SETTINGS:
                conf["settings"][key] = val
        save_config(conf)
        if payload.get("epg_urls") is not None:
            try:
                refresh_epg(conf["settings"])
            except Exception:  # noqa: BLE001
                pass
        return self.send_json({"ok": True, "settings": conf["settings"]})

    def serve_m3u(self, query=None):
        """订阅输出。切片策略按地址自动分配，两条订阅用同一个频道表：

          /iptv.m3u      → 切片走本机代理（给飞牛影视网页端：浏览器有 CORS，直连拉不到）
          /iptv.m3u?d=1  → 切片直连源站（给手机/盒子：NAS 只发几百 KB 清单，不转发大流量）
        """
        query = query or {}
        # 默认走代理（网页端最稳），只有显式 d=1 才切片直连
        direct = (query.get("d") or [""])[0] == "1"
        cache = load_cache()
        conf = load_config()
        channels = output_channels(cache, conf)
        base = self.base_url()
        epg_conf = (load_config()["settings"].get("epg_urls") or "").strip()
        epg = base + "/epg.xml" if epg_conf else ""
        if conf["settings"].get("m3u_proxy"):
            # 走 /api/live：实时重取清单 + 源过期自动换源，治「播一段就停」。
            # 已是 m3u8 的才接管；mp4/flv 直链代理没意义还会拖慢。
            proxied = []
            for c in channels:
                u = c.get("url") or ""
                if u.lower().split("?")[0].endswith(".m3u8"):
                    d = dict(c)
                    # d 显式写进地址：播放器每次取清单都带着，策略不会串到另一条订阅
                    d["url"] = "%s/api/live?u=%s&d=%d" % (base, e64(u), 1 if direct else 0)
                    proxied.append(d)
                else:
                    proxied.append(c)
            channels = proxied
        logo_mode = "cdn" if conf["settings"].get("logo_cdn", True) else "local"
        body = core.to_m3u(channels, epg_url=epg, public_base=base,
                           logos=bool(conf["settings"].get("local_logo", True)),
                           logo_mode=logo_mode)
        return self.send_body(body, "audio/x-mpegurl; charset=utf-8")

    def serve_preview(self):
        cache = load_cache()
        conf = load_config()
        allch = cache.get("channels", [])
        channels = output_channels(cache, conf)
        body = preview_html(channels, self.base_url(), cache.get("updated_at", 0),
                            total=len(allch))
        return self.send_body(body, "text/html; charset=utf-8")

    def serve_txt(self):
        cache = load_cache()
        conf = load_config()
        channels = output_channels(cache, conf)
        return self.send_body(core.to_txt(channels), "text/plain; charset=utf-8")

    def serve_epg(self):
        if os.path.exists(EPG_FILE):
            with open(EPG_FILE, "rb") as fh:
                return self.send_body(fh.read(), "application/xml; charset=utf-8")
        conf = load_config()
        if (conf["settings"].get("epg_urls") or "").strip():
            try:
                refresh_epg(conf["settings"])
            except Exception:  # noqa: BLE001
                pass
        if os.path.exists(EPG_FILE):
            with open(EPG_FILE, "rb") as fh:
                return self.send_body(fh.read(), "application/xml; charset=utf-8")
        return self.send_body('<?xml version="1.0" encoding="UTF-8"?>\n<tv></tv>\n',
                              "application/xml; charset=utf-8")


def read_log_tail(n=80):
    candidates = [os.path.join(DATA_DIR, "app.log"), os.environ.get("TRIM_TEMP_LOGFILE") or ""]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    return "".join(fh.readlines()[-n:])
            except OSError:
                continue
    return ""


# ---------------------------------------------------------------- 启动

PREVIEW_CSS = """
:root{--bg:#0b0e14;--panel:#121823;--line:#212b3a;--text:#e6ecf5;--muted:#8494ab;--accent:#3ea6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
header{padding:20px 18px 12px;border-bottom:1px solid var(--line)}
h1{margin:0 0 6px;font-size:18px;font-weight:600}
.meta{color:var(--muted);font-size:12.5px}
.tip{margin:14px 18px;padding:11px 13px;background:#132033;border:1px solid #1d3050;border-radius:9px;
  font-size:12.5px;color:#a9c2e0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tip code{font-family:ui-monospace,Menlo,Consolas,monospace;color:#8fc2ff;word-break:break-all;
  min-width:0;flex:1 1 220px}
.tabs{display:flex;gap:8px;overflow-x:auto;padding:4px 18px 12px;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{flex:none;background:var(--panel);border:1px solid var(--line);color:var(--muted);
  border-radius:99px;padding:6px 12px;font-size:12.5px}
.tab.on{background:#16324f;border-color:#2c5f8f;color:#cfe6ff}
html,body{max-width:100%;overflow-x:hidden}
table{width:100%;border-collapse:collapse;table-layout:fixed}
th:first-child,td.n{width:46px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);font-weight:500;font-size:12px;background:var(--panel)}
td.n{color:#5f6f86;width:52px}
td.nm{font-weight:500;word-break:break-word;min-width:96px}
.g{display:inline-block;padding:1px 7px;border-radius:5px;background:#1b2637;color:#93a8c4;font-size:11.5px}
td.u{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;color:#6f8098;
  white-space:nowrap;
  max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
button{border:1px solid var(--line);background:#18212e;color:#cfe0f5;border-radius:7px;
  padding:4px 10px;font-size:12px}
@media (max-width:700px){td.u,th.u{display:none}th,td{padding:9px 10px}td.n{width:38px}
  .tip{margin:12px 14px;font-size:12px}}
"""


def preview_html(channels, base, updated, total=None):
    """频道预览页。

    浏览器无法播放 .m3u 文件（会当成媒体文件打开，显示黑屏 0:00），
    所以「打开预览」必须落到这个 HTML 页，而不是订阅地址本身。
    """
    esc = htmlmod.escape
    groups = []
    for ch in channels:
        g = ch.get("group") or "其他"
        if g not in groups:
            groups.append(g)

    m3u = base + "/iptv.m3u"
    tabs = ['<button class="tab on" data-g="">全部 %d</button>' % len(channels)]
    for g in groups:
        n = sum(1 for c in channels if (c.get("group") or "其他") == g)
        tabs.append('<button class="tab" data-g="%s">%s %d</button>' % (esc(g), esc(g), n))

    rows = []
    for i, ch in enumerate(channels, 1):
        g = ch.get("group") or "其他"
        rows.append(
            '<tr data-g="%s"><td class="n">%d</td><td class="nm">%s</td>'
            '<td><span class="g">%s</span></td><td class="u">%s</td></tr>'
            % (esc(g), i, esc(ch.get("name", "")), esc(g), esc(ch.get("url", "")))
        )

    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(updated)) if updated else "尚未抓取"
    head = ("%d 个频道 · %d 个分组 · 更新于 %s" % (len(channels), len(groups), when))
    if total and total != len(channels):
        head += "（已按勾选从 %d 个中筛出）" % total

    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>频道预览 · 飞牛IPTV</title><style>', PREVIEW_CSS, '</style></head><body>',
        '<header><h1>频道预览</h1><div class="meta">', esc(head), '</div></header>',
        '<div class="tip"><span>给飞牛影视导入的是这个地址：</span>'
        '<code id="m3u">', esc(m3u), '</code>'
        '<button onclick="cp()">复制</button></div>',
        '<div class="tabs">', ''.join(tabs), '</div>',
        '<table><thead><tr><th>#</th><th>频道</th><th>分组</th>'
        '<th class="u">源地址</th></tr></thead><tbody>',
        ''.join(rows) if rows else '<tr><td colspan="4" style="color:#8494ab">还没有频道，先在管理页抓取一次</td></tr>',
        '</tbody></table>',
        """<script>
document.querySelectorAll('.tab').forEach(function (t) {
  t.onclick = function () {
    document.querySelectorAll('.tab').forEach(function (x) { x.classList.remove('on'); });
    t.classList.add('on');
    var g = t.dataset.g;
    document.querySelectorAll('tbody tr').forEach(function (r) {
      r.style.display = (!g || r.dataset.g === g) ? '' : 'none';
    });
  };
});
function cp() {
  var t = document.getElementById('m3u').textContent;
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(t).then(function () { alert('已复制：' + t); },
                                          function () { alert('请长按地址手动复制'); });
  } else { alert('请长按地址手动复制：' + t); }
}
</script></body></html>""",
    ]
    return ''.join(parts)


def main():
    port = int(os.environ.get("TRIM_SERVICE_PORT") or os.environ.get("FNOSIPTV_PORT") or 18080)
    host = os.environ.get("FNOSIPTV_HOST") or "0.0.0.0"

    print("[boot] pid=%d data=%s" % (os.getpid(), DATA_DIR), flush=True)
    conf = load_config()
    if conf.get("pending_refresh") or not load_cache().get("channels"):
        threading.Thread(target=refresh, kwargs={"conf": conf}, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()

    httpd = QuietServer((host, port), Handler)
    httpd.daemon_threads = True
    print("[boot] listening on %s:%d" % (host, port), flush=True)

    def shutdown(signum, frame):  # noqa: ARG001
        print("[boot] 收到信号 %s，退出" % signum, flush=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        print("[boot] 已停止", flush=True)


if __name__ == "__main__":
    sys.exit(main())
