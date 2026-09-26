#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV频道源测速工具 v3.8 (aiohttp 稳定版)
改进：
  1. 分片测速重试 + 扩展候选分片（最多5个）
  2. 针对特定域名动态添加 Referer
  3. ffprobe 兜底（可选，需安装 FFmpeg）
  4. 保留 v3.7 所有优点：魔数检测、双阈值、自动黑名单
"""

import asyncio
import aiohttp
import ssl
import statistics
import os
import re
import time
import subprocess
from urllib.parse import urlparse, urljoin, urldefrag, urlunparse
from datetime import datetime, timedelta, timezone

# ====================== 全局配置 ======================
AVAILABLE_THRESHOLD = 150     # KB/s，可用线
FAST_THRESHOLD = 600          # KB/s，优质线
CHECK_TIMEOUT = 5             # 秒（首次测速）
FALLBACK_TIMEOUT = 8          # 秒（备用测速）
MAX_CONCURRENT = 12           # 并发数（比 v3.7 略低，更稳定）
DEEP_TEST_SIZE = 524288       # 字节 (~512KB)
STEADY_BYTES = 196608         # 排除前192KB爆发期
MIN_TEST_TIME = 2.5           # 秒

# ffprobe 兜底开关（设为 False 可关闭，无需安装 FFmpeg）
ENABLE_FFPROBE_FALLBACK = True

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'VLC/3.0.18 LibVLC/3.0.18',
    'PotPlayer/210603 (Windows NT 10.0; Win64; x64)',
    'Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1'
]

BASE_HEADERS = {
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Connection': 'keep-alive',
    'Referer': 'https://www.google.com/',
}

# ---------- 针对特定域名的 Referer 映射 ----------
SPECIAL_REFERER = {
    'jdshipin.com': 'https://www.jdshipin.com/',
    'tvbus.cc': 'http://tvbus.cc/',
    'rr.kg': 'http://rr.kg/',
    'bkpcp.top': 'http://bkpcp.top/',
    '061899.xyz': 'http://061899.xyz/',
    'chinacert.cftest5.cn': 'https://live.chinacert.cftest5.cn/',
}

# ====================== 黑名单管理（同 v3.7） ======================
class Blacklist:
    def __init__(self, path='freetv/blacklist.txt'):
        self.path = path
        self.domains = set()
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
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
        except:
            return False

    def add(self, url):
        try:
            domain = urlparse(url).netloc
            if ':' in domain:
                domain = domain.split(':')[0]
            if domain not in self.domains:
                self.domains.add(domain)
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write(f"{domain}\n")
                print(f"🚫 已将 {domain} 加入黑名单")
        except:
            pass

# ====================== 频道模板处理（同 v3.7） ======================
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
        if other_cat not in self.categories:
            self.categories.append(other_cat)
            self.category_channels[other_cat] = []
        else:
            self.categories.remove(other_cat)
            self.categories.append(other_cat)

        print(f"模板加载完成：{len(self.categories)} 个分类，{len(self.channel_map)} 个别名")
        return True

    def add_to_other(self, name):
        other_cat = '其它频道'
        if other_cat not in self.categories:
            self.categories.append(other_cat)
            self.category_channels[other_cat] = []
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

# ====================== 频道列表获取（同 v3.7） ======================
async def fetch_text(session, url):
    try:
        async with session.get(url, timeout=10) as resp:
            return await resp.text()
    except Exception as e:
        print(f"获取失败 {url}: {e}")
        return ''

def clean_m3u_name(raw):
    name = re.sub(r'\([^)]*\)', '', raw)
    name = re.sub(r'\[[^\]]*\]', '', name)
    name = name.strip()
    name = re.sub(r'\s+', ' ', name)
    return name

def sanitize_url(raw_url):
    url = raw_url.strip()
    url, _ = urldefrag(url)
    if '$' in url:
        url = url.split('$')[0]
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
                    raw_url = lines[j].strip()
                    url = sanitize_url(raw_url)
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
            except:
                pass
    return channels

async def fetch_channels_from_urls(urls):
    all_channels = []
    async with aiohttp.ClientSession(headers=BASE_HEADERS) as session:
        for url in urls:
            text = await fetch_text(session, url)
            if not text:
                continue
            if text.strip().startswith('#EXTM3U'):
                chs = parse_m3u(text)
                print(f"  M3U源 {url}: {len(chs)} 个频道")
            else:
                chs = parse_txt(text)
                print(f"  TXT源 {url}: {len(chs)} 个频道")
            all_channels.extend(chs)
    return all_channels

# ====================== 视频魔数检测（同 v3.7） ======================
def detect_video_magic(data: bytes):
    if len(data) < 4:
        return None
    if data.startswith(b'#EXTM3U'):
        return 'M3U8'
    if data[0] == 0x47:
        return 'TS'
    if data[:3] == b'FLV':
        return 'FLV'
    if data[4:8] == b'ftyp' or data[:4] == b'\x00\x00\x00\x1c':
        return 'MP4'
    head = data[:1024].decode('utf-8', errors='ignore').lower()
    if any(x in head for x in ['<!doctype html', '<html', 'gitea', '登录', 'webautn']):
        return 'HTML'
    if len(data) >= 64:
        return 'BINARY'
    return None

# ====================== ffprobe 兜底函数 ======================
async def ffprobe_check(url):
    """调用 ffprobe 检查 URL 是否包含视频流，返回 True/False"""
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=codec_type',
        '-of', 'csv=p=0',
        url
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        return b'video' in stdout
    except (asyncio.TimeoutError, FileNotFoundError, subprocess.SubprocessError):
        return False

# ====================== 异步测速引擎（增强版） ======================
class AsyncSpeedTester:
    def __init__(self, blacklist):
        self.blacklist = blacklist
        self.stats = {'total': 0, 'available': 0, 'fast': 0,
                      'failed': 0, 'speeds': [], 'max': 0, 'min': float('inf')}
        self.session = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def __aenter__(self):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        conn = aiohttp.TCPConnector(
            limit=MAX_CONCURRENT * 2,
            ttl_dns_cache=600,
            ssl=ssl_ctx,
            force_close=False
        )
        self.session = aiohttp.ClientSession(
            connector=conn,
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT + 2)
        )
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    def _build_headers(self, url, ua_index=0):
        """构建请求头，根据域名动态添加 Referer"""
        headers = BASE_HEADERS.copy()
        headers['User-Agent'] = USER_AGENTS[ua_index % len(USER_AGENTS)]

        # 针对特定域名设置 Referer
        domain = urlparse(url).hostname or ''
        for key, ref in SPECIAL_REFERER.items():
            if key in domain:
                headers['Referer'] = ref
                break
        return headers

    async def _quick_check(self, url, ua_index=0, timeout=3):
        """快速检查URL是否返回视频流，返回 (format, data)"""
        headers = self._build_headers(url, ua_index)
        try:
            async with self.session.get(url, headers=headers, timeout=timeout) as resp:
                data = await resp.content.read(8192)
                if len(data) < 32:
                    return 'TOO_SHORT', data
                fmt = detect_video_magic(data)
                if fmt:
                    return fmt, data
                return 'UNKNOWN', data
        except asyncio.TimeoutError:
            return 'TIMEOUT', b''
        except Exception as e:
            return f'ERR:{str(e)[:40]}', b''

    async def _measure_stream(self, stream_response, deep_size=DEEP_TEST_SIZE,
                              steady_bytes=STEADY_BYTES, min_time=MIN_TEST_TIME):
        """通用测速函数（同 v3.7）"""
        downloaded = 0
        steady_downloaded = 0
        steady_start = None
        chunk_speeds = []
        chunk_start = time.time()
        test_start = time.time()

        async for chunk in stream_response.content.iter_chunked(32768):
            now = time.time()
            chunk_len = len(chunk)
            if chunk_len == 0:
                break
            elapsed = now - chunk_start
            if elapsed > 0.001:
                chunk_speeds.append(chunk_len / elapsed / 1024)
            chunk_start = now

            downloaded += chunk_len
            if downloaded > steady_bytes:
                if steady_start is None:
                    steady_start = now
                steady_downloaded += chunk_len

            if downloaded >= deep_size:
                break
            if (now - test_start) >= min_time and downloaded >= 65536:
                break

        total_time = time.time() - test_start
        if total_time <= 0 or downloaded < 4096:
            return 0.0, downloaded

        overall_speed = downloaded / total_time / 1024
        steady_speed = 0
        if steady_downloaded > 0 and steady_start:
            steady_elapsed = time.time() - steady_start
            if steady_elapsed > 0:
                steady_speed = steady_downloaded / steady_elapsed / 1024
        median_speed = statistics.median(chunk_speeds) if len(chunk_speeds) >= 3 else overall_speed

        final_speed = 0.5 * steady_speed + 0.3 * overall_speed + 0.2 * median_speed
        return final_speed, downloaded

    async def _download_m3u8_segment(self, seg_url, ua_index, timeout=CHECK_TIMEOUT):
        """下载单个 m3u8 分片并测速，返回速度 KB/s"""
        headers = self._build_headers(seg_url, ua_index)
        try:
            async with self.session.get(seg_url, headers=headers, timeout=timeout) as resp:
                speed, downloaded = await self._measure_stream(resp, deep_size=262144, min_time=1.5)
                return speed
        except:
            return 0.0

    async def _try_speed_test(self, url, ua_index=0, timeout=CHECK_TIMEOUT,
                              deep_size=DEEP_TEST_SIZE):
        """增强版测速：m3u8 分片重试 + 扩展候选"""
        headers = self._build_headers(url, ua_index)
        try:
            start = time.time()
            async with self.session.get(url, headers=headers, timeout=timeout) as resp:
                ttfb = time.time() - start
                if ttfb > 2.5:
                    return 0.0

                body_preview = await resp.content.read(2048)
                resp.content.unread_data(body_preview)

                is_m3u8 = body_preview.startswith(b'#EXTM3U')

                if is_m3u8:
                    # 解析完整播放列表
                    playlist_text = body_preview.decode('utf-8', errors='ignore')
                    remaining = await resp.content.read()
                    full_playlist = playlist_text + remaining.decode('utf-8', errors='ignore')

                    base_url = url
                    seg_urls = []
                    has_extinf = False
                    for line in full_playlist.splitlines():
                        line = line.strip()
                        if line.startswith('#EXTINF'):
                            has_extinf = True
                        elif line and not line.startswith('#') and has_extinf:
                            seg_url = urljoin(base_url, line)
                            parsed_base = urlparse(base_url)
                            parsed_seg = urlparse(seg_url)
                            if not parsed_seg.query and parsed_base.query:
                                seg_url = urlunparse(parsed_seg._replace(query=parsed_base.query))
                            seg_url, _ = urldefrag(seg_url)
                            seg_urls.append(seg_url)
                            if len(seg_urls) >= 5:   # 扩展到最多5个候选
                                break

                    if not has_extinf or not seg_urls:
                        return 0.0

                    # 依次测试分片，优先测试前2个，失败则尝试后面的
                    best_speed = 0.0
                    for idx, seg_url in enumerate(seg_urls):
                        # 尝试两个不同的 UA
                        for ua_offset in range(2):
                            speed = await self._download_m3u8_segment(
                                seg_url,
                                ua_index=(ua_index + ua_offset) % len(USER_AGENTS),
                                timeout=timeout
                            )
                            if speed > best_speed:
                                best_speed = speed
                            if best_speed >= FAST_THRESHOLD:
                                return best_speed   # 优质就提前返回
                        # 如果前两个分片都失败，继续尝试下一个
                        if best_speed == 0.0 and idx < 2:
                            continue
                        # 如果已有速度但不高，也继续尝试更多分片
                        if best_speed > 0 and best_speed < AVAILABLE_THRESHOLD:
                            continue
                        # 如果已有一定速度，停止
                        if best_speed >= AVAILABLE_THRESHOLD:
                            break
                    return best_speed
                else:
                    # 直连流
                    speed, downloaded = await self._measure_stream(resp)
                    return speed

        except asyncio.TimeoutError:
            return 0.0
        except Exception:
            return 0.0

    async def test_one(self, url, channel_name):
        """测试单个源，返回速度KB/s，失败返回0"""
        if self.blacklist.contains(url):
            domain = urlparse(url).netloc
            print(f"⏭️  黑名单跳过: {channel_name:<10}| {domain}")
            return 0.0

        # 清理URL
        clean_url, _ = urldefrag(url)
        if '$' in clean_url:
            clean_url = clean_url.split('$')[0]
        if clean_url != url:
            print(f"🔧 自动清理URL: {channel_name:<10}| {url[:55]} → {clean_url[:55]}")
            url = clean_url

        # 快速检查（两次）
        fmt, data = await self._quick_check(url, ua_index=0, timeout=3)
        if fmt in ('HTML', 'TOO_SHORT', 'UNKNOWN', 'TIMEOUT'):
            fmt2, data2 = await self._quick_check(url, ua_index=1, timeout=3)
            if fmt2 in ('HTML', 'TOO_SHORT', 'UNKNOWN', 'TIMEOUT'):
                print(f"🌐 快速检查无效({fmt2}): {channel_name:<10}| {url[:85]}")
                if fmt2 == 'HTML':
                    self.blacklist.add(url)
                return 0.0
            else:
                print(f"🌐 二次检查有效({fmt2}): {channel_name:<10}| {url[:85]}")
        else:
            print(f"🌐 快速检查有效({fmt}): {channel_name:<10}| {url[:85]}")

        # 正式测速（首次）
        speed_first = await self._try_speed_test(url, ua_index=0, timeout=CHECK_TIMEOUT)
        if speed_first >= FAST_THRESHOLD:
            self._update_stats(speed_first, True)
            print(f"✅ {channel_name:<10}|{url[:85]:<85}|速度:{speed_first:>7.1f} KB/s|优质")
            return speed_first

        # 备用测速（更换UA、增加超时）
        print(f"🔄 {channel_name:<10}|{url[:85]:<85}|首次测速不佳({speed_first:.1f}KB/s)，启用备用测速...")
        speed_fallback = await self._try_speed_test(url, ua_index=2, timeout=FALLBACK_TIMEOUT,
                                                     deep_size=393216)
        final_speed = max(speed_first, speed_fallback)

        # 如果最终速度为0且快速检查为M3U8，尝试ffprobe兜底
        if final_speed == 0.0 and ENABLE_FFPROBE_FALLBACK:
            print(f"🔍 尝试 ffprobe 兜底: {channel_name:<10}| {url[:85]}")
            if await ffprobe_check(url):
                final_speed = 1.0   # 标记为“可用但速度未知”
                print(f"  ✅ ffprobe 确认可播，标记为可用")
            else:
                print(f"  ❌ ffprobe 也无法播放")

        self._update_stats(final_speed, final_speed >= AVAILABLE_THRESHOLD)

        status = '✅' if final_speed >= FAST_THRESHOLD else ('⚠️' if final_speed >= AVAILABLE_THRESHOLD else '❌')
        note = f"备用({speed_fallback:.1f})" if speed_fallback > 0 else "备用失败"
        if final_speed == 1.0:
            note = "ffprobe兜底"
        print(f"{status} {channel_name:<10}|{url[:85]:<85}|速度:{final_speed:>7.1f} KB/s|{note}")
        return final_speed

    def _update_stats(self, speed, available):
        self.stats['total'] += 1
        if available:
            self.stats['available'] += 1
            if speed >= FAST_THRESHOLD:
                self.stats['fast'] += 1
        else:
            self.stats['failed'] += 1
        self.stats['speeds'].append(speed)
        self.stats['max'] = max(self.stats['max'], speed)
        self.stats['min'] = min(self.stats['min'], speed)

    async def batch_test(self, channel_list, template):
        groups = {}
        for main, url in channel_list:
            groups.setdefault(main, []).append(url)

        results = {}
        total_sources = len(channel_list)
        tested = 0

        print(f"\n开始并发测速，最大并发 {MAX_CONCURRENT}，共 {total_sources} 个源")
        print("=" * 200)

        for cat in template.categories:
            for main in template.category_channels.get(cat, []):
                if main not in groups:
                    continue
                urls = groups[main]
                tasks = [self.test_one(url, main) for url in urls]
                speeds = await asyncio.gather(*tasks)

                passed = [(url, sp) for url, sp in zip(urls, speeds) if sp >= AVAILABLE_THRESHOLD]
                passed.sort(key=lambda x: x[1], reverse=True)
                if passed:
                    results[main] = passed

                tested += len(urls)
                passed_now = sum(1 for sp in speeds if sp >= AVAILABLE_THRESHOLD)
                fast_now = sum(1 for sp in speeds if sp >= FAST_THRESHOLD)
                print(f"  {main:<20} 可用 {passed_now}/{len(urls)}  优质 {fast_now}/{len(urls)}  进度 {tested}/{total_sources}")
                print("-" * 200)

        return results, self.stats

# ====================== 文件输出（同 v3.7） ======================
def save_output(all_channels, template, output_dir='freetv'):
    os.makedirs(output_dir, exist_ok=True)

    utc_now = datetime.now(timezone.utc)
    bj_time = utc_now + timedelta(hours=8)
    time_str = bj_time.strftime('%Y%m%d %H:%M:%S')

    txt_path = os.path.join(output_dir, 'freetv.txt')
    txt_lines = ['#genre#', f'更新时间,{time_str}', '']

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
            sources = all_channels[main]
            for url, speed in sources:
                txt_lines.append(f'{main},{url}')
            logo = template.get_logo_url(main)
            for url, speed in sources:
                m3u_lines.append(
                    f'#EXTINF:-1 tvg-name="{main}" tvg-logo="{logo}" group-title="{cat}", {main}'
                )
                m3u_lines.append(url)

    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))

    total_src = sum(len(v) for v in all_channels.values())
    print(f"\n输出文件：")
    print(f"  {txt_path} ({total_src} 个源)")
    print(f"  {m3u_path} ({total_src} 个源)")

# ====================== 主流程（同 v3.7） ======================
async def main():
    print("=" * 160)
    print("IPTV频道源测速工具 v3.8 (aiohttp 增强版 · 分片重试 · 动态Referer · ffprobe兜底)")
    print("=" * 160)

    blacklist = Blacklist('freetv/blacklist.txt')
    template = ChannelTemplate('freetv/dome.txt')
    if not template.load():
        return

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

    print("\n" + "=" * 128)
    print("测速完成！")
    print(f"  总测试源数: {stats['total']}")
    print(f"  可用(≥{AVAILABLE_THRESHOLD}KB/s): {stats['available']}")
    print(f"  优质(≥{FAST_THRESHOLD}KB/s): {stats['fast']}")
    print(f"  失败: {stats['failed']}")
    if stats['speeds']:
        print(f"  平均速度: {statistics.mean(stats['speeds']):.1f} KB/s")
        print(f"  最高速度: {stats['max']:.1f} KB/s")
        print(f"  最低速度: {stats['min']:.1f} KB/s")
    print(f"  通过频道数: {len(results)}")

    save_output(results, template)

    print("\n分类统计：")
    for cat in template.categories:
        mains = template.category_channels.get(cat, [])
        avail = [m for m in mains if m in results]
        src_cnt = sum(len(results[m]) for m in avail)
        print(f"  {cat}: {len(avail)}/{len(mains)} 频道, {src_cnt} 源")

    print("\n完成！")

if __name__ == '__main__':
    asyncio.run(main())
