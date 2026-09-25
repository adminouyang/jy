#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV频道源测速工具 v3.4
改动(vs v3.3)：
- global声明移至main函数首行
- 直链/分片测速：同源Referer、Range失败自动降级、连接中断保留已下载数据
- 接口型m3u8容错：Content-Type为html/json时，若body含#EXTM3U仍当HLS处理
- HLS递归：返回(media_url, text)列表，逐个media尝试分片
- 有效字节下限降至1024，低速/失败区分更清晰
"""

import asyncio
import aiohttp
import ssl
import statistics
import os
import re
import json
import time
import argparse
from urllib.parse import urlparse, urljoin, urldefrag
from datetime import datetime, timedelta, timezone

# ====================== 全局配置 ======================
SPEED_THRESHOLD = 600          # KB/s
CHECK_TIMEOUT = 5              # 秒
MAX_CONCURRENT = 50            # 最大并发数
DEEP_TEST_SIZE = 786432        # 字节 (~768KB)
STEADY_BYTES = 262144          # 排除前256KB爆发期
MIN_TEST_TIME = 2.5            # 秒

HLS_MAX_DEPTH = 3              # master/media 递归深度
HLS_SEGMENT_COUNT = 4          # 每个 media playlist 取最近几个 ts
RANGE_PROBE_BYTES = 524288     # 直链/分片用 Range 拉 512KB
MIN_MEASURE_BYTES = 1024       # 有效分片最低字节数（放宽）
RETRY_ONCE = True              # 瞬态错误重试1次
INSECURE_SSL = True            # 关闭证书校验（IPTV常用，生产可改False）

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Connection': 'keep-alive',
}


# ====================== 工具函数 ======================
def build_ssl_context():
    ctx = ssl.create_default_context()
    if INSECURE_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def parse_m3u8_text(text):
    """解析 m3u8 文本，返回 is_master / streams / segments"""
    streams = []
    segments = []
    cur_dur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXT-X-STREAM-INF'):
            cur_dur = None
            continue
        if line.startswith('#EXTINF'):
            try:
                dur = float(line.split(':', 1)[1].split(',', 1)[0].strip())
            except Exception:
                dur = None
            cur_dur = dur
            continue
        if line.startswith('#'):
            continue
        if line.lower().split('?')[0].endswith('.m3u8') or 'm3u8' in line.lower():
            streams.append(line)
        else:
            segments.append((cur_dur, line))
            cur_dur = None
    return {'is_master': bool(streams), 'streams': streams, 'segments': segments}


def try_extract_stream_url(body, content_type=''):
    """从 JSON / 纯文本中提取可能的流地址"""
    candidates = []
    if content_type.startswith('application/json') or body.lstrip().startswith('{'):
        try:
            data = json.loads(body.strip())
            if isinstance(data, dict):
                for k in ('url', 'stream', 'source', 'playurl', 'play_url', 'hls', 'm3u8'):
                    v = data.get(k)
                    if v:
                        candidates.append(str(v))
                # 递归找含 m3u8 的字段
                def walk(o):
                    if isinstance(o, dict):
                        for v in o.values():
                            walk(v)
                    elif isinstance(o, list):
                        for v in o:
                            walk(v)
                    elif isinstance(o, str) and 'm3u8' in o:
                        candidates.append(o)
                walk(data)
        except Exception:
            pass
    # 纯文本里挑 m3u8 链接
    for m in re.findall(r'https?://[^\s\'"]+\.m3u8[^\s\'"]*', body):
        candidates.append(m)
    return candidates


# ====================== 黑名单管理 ======================
class Blacklist:
    def __init__(self, path='freetv/blacklist.txt'):
        self.path = path
        self.domains = set()
        self.load()

    def load(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.path):
            with open(self.path, 'w', encoding='utf-8') as f:
                f.write("# IPTV黑名单域名列表\n# 每行一个域名，以#开头的行视为注释\n")
            print(f"已创建黑名单文件: {self.path}")
            return
        with open(self.path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    self.domains.add(line)
        print(f"黑名单加载完毕: {len(self.domains)} 个域名")

    def contains(self, url):
        try:
            domain = urlparse(url).netloc
            if ':' in domain:
                domain = domain.split(':')[0]
            return domain in self.domains
        except Exception:
            return False


# ====================== 频道模板处理 ======================
class ChannelTemplate:
    def __init__(self, template_path):
        self.path = template_path
        self.categories = []
        self.channel_map = {}
        self.main_channels = {}
        self.category_channels = {}

    def load(self):
        if not os.path.exists(self.path):
            print(f"[错误] 模板文件 {self.path} 不存在")
            return False

        current_cat = None
        with open(self.path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if '📡' in line and '#genre#' in line:
                    parts = line.split('#genre#')
                    cat = parts[0].replace('📡', '').strip()
                    if cat and cat not in self.categories:
                        self.categories.append(cat)
                        self.category_channels[cat] = []
                    current_cat = cat
                elif current_cat and ',' in line:
                    items = [x.strip() for x in line.split(',') if x.strip()]
                    if items:
                        main = items[0]
                        self.main_channels[main] = current_cat
                        if main not in self.category_channels[current_cat]:
                            self.category_channels[current_cat].append(main)
                        for alias in items:
                            if alias not in self.channel_map:
                                self.channel_map[alias] = main

        other_cat = '其它频道'
        if other_cat in self.categories:
            self.categories.remove(other_cat)
        self.categories.append(other_cat)
        if other_cat not in self.category_channels:
            self.category_channels[other_cat] = []

        print(f"模板加载完成：{len(self.categories)} 个分类，{len(self.channel_map)} 个别名")
        return True

    def add_to_other(self, name):
        other_cat = '其它频道'
        self.main_channels[name] = other_cat
        if name not in self.category_channels[other_cat]:
            self.category_channels[other_cat].append(name)
        if name not in self.channel_map:
            self.channel_map[name] = name

    def get_main(self, name):
        return self.channel_map.get(name, name)

    def get_category(self, name):
        main = self.get_main(name)
        return self.main_channels.get(main, '其它频道')

    def get_logo_url(self, name):
        main = self.get_main(name)
        safe = main.replace('/', '').replace('\\', '').replace(':', '')
        return f"https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/{safe}.png"

    def get_template_names(self):
        return set(self.channel_map.keys())


# ====================== 频道列表获取 ======================
async def fetch_text(session, url):
    headers = HEADERS.copy()
    parsed = urlparse(url)
    headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}"
    try:
        async with session.get(url, timeout=10, headers=headers) as resp:
            if resp.status >= 400:
                print(f"源获取失败 {url}: HTTP {resp.status}")
                return ''
            return await resp.text()
    except Exception as e:
        print(f"获取失败 {url}: {e}")
        return ''


def clean_m3u_name(raw):
    name = re.sub(r'\([^)]*\)', '', raw)
    name = re.sub(r'\[[^\]]*\]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def sanitize_url(raw_url):
    url = raw_url.strip()
    url, _ = urldefrag(url)
    if not url or not url.startswith(('http://', 'https://')):
        return None
    return url


def parse_m3u(text):
    channels = []
    lines = text.strip().split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            parts = line.split(',')
            if len(parts) >= 2:
                name = clean_m3u_name(parts[-1].strip())
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].startswith('#')):
                    j += 1
                if j < len(lines):
                    url = sanitize_url(lines[j].strip())
                    if url:
                        channels.append((name, url))
                        i = j
        i += 1
    return channels


def parse_txt(text):
    channels = []
    for line in text.split('\n'):
        line = line.strip()
        if '#genre#' in line or not line:
            continue
        if ',' in line and '://' in line:
            try:
                name, raw_url = line.split(',', 1)
                url = sanitize_url(raw_url)
                if url:
                    name = re.sub(r'^\[[A-Z0-9]+\]\s*', '', name).strip()
                    channels.append((name, url))
            except Exception:
                pass
    return channels


async def fetch_channels_from_urls(urls):
    all_channels = []
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        results = await asyncio.gather(
            *(fetch_text(session, u) for u in urls), return_exceptions=True
        )
    for u, t in zip(urls, results):
        if isinstance(t, Exception) or not t:
            if t and not isinstance(t, Exception):
                print(f"源失败 {u}")
            continue
        if t.strip().startswith('#EXTM3U'):
            chs = parse_m3u(t)
            print(f"  M3U源 {u}: {len(chs)} 个频道")
        else:
            chs = parse_txt(t)
            print(f"  TXT源 {u}: {len(chs)} 个频道")
        all_channels.extend(chs)
    return all_channels


# ====================== 异步测速引擎 ======================
class AsyncSpeedTester:
    def __init__(self, blacklist):
        self.blacklist = blacklist
        self.stats = {
            'total': 0, 'passed': 0, 'low': 0, 'fail': 0, 'skip': 0,
            'speeds': [], 'max': 0.0, 'min': float('inf')
        }
        self.session = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def __aenter__(self):
        conn = aiohttp.TCPConnector(
            limit=MAX_CONCURRENT * 2,
            ttl_dns_cache=600,
            ssl=build_ssl_context(),
            force_close=False
        )
        self.session = aiohttp.ClientSession(
            connector=conn,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT + 2)
        )
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    # ---------- 测量 ----------
    async def _measure_url_fixed(self, url, channel_name, use_range=True,
                                 max_bytes=RANGE_PROBE_BYTES, min_time=MIN_TEST_TIME):
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = HEADERS.copy()
        headers['Referer'] = origin
        if use_range:
            headers['Range'] = f'bytes=0-{max_bytes - 1}'

        start = time.time()
        try:
            async with self.session.get(url, timeout=CHECK_TIMEOUT, headers=headers) as resp:
                if resp.status in (403, 416) and use_range:
                    return await self._measure_url_fixed(
                        url, channel_name, use_range=False,
                        max_bytes=max_bytes, min_time=min_time
                    )
                if resp.status not in (200, 206):
                    return 0.0, 0, f'HTTP {resp.status}'

                ct = resp.headers.get('Content-Type', '').lower()
                if any(x in ct for x in ['text/html', 'application/json', 'text/plain']):
                    snippet = await resp.content.read(1024)
                    if b'#EXTM3U' not in snippet:
                        return 0.0, len(snippet), f'ct={ct}'
                    return 0.0, len(snippet), 'content_type_mismatch'

                downloaded = 0
                chunk_speeds = []
                steady = 0
                steady_t0 = None
                chunk_start = time.time()
                try:
                    async for chunk in resp.content.iter_chunked(32768):
                        if not chunk:
                            break
                        now = time.time()
                        l = len(chunk)
                        downloaded += l
                        if downloaded > STEADY_BYTES:
                            if steady_t0 is None:
                                steady_t0 = now
                            steady += l
                        el = now - chunk_start
                        if el > 0.001:
                            chunk_speeds.append(l / el / 1024)
                        chunk_start = now
                        if downloaded >= max_bytes:
                            break
                        if (now - start) >= min_time and downloaded >= 131072:
                            break
                except (aiohttp.ServerDisconnectedError,
                        aiohttp.ClientConnectionError,
                        asyncio.TimeoutError):
                    pass  # 中断但保留已下载数据

                total = time.time() - start
                if total <= 0 or downloaded < MIN_MEASURE_BYTES:
                    return 0.0, downloaded, f'small_{downloaded}B'

                overall = downloaded / total / 1024
                steady_speed = 0.0
                if steady_t0 and steady > 0:
                    se = time.time() - steady_t0
                    if se > 0:
                        steady_speed = steady / se / 1024
                median_s = statistics.median(chunk_speeds) if len(chunk_speeds) >= 3 else overall
                final = 0.5 * steady_speed + 0.3 * overall + 0.2 * median_s
                return final, downloaded, 'ok'

        except asyncio.TimeoutError:
            return 0.0, 0, 'timeout'
        except aiohttp.ServerDisconnectedError:
            return 0.0, 0, 'disconnected'
        except Exception as e:
            return 0.0, 0, f'{type(e).__name__}:{str(e)[:40]}'

    # ---------- HLS 递归 ----------
    async def resolve_media_playlist(self, url, depth=0):
        """返回 [(media_url, text), ...]；非hls返回空列表"""
        if depth > HLS_MAX_DEPTH:
            return []
        try:
            parsed = urlparse(url)
            headers = HEADERS.copy()
            headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}"
            async with self.session.get(url, timeout=CHECK_TIMEOUT, headers=headers) as resp:
                if resp.status not in (200, 206):
                    return []
                text = await resp.text()
        except Exception:
            return []
        if not text.lstrip().lower().startswith('#extm3u'):
            return []
        info = parse_m3u8_text(text)
        if info['is_master']:
            result = []
            for s in info['streams']:
                sub = urljoin(url, s)
                sub, _ = urldefrag(sub)
                result.extend(await self.resolve_media_playlist(sub, depth + 1))
            return result
        return [(url, text)]

    async def measure_hls(self, playlist_url, channel_name):
        medias = await self.resolve_media_playlist(playlist_url)
        if not medias:
            return 0.0, 'no_media_playlist'
        for media_url, text in medias:
            info = parse_m3u8_text(text)
            segs = [s for _, s in info['segments']]
            if not segs:
                continue
            recent = []
            for s in segs[-HLS_SEGMENT_COUNT:]:
                u = urljoin(media_url, s)
                u, _ = urldefrag(u)
                if u not in recent:
                    recent.append(u)
            total_bytes = 0
            total_t0 = time.time()
            valid = 0
            last_err = 'ok'
            for seg_url in recent:
                speed, downloaded, reason = await self._measure_url_fixed(
                    seg_url, channel_name, use_range=True
                )
                if speed > 0 and downloaded >= MIN_MEASURE_BYTES:
                    total_bytes += downloaded
                    valid += 1
                else:
                    last_err = f'seg_{reason}'
            elapsed = time.time() - total_t0
            if valid == 0:
                continue
            if elapsed <= 0:
                continue
            avg = total_bytes / elapsed / 1024
            return avg, f'segs={valid}/{len(recent)}'
        return 0.0, 'all_media_failed'

    # ---------- 记录 ----------
    def _record(self, url, name, speed, ttfb):
        st = self.stats
        st['speeds'].append(speed)
        st['max'] = max(st['max'], speed)
        st['min'] = min(st['min'], speed)
        if speed >= SPEED_THRESHOLD:
            st['passed'] += 1
            tag = '✅'
        else:
            st['low'] += 1
            tag = '⚠️'
        print(f"{tag} {name:<10}|{url[:85]:<85}|速度:{speed:>7.1f}KB/s|ttfb:{ttfb * 1000:>5.0f}ms")

    # ---------- 单源测试 ----------
    async def test_one(self, url, channel_name):
        if self.blacklist.contains(url):
            self.stats['skip'] += 1
            print(f"⏭️  黑名单跳过: {channel_name:<10}| {urlparse(url).netloc}")
            return 0.0

        clean, _ = urldefrag(url)
        if clean != url:
            url = clean

        async with self.semaphore:
            self.stats['total'] += 1
            attempts = 2 if RETRY_ONCE else 1
            for attempt in range(attempts):
                try:
                    start = time.time()
                    parsed = urlparse(url)
                    headers = HEADERS.copy()
                    headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}"
                    is_hls = url.lower().split('?')[0].endswith('.m3u8')

                    if not is_hls:
                        async with self.session.get(url, timeout=CHECK_TIMEOUT, headers=headers) as probe:
                            ttfb = time.time() - start
                            if probe.status in (401, 403, 429):
                                self.stats['fail'] += 1
                                print(f"❌ {channel_name:<10}|{url[:85]:<85}|HTTP {probe.status}")
                                return 0.0
                            if probe.status >= 400:
                                self.stats['fail'] += 1
                                print(f"❌ {channel_name:<10}|{url[:85]:<85}|HTTP {probe.status}")
                                return 0.0
                            ct = probe.headers.get('Content-Type', '').lower()
                            if 'mpegurl' in ct:
                                is_hls = True
                            # html/json 里可能藏了 m3u8
                            if any(x in ct for x in ['text/html', 'application/json', 'text/plain']):
                                snippet = await probe.content.read(1024)
                                if b'#EXTM3U' in snippet:
                                    is_hls = True
                                else:
                                    extracted = try_extract_stream_url(snippet.decode('utf-8', 'ignore'), ct)
                                    if extracted:
                                        is_hls = True
                                        url = extracted[0]  # 递归到真实流
                    else:
                        ttfb = 0.0

                    if is_hls:
                        speed, reason = await self.measure_hls(url, channel_name)
                    else:
                        speed, downloaded, reason = await self._measure_url_fixed(
                            url, channel_name, use_range=True
                        )

                    if speed > 0:
                        self._record(url, channel_name, speed, ttfb)
                        return speed

                    if is_hls:
                        continue  # 重新解析 playlist 重试
                    self.stats['low'] += 1
                    print(f"❌ {channel_name:<10}|{url[:85]:<85}|直链速度0/过小 reason={reason}")
                    return 0.0

                except asyncio.TimeoutError:
                    if attempt < attempts - 1:
                        continue
                    self.stats['fail'] += 1
                    print(f"❌ {channel_name:<10}|{url[:85]:<85}|超时")
                    return 0.0
                except aiohttp.ServerDisconnectedError:
                    if attempt < attempts - 1:
                        continue
                    self.stats['fail'] += 1
                    print(f"❌ {channel_name:<10}|{url[:85]:<85}|Server disconnected")
                    return 0.0
                except Exception as e:
                    if attempt < attempts - 1:
                        continue
                    self.stats['fail'] += 1
                    print(f"❌ {channel_name:<10}|{url[:85]:<85}|异常:{str(e)[:30]}")
                    return 0.0

            self.stats['fail'] += 1
            print(f"❌ {channel_name:<10}|{url[:85]:<85}|所有分片测速失败")
            return 0.0

    # ---------- 批量测试 ----------
    async def batch_test(self, channel_list, template):
        groups = {}
        for main, url in channel_list:
            groups.setdefault(main, []).append(url)

        results = {}
        total_sources = len(channel_list)
        tested = 0

        print(f"\n开始并发测速，最大并发 {MAX_CONCURRENT}，共 {total_sources} 个源")
        print("=" * 145)

        for cat in template.categories:
            for main in template.category_channels.get(cat, []):
                if main not in groups:
                    continue
                urls = groups[main]
                tasks = [self.test_one(url, main) for url in urls]
                speeds = await asyncio.gather(*tasks)

                passed = [(url, sp) for url, sp in zip(urls, speeds) if sp >= SPEED_THRESHOLD]
                passed.sort(key=lambda x: x[1], reverse=True)
                if passed:
                    results[main] = passed

                tested += len(urls)
                passed_now = sum(1 for sp in speeds if sp >= SPEED_THRESHOLD)
                print(f"  {main:<20} 通过 {passed_now}/{len(urls)}  进度 {tested}/{total_sources}")
                print("-" * 145)

        return results, self.stats


# ====================== 文件输出 ======================
def save_output(all_channels, template, output_dir='freetv'):
    os.makedirs(output_dir, exist_ok=True)

    utc_now = datetime.now(timezone.utc)
    bj_time = utc_now + timedelta(hours=8)
    time_str = bj_time.strftime('%Y%m%d %H:%M:%S')

    txt_path = os.path.join(output_dir, 'freetv.txt')
    txt_lines = [f'#更新时间 {time_str}']
    m3u_path = os.path.join(output_dir, 'freetv.m3u')
    epg_url = 'https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml'
    m3u_lines = [f'#EXTM3U x-tvg-url="{epg_url}"']

    for cat in template.categories:
        mains = template.category_channels.get(cat, [])
        avail = [m for m in mains if m in all_channels and all_channels[m]]
        if not avail:
            continue
        txt_lines.append(f'{cat},#genre#')
        for main in avail:
            logo = template.get_logo_url(main)
            for url, speed in all_channels[main]:
                txt_lines.append(f'{main},{url}')
            for url, speed in all_channels[main]:
                m3u_lines.append(
                    f'#EXTINF:-1 tvg-name="{main}" tvg-logo="{logo}" group-title="{cat}",{main}'
                )
                m3u_lines.append(url)

    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines) + '\n')
    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines) + '\n')

    total_src = sum(len(v) for v in all_channels.values())
    print(f"\n输出文件：")
    print(f"  {txt_path} ({total_src} 个源)")
    print(f"  {m3u_path} ({total_src} 个源)")


# ====================== 主流程 ======================
async def main():
    # global 声明必须在所有全局变量使用之前
    global SPEED_THRESHOLD, CHECK_TIMEOUT, MAX_CONCURRENT

    parser = argparse.ArgumentParser(description='IPTV频道源测速工具')
    parser.add_argument('--threshold', type=int, default=SPEED_THRESHOLD)
    parser.add_argument('--timeout', type=int, default=CHECK_TIMEOUT)
    parser.add_argument('--concurrency', type=int, default=MAX_CONCURRENT)
    parser.add_argument('--template', default='freetv/dome.txt')
    parser.add_argument('--output', default='freetv')
    parser.add_argument('--blacklist', default='freetv/blacklist.txt')
    parser.add_argument('--sources', default='',
                        help='源URL文件，每行一个；留空使用内置列表')
    args = parser.parse_args()

    SPEED_THRESHOLD = args.threshold
    CHECK_TIMEOUT = args.timeout
    MAX_CONCURRENT = args.concurrency

    print("=" * 85)
    print("IPTV频道源测速工具 v3.4 (接口型容错/Range降级/同源Referer/HLS多media)")
    print(f"阈值:{SPEED_THRESHOLD}KB/s 超时:{CHECK_TIMEOUT}s 并发:{MAX_CONCURRENT}")
    print("=" * 85)

    blacklist = Blacklist(args.blacklist)
    template = ChannelTemplate(args.template)
    if not template.load():
        return

    if args.sources.strip():
        with open(args.sources, 'r', encoding='utf-8') as f:
            source_urls = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    else:
        source_urls = [
            "https://sub.ottiptv.cc/yylunbo.m3u",
            "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
            "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt",
            "https://raw.githubusercontent.com/lbxxxtw2/iptv/refs/heads/master/output/tv.txt",
            "https://raw.githubusercontent.com/qingtian6325-lang/IPTV/refs/heads/main/mytv.m3u",
        ]

    print("\n从网络源获取频道列表...")
    all_raw = await fetch_channels_from_urls(source_urls)
    print(f"总共获取到 {len(all_raw)} 个频道源")
    if not all_raw:
        print("错误：未获取到任何频道源")
        return

    known_names = template.get_template_names()
    known = []
    unknown = []
    for name, url in all_raw:
        if name in known_names:
            known.append((name, url))
        else:
            unknown.append((name, url))

    for name, url in unknown:
        template.add_to_other(name)
    print(f"已知频道: {len(known)}, 未知频道(归入其它): {len(unknown)}")

    std_list = [(template.get_main(name), url) for name, url in known + unknown]
    print(f"待测源总数: {len(std_list)}")

    async with AsyncSpeedTester(blacklist) as tester:
        results, stats = await tester.batch_test(std_list, template)

    print("\n" + "=" * 85)
    print("测速完成！")
    print(f"  总测试源数: {stats['total']}")
    print(f"  通过(≥{SPEED_THRESHOLD}KB/s): {stats['passed']}")
    print(f"  低速(连通但未达标): {stats['low']}")
    print(f"  失败(连接/超时/4xx5xx): {stats['fail']}")
    print(f"  跳过(黑名单): {stats['skip']}")
    if stats['speeds']:
        print(f"  平均速度: {statistics.mean(stats['speeds']):.1f} KB/s")
        print(f"  最高速度: {stats['max']:.1f} KB/s")
        print(f"  最低速度: {stats['min']:.1f} KB/s")
    print(f"  通过频道数: {len(results)}")

    save_output(results, template, args.output)

    print("\n分类统计：")
    for cat in template.categories:
        mains = template.category_channels.get(cat, [])
        avail = [m for m in mains if m in results]
        src_cnt = sum(len(results[m]) for m in avail)
        print(f"  {cat}: {len(avail)}/{len(mains)} 频道, {src_cnt} 源")

    print("\n完成！")


if __name__ == '__main__':
    asyncio.run(main())
