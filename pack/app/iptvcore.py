#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPTV 核心：抓取 / 解析 / 规范化 / 输出 M3U、TXT、EPG。

只依赖标准库，保证 platform=all 单包通吃 x86 与 ARM。
"""

import gzip
import io
import json
import os
import re
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
MAX_BYTES = 12 * 1024 * 1024

STREAM_RE = re.compile(r"^(https?|rtmp|rtsp|udp|rtp|p3p|mitv|rtp2http)://", re.I)
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
TAG_TAIL = re.compile(r"(高清|超清|标清|蓝光|4K|8K|FHD|UHD|HD|SD|HDR|杜比视界|杜比)$", re.I)
BRACKETS = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")

GROUP_ORDER = ["央视", "卫视", "港澳台", "体育", "影视", "少儿", "音乐", "纪录", "新闻",
               "财经", "科教", "生活", "综合", "地方", "国际", "其他"]

# 这些分组名太泛（或者干脆是源站自造的 "IPTV"），不能采信，
# 否则一个全场 group-title="IPTV" 的源会把所有卫视压进「地方」
GENERIC_GROUPS = ("其他", "地方", "综合", "未分组", "默认")

# 公开源里混的占位/广告条目：更新时间戳、求赞助的 mp4、测试台
JUNK_NAME = re.compile(
    r"支持作者|更新时间|更新提示|免责声明|公告|请关注|交流群|QQ群|微信群|公众号|"
    r"打赏|赞助|捐赠|广告宣传|测试", re.I)
JUNK_URL = re.compile(r"^(?:https?://)?(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?(?:/|$)", re.I)

# 各源的分组写法五花八门，先归一到统一分组名，否则同类频道会被拆成好几段
GROUP_ALIAS = {
    "央视": "央视", "央视频道": "央视", "中央电视台": "央视", "cctv": "央视", "中央": "央视",
    "卫视": "卫视", "卫视频道": "卫视", "省级卫视": "卫视", "各省卫视": "卫视",
    "港澳台": "港澳台", "港台": "港澳台", "港澳台频道": "港澳台", "台湾": "港澳台",
    "香港": "港澳台", "澳门": "港澳台",
    "体育": "体育", "体育频道": "体育", "赛事": "体育",
    "影视": "影视", "影视频道": "影视", "电影": "影视", "电影频道": "影视", "剧场": "影视",
    "少儿": "少儿", "少儿频道": "少儿", "卡通": "少儿", "动漫": "少儿",
    "音乐": "音乐", "音乐频道": "音乐",
    "纪录": "纪录", "纪录片": "纪录", "纪实": "纪录", "纪实频道": "纪录",
    "地方": "地方", "地方频道": "地方", "本地": "地方", "本地频道": "地方",
    "城市": "地方", "城市频道": "地方", "省内": "地方",
    "国际": "国际", "海外": "国际", "国际频道": "国际",
}

GROUP_SUFFIX = ("频道", "台", "栏目", "节目")

# 央视标准频道号对应的中文名。公开源里 CCTV-3 / CCTV-6 只有台标号，
# 飞牛影视里显示「CCTV-3」不如「CCTV-3 综艺」直观
CCTV_CN = {
    "1": "综合", "2": "财经", "3": "综艺", "4": "中文国际", "5": "体育",
    "5+": "体育赛事", "6": "电影", "7": "国防军事", "8": "电视剧", "9": "纪录",
    "10": "科教", "11": "戏曲", "12": "社会与法", "13": "新闻", "14": "少儿",
    "15": "音乐", "16": "奥林匹克", "17": "农业农村",
}


CGTN_CN = {"globalbiz": "CGTN 财经", "documentary": "CGTN 纪录"}


def cctv_label(name):
    m = re.match(r"^CGTN(\w+)$", name, re.I)
    if m and m.group(1).lower() in CGTN_CN:
        return CGTN_CN[m.group(1).lower()]
    m = re.match(r"^CCTV-(\d{1,2}\+?)$", name, re.I)
    if m:
        cn = CCTV_CN.get(m.group(1))
        if cn:
            return "CCTV-%s %s" % (m.group(1), cn)
    return name

CN_HINT = re.compile(r"[\u4e00-\u9fa5]|CCTV|CGTN|凤凰|翡翠|明珠|澳视|中天|东森|华娱", re.I)

# 公开源（iptv-org 等）多用英文分组，且写成 "News;Sports" 这种复合值
EN_GROUP = {
    "news": "新闻", "sports": "体育", "sport": "体育",
    "movies": "影视", "movie": "影视", "entertainment": "影视", "classic": "影视",
    "kids": "少儿", "animation": "少儿", "family": "少儿",
    "music": "音乐", "documentary": "纪录", "education": "科教", "science": "科教",
    "culture": "科教", "business": "财经", "finance": "财经",
    "general": "综合", "undefined": "其他", "lifestyle": "生活", "religious": "其他",
    "weather": "生活", "shop": "生活", "outdoor": "生活", "travel": "生活",
    "public": "综合", "auto": "生活", "cooking": "生活", "series": "影视",
    "comedy": "影视", "drama": "影视", "legislative": "综合", "relax": "生活",
}


# ---------------------------------------------------------------- 工具

def decode(raw):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def http_get(url, timeout=15, limit=MAX_BYTES):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(limit)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return resp.status, raw


def now():
    return int(time.time())


# ---------------------------------------------------------------- 解析

def parse_extinf(line):
    body = line.split(":", 1)[1] if ":" in line else ""
    attrs = dict(ATTR_RE.findall(body))
    name = body.rsplit(",", 1)[1].strip() if "," in body else ""
    name = name or attrs.get("tvg-name", "").strip() or "未命名"
    return {
        "name": name,
        "tvg_id": attrs.get("tvg-id", "").strip(),
        "tvg_name": attrs.get("tvg-name", "").strip(),
        "logo": attrs.get("tvg-logo", "").strip(),
        "group": attrs.get("group-title", "").strip(),
        "url": "",
    }


def parse_m3u(text):
    out, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXTINF"):
            cur = parse_extinf(line)
        elif line.startswith("#"):
            continue
        elif cur is not None:
            if STREAM_RE.match(line):
                cur["url"] = line
                out.append(cur)
            cur = None
    return out


def parse_txt(text):
    out, group = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        flat = line.replace(" ", "")
        if flat.endswith(",#genre#") or flat.endswith(",#GENRE#"):
            group = line.split(",", 1)[0].strip()
            continue
        parts = line.split(",", 1)
        if len(parts) != 2:
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
        name, url = parts[0].strip(), parts[1].strip()
        if name and STREAM_RE.match(url):
            out.append({"name": name, "tvg_id": "", "tvg_name": "", "logo": "",
                        "group": group, "url": url})
    return out


def parse_any(text, hint=""):
    t = text.lstrip("\ufeff \r\n\t")
    if t.upper().startswith("#EXTM3U") or "#EXTINF" in t[:4096].upper():
        return parse_m3u(t), "m3u"
    if hint.endswith(".txt") or ",#genre#" in t.replace(" ", "")[:8192]:
        return parse_txt(t), "txt"
    return parse_txt(t), "txt"


# ---------------------------------------------------------------- 规范化

# 源站爱在名字里塞画质和在线状态：SD (1080p) / FHD / 4K / [Not 24/7]
QUALITY_TAIL = re.compile(
    r"\s*[（(\[【]?\s*(?:SD|HD|FHD|UHD|4K|8K|1080[PI]?|720[PI]?|540[PI]?|360[PI]?|"
    r"not\s*24/?7|geo[- ]?blocked|backup)\s*[)）\]】]?\s*$", re.I)


def strip_quality(name):
    s = name or ""
    for _ in range(3):
        new = QUALITY_TAIL.sub("", s).strip()
        if new == s:
            break
        s = new
    return s


def norm_name(name):
    s = BRACKETS.sub("", name or "").strip()
    s = s.replace("\u3000", " ").strip()
    s = re.sub(r"\s+", "", s)
    s = TAG_TAIL.sub("", s).strip()
    m = re.match(r"^CCTV[-\s_]?(\d{1,2})(\+|plus)?(.*)$", s, re.I)
    if m:
        plus = "+" if (m.group(2) or "").lower() in ("+", "plus") else ""
        return "CCTV-%d%s" % (int(m.group(1)), plus)
    m = re.match(r"^CGTN(.*)$", s, re.I)
    if m:
        return "CGTN" + m.group(1)
    return s


def norm_group(g):
    """把「央视频道 / 中央电视台 / CCTV」这类写法归一到同一分组。"""
    s = (g or "").strip()
    if not s:
        return ""
    # "News;Sports" / "Public;Sports" 这类复合值取第一个
    if ";" in s:
        s = s.split(";")[0].strip()
    low = s.lower()
    if low in EN_GROUP:
        return EN_GROUP[low]
    if low in GROUP_ALIAS:
        return GROUP_ALIAS[low]
    for suf in GROUP_SUFFIX:
        if s.endswith(suf) and len(s) > len(suf):
            base = s[: -len(suf)].strip().lower()
            if base in GROUP_ALIAS:
                return GROUP_ALIAS[base]
            s = s[: -len(suf)].strip()
            break
    return GROUP_ALIAS.get(s.lower(), s)


def _guess_by_name(name):
    """只看频道名推断分组，判断不出返回空串。"""
    s = name or ""
    if re.match(r"^CCTV-?\d", s, re.I) or re.match(r"^CGTN", s, re.I):
        return "央视"
    if "卫视" in s:
        return "卫视"
    if any(k in s for k in ("香港", "澳门", "台湾", "翡翠", "明珠", "凤凰", "澳视", "中天", "东森")):
        return "港澳台"
    if any(k in s for k in ("体育", "足球", "篮球", "NBA", "英超", "西甲", "赛事")):
        return "体育"
    if any(k in s for k in ("电影", "影视", "剧场", "影院", "电视剧")):
        return "影视"
    if any(k in s for k in ("少儿", "卡通", "动漫", "动画")):
        return "少儿"
    if any(k in s for k in ("音乐", "MTV", "演唱会")):
        return "音乐"
    if any(k in s for k in ("纪录", "纪实", "探索", "地理")):
        return "纪录"
    return ""


def guess_group(name, group):
    # CCTV-x / CGTN 无论源里标成什么分类，都归到「央视」，否则央视会被拆到体育、影视组里。
    # 但 CCTV-<英文> 是境外冒用台号的频道（CCTV-CultureOfQuality），不能算央视
    if re.match(r"^CCTV-?\d", name, re.I) or re.match(r"^CGTN(\d|$)", name, re.I):
        return "央视"
    g = norm_group(group)
    # 分组名足够具体（「卫视频道」「纪录频道」）才直接采信；
    # 「IPTV」「地方频道」「其他」这类泛称交给名字判断，否则卫视会被压进地方
    if g and g in GROUP_ORDER and g not in GENERIC_GROUPS:
        return g
    by_name = _guess_by_name(name)
    if by_name:
        return by_name
    return g if (g and g in GROUP_ORDER) else "地方"


def group_rank(g):
    return GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER)


def natural_key(s):
    """让 CCTV-2 排在 CCTV-13 前面。"""
    out = []
    for part in re.split(r"(\d+)", s or ""):
        if not part:
            continue
        out.append((0, int(part)) if part.isdigit() else (1, part.lower()))
    return out


# ---------------------------------------------------------------- 源抓取

def load_source(src, timeout=15):
    """返回 (channels, kind, error)"""
    kind = src.get("kind", "url")
    val = src.get("value", "")
    hint = ""
    try:
        if kind == "url":
            hint = val.split("?")[0]
            _, raw = http_get(val, timeout=timeout)
            text = decode(raw)
        elif kind == "file":
            path = val
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
            with open(path, "rb") as fh:
                text = decode(fh.read(MAX_BYTES))
            hint = path
        else:
            text = val
        chans, fmt = parse_any(text, hint)
        return chans, fmt, ""
    except urllib.error.HTTPError as exc:
        return [], "", "HTTP %s" % exc.code
    except urllib.error.URLError as exc:
        return [], "", "网络不可达：%s" % getattr(exc, "reason", exc)
    except Exception as exc:  # noqa: BLE001
        return [], "", "%s: %s" % (type(exc).__name__, exc)


def probe(url, timeout=6):
    """轻量可用性探测。

    只发 HEAD 不够：不少 IPTV 源队 HEAD 返回 200 但实际是错误页，
    还有 CDN 直接 403。所以先 HEAD，可疑时再用 GET 读前 2KB 看内容。
    证书过期的源（自建反代很常见）播放器其实照播，所以校验失败时降级重探一次。
    """
    ok, info = _probe_once(url, timeout, verify=True)
    if not ok and ("CERTIFICATE" in info.upper() or "SSL" in info.upper()):
        ok, info = _probe_once(url, timeout, verify=False)
        if ok:
            info += " 证书未校验"
    return ok, info


def _probe_once(url, timeout, verify=True):
    start = time.time()
    ctx = None if verify else ssl._create_unverified_context()  # noqa: SLF001
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = resp.status
            ctype = (resp.headers.get("Content-Type") or "").lower()
            # HEAD 返回 2xx/3xx 且不是明显错误类型，基本可信
            if 200 <= code < 400 and "text/html" not in ctype:
                return True, "%d %.2fs" % (code, time.time() - start)
    except Exception:  # noqa: BLE001
        pass

    # 退化：GET 读头部字节，m3u8 应含 #EXTM3U，否则看是否 HTML
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Range": "bytes=0-2048"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = resp.status
            if not (200 <= code < 400):
                return False, "HTTP %d" % code
            head = resp.read(2048)
            low = head.lower()
            if b"#extm3u" in low or b"#extinf" in low or b"#ext-x-" in low:
                return True, "%d %.2fs" % (code, time.time() - start)
            if b"<html" in low or b"<!doctype" in low:
                return False, "返回网页非流"
            if head:
                return True, "%d %.2fs" % (code, time.time() - start)
            return False, "空响应"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:60]


# ---------------------------------------------------------------- 台标

def logo_slug(name):
    """频道名 → URL 安全的 slug（hex）。避免中文与空格在播放器里被截断。"""
    return (name or "x").encode("utf-8").hex()


def slug_name(slug):
    try:
        return bytes.fromhex(slug).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


# 台标库里频道的写法：CCTV-1 综合 -> CCTV1，CCTV-5+ 体育赛事 -> CCTV5+
def logo_key(name):
    s = (name or "").strip()
    s = re.sub(r"\s*(综合|财经|综艺|中文国际|体育赛事|体育|电影|国防军事|电视剧|纪录|科教|戏曲|社会与法|新闻|少儿|音乐|奥林匹克|农业农村)$", "", s)
    s = s.replace(" ", "").replace("-", "").replace("－", "")
    return s


# 实测：live.fanmingming.com 在部分网络下直接超时，jsDelivr 稳定几百毫秒，
# 所以只保留 CDN 通道，避免每个台标白等 5 秒才降级
LOGO_MIRRORS = (
    "https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/%s.png",
    "https://fastly.jsdelivr.net/gh/fanmingming/live@main/tv/%s.png",
)

# 这些图床在国内基本连不上，挨个等超时会让 300 个台标预热卡十几分钟
SKIP_LOGO_HOSTS = ("imgur.com", "i.imgur", "ibb.co", "postimg", "imgbb",
                   "giphy", "t.me", "telegram", "discord", "googleusercontent")


def fetch_logo(ch, timeout=3):
    """按优先级取台标二进制：原图 → 台标库→ 返回 (bytes, ext) 或 (None, "")。"""
    # 台标库放前面：国内 CDN 几百毫秒就返回，imgur 这类源站经常十几秒才超时
    cands = []
    key = logo_key(ch.get("name", ""))
    if key:
        cands.extend(m % urllib.parse.quote(key) for m in LOGO_MIRRORS)
    raw = ch.get("raw_name") or ""
    if raw and raw != ch.get("name"):
        if logo_key(raw) != key:
            cands.extend(m % urllib.parse.quote(logo_key(raw)) for m in LOGO_MIRRORS)
    logo = ch.get("logo") or ""
    if logo and not any(h in logo.lower() for h in SKIP_LOGO_HOSTS):
        cands.append(logo)

    for url in cands:
        if not url.lower().startswith(("http://", "https://")):
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ""})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if not (200 <= resp.status < 300):
                    continue
                ctype = (resp.headers.get("Content-Type") or "").lower()
                data = resp.read(200 * 1024)
                if len(data) < 200:
                    continue
                if "svg" in ctype:
                    ext = ".svg"
                elif "png" in ctype or data[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                elif "gif" in ctype or data[:3] == b"GIF":
                    ext = ".gif"
                elif "webp" in ctype or data[:4] == b"RIFF":
                    ext = ".webp"
                else:
                    ext = ".jpg"
                return data, ext
        except Exception:  # noqa: BLE001
            continue
    return None, ""


# ---------------------------------------------------------------- 源校正

# 同一个台往往有七八个地址，质量差别极大：有的来自运营商正版 IPTV 网关，
# 有的是「一台海外小机器挂全套央视」的转播站——后者常常拿广告片冒充频道。
# 这里给地址打信任分，合并时优先选真直播。
IPTV_MARKS = (
    ("tsfile/live", 45), ("key=txiptv", 45), ("udp/", 45), ("rtp/", 45),
    ("/pltv/", 40), ("channels/lantian", 40),
    ("playlive=", 35), ("authid=", 35), ("/iptv/live", 35),
    ("newlive/live", 25),
)
RESTREAM_MARKS = ("memfs", "restream", "_output_")
UUID_PATH = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
IPV4_ONLY = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# 转播站爱把地址写成 /live/cctv2hd.m3u8 这种「台名+hd」，正规网关不会这样
RELAY_NAME = re.compile(r"/(?:live|hls|tv)/[a-z0-9_-]*?(?:cctv|tv|hd)[a-z0-9_-]*\.m3u8", re.I)


def url_score(url, peers=0):
    """播放地址信任分。peers = 同一域名在本轮抓取里出现的频道数。

    分数只用于排序和提示，不直接删台：判断错的代价比漏判高。
    """
    low = (url or "").lower()
    try:
        u = urllib.parse.urlparse(low)
        host, path = (u.hostname or ""), (u.path or "")
        port = u.port
    except ValueError:
        return 0, []
    s, why = 0, []

    hits = [v for k, v in IPTV_MARKS if k in low]
    if hits:
        s += max(hits)
        why.append("运营商IPTV地址")
    if any(m in low for m in RESTREAM_MARKS):
        s -= 60
        why.append("视频文件转播")
    if UUID_PATH.search(path):
        s -= 45
        why.append("随机文件名")

    if IPV4_ONLY.match(host):
        if port and port not in (80, 443, 8080):
            s += 15
            why.append("直连端口")
        if RELAY_NAME.search(path):
            s -= 35
            why.append("单机转播")

    # 运营商 IPTV 网关本来就是一个 IP 挂几十个台，已识别为网关的不罚；
    # 没有鉴权特征却挂几十个台的，才是转播站
    if peers > 30 and not hits:
        s -= 30
        why.append("单域名挂%d个台" % peers)
    return s, why


def inspect(url, timeout=8):
    """拉取 m3u8 判断这是直播、点播还是循环短片。

    返回 kind: live / vod（带 ENDLIST 的录播）/ loop（极短循环）/ notm3u8 / error
    fp 是片段命名指纹，用来发现「好几个台其实是同一条流」。
    """
    try:
        _, raw = http_get(url, timeout=timeout, limit=96 * 1024)
        text = decode(raw)
        if "#EXTM3U" not in text[:400].upper():
            return {"kind": "notm3u8", "segs": 0, "target": 0, "fp": "", "error": ""}
        # 主列表：跟第一个码率变体
        m = re.search(r"^#EXT-X-STREAM-INF[^\n]*\n\s*([^#\s][^\n]*)$", text, re.M)
        if m:
            vurl = urllib.parse.urljoin(url, m.group(1).strip())
            _, raw = http_get(vurl, timeout=timeout, limit=96 * 1024)
            text = decode(raw)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "error", "segs": 0, "target": 0, "fp": "", "error": str(exc)[:60]}

    up = text.upper()
    segs = up.count("#EXTINF")
    td = 0
    m = re.search(r"#EXT-X-TARGETDURATION:\s*(\d+)", up)
    if m:
        td = int(m.group(1))
    fp = ""
    for line in text.splitlines():
        if line and not line.startswith("#"):
            base = line.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            # 只抛掉时间戳/长序号（>=6 位数字），短数字要留着：
            # 运营商网关的切片叫 live_2_1_2540725.ts，那个 2 就是频道号，去掉就全撞一起了
            cand = re.sub(r"\d{6,}", "", stem)
            fp = cand if len(cand) >= 8 else ""
            break
    if "#EXT-X-ENDLIST" in up or "PLAYLIST-TYPE:VOD" in up:
        kind = "vod"
    elif 0 < segs <= 2 and td >= 9:
        kind = "loop"
    else:
        kind = "live"
    return {"kind": kind, "segs": segs, "target": td, "fp": fp, "error": ""}


# ---------------------------------------------------------------- 聚合

def build_channels(entries, settings):
    """entries: [(source_id, source_name, priority, channel_dict)]"""
    unify = settings.get("unify_name", True)
    dedupe = settings.get("dedupe", True)
    buckets, order = {}, []

    # 先数一遍每个域名挂了多少个台：一个域名下几十个不同频道基本是转播站
    peers = Counter()
    for _, _, _, c in entries:
        try:
            peers[urllib.parse.urlparse((c.get("url") or "").strip()).hostname or ""] += 1
        except ValueError:
            pass

    for sid, sname, prio, ch in entries:
        url = (ch.get("url") or "").strip()
        if not url or JUNK_URL.match(url):
            continue
        name = (ch.get("name") or "").strip() or "未命名"
        # 占位与广告条目：源站的「更新时间 2026-11-20」、求捐赠的 mp4 宣传片、测试台
        if JUNK_NAME.search(name):
            continue
        # mp4/flv 点播文件不是直播流：拉到飞牛影视里既没节目单也拖不动进度条
        if url.split("?")[0].lower().endswith((".mp4", ".flv")):
            continue
        # 公开源里混着大量外语台，中文用户通常只想留中文频道。
        # 但 iptv-org 的中国/港澳台源里，地方台名常写成英文（Anhui TV、CETV-1），
        # 按名字过滤会把它们全砍掉，所以中文源整体豁免；
        # 注意 CCTV-1 / CGTN 这类名字本身没有汉字，也要一并算作中文频道
        if (settings.get("cn_only") and not ch.get("_cn")
                and not CN_HINT.search(name)):
            continue
        shown = cctv_label(norm_name(strip_quality(name))) if unify else strip_quality(name)
        # 清洗后如果是空串（原文只有画质标注），退回原名
        shown = shown or name
        key = shown if dedupe else "%s|%s" % (shown, url)
        host = ""
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except ValueError:
            pass
        score, why = url_score(url, peers.get(host, 0))
        cand = {"url": url, "source": sname, "prio": prio, "score": score, "why": why}

        if key in buckets:
            b = buckets[key]
            if url not in [c["url"] for c in b["cands"]]:
                b["cands"].append(cand)
            continue
        group = guess_group(shown, ch.get("group", ""))
        buckets[key] = {
            "name": shown,
            "raw_name": name,
            "group": group,
            "tvg_id": ch.get("tvg_id", ""),
            "logo": ch.get("logo", ""),
            "cands": [cand],
        }
        # 先按分组聚拢，再按源优先级、名称排序：保证 TXT 里同组连续，M3U 也按组聚集
        order.append((group_rank(group), natural_key(shown), prio, key))

    order.sort(key=lambda x: (x[0], x[1], x[2]))
    result = []
    for _, _, _, key in order:
        it = buckets[key]
        cands = sorted(it.pop("cands"), key=lambda c: (-c["score"], c["prio"]))
        best = cands[0]
        it["url"] = best["url"]
        it["source"] = best["source"]
        it["score"] = best["score"]
        it["score_why"] = best["why"]
        it["alts"] = [c["url"] for c in cands[1:]]
        it["alt_sources"] = [c["source"] for c in cands[1:]]
        # 静态只看「几乎肯定是广告」的：一个台只有视频文件转播地址、没有备选。
        # 分数一般偏低（-30/-35）的不标，避免把县域小台的转播站全刷成可疑；
        # 「可疑」留给深度校正按共用流抓出来的。
        it["verdict"] = "ad" if best["score"] <= -50 else "ok"
        if not it["tvg_id"]:
            # 国内 EPG（如 51zmt）的 channel id 习惯写成 CCTV1 而不是 CCTV-1
            it["tvg_id"] = it["name"].replace("-", "").replace(" ", "")
        result.append(it)
    return result


def apply_keep(channels, keep):
    """按用户勾选过滤。keep 为空表示不筛选（全量输出）。"""
    if not keep:
        return channels
    wanted = set(keep)
    return [c for c in channels if c.get("name") in wanted]


def pick_logo_url(ch, public_base="", logos=True, mode="local"):
    """台标地址。

    飞牛影视只认**外网** tvg-logo，内网地址（http://192.168.x.x/...）一律不显示
    （官方论坛已确认）。所以 mode=cdn 时优先给公网 HTTPS 图床；网页端仍可用本机代理。
    """
    raw = (ch.get("logo") or "").strip()
    if mode == "cdn":
        if raw.lower().startswith("https://") and not any(h in raw.lower() for h in SKIP_LOGO_HOSTS):
            return raw
        key = logo_key(ch.get("name", ""))
        if key:
            return LOGO_MIRRORS[0] % urllib.parse.quote(key)
        return ""
    if logos and public_base:
        return "%s/logo/%s.png" % (public_base, logo_slug(ch.get("name", "")))
    if raw and public_base and raw.startswith("/"):
        return public_base + raw
    return raw


def to_m3u(channels, epg_url="", public_base="", logos=True, logo_mode="local"):
    """logos=True 时台标走本机 /logo 代理；logo_mode="cdn" 时改用公网台标库（飞牛影视需要）。"""
    lines = ['#EXTM3U x-tvg-url="%s"' % epg_url if epg_url else "#EXTM3U"]
    for ch in channels:
        logo = pick_logo_url(ch, public_base, logos, logo_mode)
        lines.append(
            '#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-logo="%s" group-title="%s",%s'
            % (esc(ch.get("tvg_id", "")), esc(ch.get("name", "")), esc(logo),
               esc(ch.get("group", "")), ch.get("name", ""))
        )
        lines.append(ch["url"])
    return "\n".join(lines) + "\n"


def to_txt(channels):
    out, cur = [], None
    for ch in channels:
        g = ch.get("group", "其他")
        if g != cur:
            out.append("%s,#genre#" % g)
            cur = g
        out.append("%s,%s" % (ch.get("name", ""), ch["url"]))
    return "\n".join(out) + "\n"


def esc(s):
    return (s or "").replace('"', "'").replace("\n", " ").replace("\r", " ")


# ---------------------------------------------------------------- EPG

def merge_epg(xml_texts, epg_urls):
    """把多份 XMLTV 合并成一份；解析失败的源跳过并记录。"""
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<tv generator-info-name="fnosiptv">\n')
    chans, progs, seen_c, seen_p, used = [], [], set(), set(), []
    for url, text in zip(epg_urls, xml_texts):
        if not text:
            continue
        try:
            root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
        except ET.ParseError:
            continue
        used.append(url)
        for ch in root.findall("channel"):
            cid = ch.get("id", "")
            if cid and cid in seen_c:
                continue
            seen_c.add(cid)
            chans.append(ET.tostring(ch, encoding="unicode").strip())
        for pg in root.findall("programme"):
            key = (pg.get("channel", ""), pg.get("start", ""), pg.get("stop", ""))
            if key in seen_p:
                continue
            seen_p.add(key)
            progs.append(ET.tostring(pg, encoding="unicode").strip())
    body = "\n".join(chans + progs)
    return head + body + "\n</tv>\n", used
