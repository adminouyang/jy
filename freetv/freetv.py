#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 连通性 + 分辨率 + 测速筛选工具 v3.1
- 模板解析：支持任意emoji/符号前缀的分类行
- 连通性测试：HEAD优先，失败降级GET，仅接受200
- 分辨率解析：ffprobe获取宽高，映射到8K/4K/1080p/720p/576p/480p/360p/unknown
- 测速：ffmpeg下载小片段，计算速度(KB/s)
- 阈值筛选：按分辨率设定速度下限，每个分类+域名取前2个测速样本，均达标则保留该组全部URL
- 输出：高清(≥720p)→freetv.txt/freetv.m3u；标清(<720p)→freetv/标清.txt
- 打印每个频道的名称、URL、分辨率、速度(KB/s)
"""

import asyncio
import aiohttp
import ssl
import os
import re
import time
import subprocess
import tempfile
import json
from urllib.parse import urlparse, urldefrag
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# ====================== 配置 ======================
CHECK_TIMEOUT = 5          # 连通性超时（秒）
RESOLUTION_TIMEOUT = 8     # 分辨率解析超时（秒）
SPEED_TIMEOUT = 8          # 测速超时（秒）
MAX_CONCURRENT = 30        # 连通性并发数
SPEED_CONCURRENT = 5       # 测速并发数（子进程较慢）
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Connection': 'keep-alive',
    'Referer': 'https://www.google.com/',
}

# 分辨率优先级（用于排序，越高越清晰）
RESOLUTION_PRIORITY = {
    '8K': 100,
    '4K': 95,
    '2160p': 90,
    '1080p': 82,
    '1080i': 78,
    '720p': 72,
    '576p': 62,
    '480p': 52,
    '360p': 42,
    'unknown': 15,
}

# 各分辨率速度阈值（KB/s），可根据实际调整
SPEED_THRESHOLD_KBPS = {
    '8K': 2048,      # 50 MB/s
    '4K': 1024,      # 25 MB/s
    '1080p': 700,   # 10 MB/s
    '1080i': 650,    # 8 MB/s
    '720p': 600,     # 5 MB/s
    '576p': 170,     # 3 MB/s
    '480p': 150,     # 2 MB/s
    '360p': 100,     # 1 MB/s
    'unknown': 1024,  # 保守值
}

# emoji清洗
EMOJI_RE = re.compile(
    "[" 
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "]+", flags=re.UNICODE
)
WS_RE = re.compile(r'[\u3000\s]+')

def clean_cat_name(raw: str) -> str:
    s = EMOJI_RE.sub('', raw)
    s = WS_RE.sub('', s)
    return s.strip(' ,，、-')

# ====================== 黑名单 ======================
class Blacklist:
    def __init__(self, path='freetv/blacklist.txt'):
        self.path = path
        self.domains = set()
        self._lock = asyncio.Lock()
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            with open(self.path, 'w', encoding='utf-8') as f:
                f.write("# IPTV黑名单域名列表\n# 每行一个域名，#开头为注释\n")
            print(f"已创建黑名单文件: {self.path}")
            return
        with open(self.path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    self.domains.add(line)
        print(f"黑名单加载完毕: {len(self.domains)} 个域名")

    @staticmethod
    def host_of(url: str) -> str:
        try:
            netloc = urlparse(url).netloc
            if '@' in netloc:
                netloc = netloc.split('@', 1)[1]
            if ':' in netloc:
                netloc = netloc.split(':', 1)[0]
            return netloc.lower()
        except Exception:
            return ''

    async def add(self, url: str):
        host = self.host_of(url)
        if not host:
            return
        async with self._lock:
            if host in self.domains:
                return
            self.domains.add(host)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(host + '\n')
            self._dedup()

    def _dedup(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        seen = set()
        out = []
        for ln in lines:
            st = ln.strip()
            if st.startswith('#') or st == '':
                out.append(ln if ln.endswith('\n') else ln + '\n')
                continue
            if st in seen:
                continue
            seen.add(st)
            out.append(ln if ln.endswith('\n') else ln + '\n')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.writelines(out)

    def contains(self, url: str) -> bool:
        return self.host_of(url) in self.domains

# ====================== 模板 ======================
class ChannelTemplate:
    def __init__(self, path):
        self.path = path
        self.categories = []
        self.channel_map = {}          # alias -> main
        self.main_channels = {}        # main -> cat
        self.category_channels = {}    # cat -> [main...]

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
                if '#genre#' in line:
                    head = line.split('#genre#', 1)[0]
                    if ',' in head:
                        head = head.split(',', 1)[0]
                    cat = clean_cat_name(head)
                    if cat and cat not in self.categories:
                        self.categories.append(cat)
                        self.category_channels[cat] = []
                    current_cat = cat
                    continue
                if current_cat and ',' in line:
                    items = [x.strip() for x in line.split(',') if x.strip()]
                    if not items:
                        continue
                    main = items[0]
                    self.main_channels.setdefault(main, current_cat)
                    if main not in self.category_channels[current_cat]:
                        self.category_channels[current_cat].append(main)
                    for alias in items:
                        self.channel_map.setdefault(alias, main)

        other = '其它频道'
        self.categories = [c for c in self.categories if c != other]
        if other not in self.categories:
            self.categories.append(other)
        self.category_channels.setdefault(other, [])

        print(f"模板加载完成：{len(self.categories)} 个分类，{len(self.channel_map)} 个别名")
        return True

    def add_to_other(self, name):
        other = '其它频道'
        self.main_channels.setdefault(name, other)
        if name not in self.category_channels[other]:
            self.category_channels[other].append(name)
        self.channel_map.setdefault(name, name)

    def get_main(self, name):
        return self.channel_map.get(name, name)

    def get_logo_url(self, name):
        main = self.get_main(name)
        safe = re.sub(r'[\\/:*?"<>|]', '', main).strip()
        return f"https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/{safe}.png"

    def template_names(self):
        return set(self.channel_map.keys())

# ====================== 分辨率与测速工具 ======================
def parse_resolution(width, height):
    """根据宽高解析分辨率等级"""
    if not width or not height or width == 0 or height == 0:
        return 'unknown'
    if width >= 7680 or height >= 4320:
        return '8K'
    if width >= 3840 or height >= 2160:
        return '4K'
    if width >= 1920 or height >= 1080:
        return '1080p'
    if width >= 1440 or height >= 900:
        return '720p'
    if width >= 960 or height >= 540:
        return '576p'
    if width >= 854 or height >= 480:
        return '480p'
    return '360p'

def get_resolution_info(url, timeout=RESOLUTION_TIMEOUT):
    """使用ffprobe获取视频分辨率"""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-timeout', str(int(timeout * 1e6)),
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        if result.returncode != 0:
            return 'unknown', 0, 0
        data = json.loads(result.stdout)
        video_streams = [s for s in data.get('streams', []) if s.get('codec_type') == 'video']
        if not video_streams:
            return 'unknown', 0, 0
        stream = video_streams[0]
        width = stream.get('width', 0)
        height = stream.get('height', 0)
        resolution = parse_resolution(width, height)
        return resolution, width, height
    except Exception:
        return 'unknown', 0, 0

def test_speed(url, timeout=SPEED_TIMEOUT):
    """使用ffmpeg测试流媒体速度，返回速度KB/s"""
    try:
        with tempfile.NamedTemporaryFile(delete=True, suffix='.ts') as temp_file:
            cmd = [
                'ffmpeg', '-y',
                '-timeout', str(int(timeout * 1e6)),
                '-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '-i', url,
                '-t', str(timeout),
                '-c', 'copy',
                '-f', 'mpegts',
                temp_file.name
            ]
            start_time = time.time()
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                process.communicate(timeout=timeout + 2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            end_time = time.time()
            duration = end_time - start_time
            try:
                file_size = os.path.getsize(temp_file.name)
                if file_size > 10240 and duration > 0.1:
                    speed_kbps = (file_size / duration) / 1024
                    return round(speed_kbps, 2)
            except:
                pass
    except:
        pass
    return 0.0

def clean_url(url):
    """去除URL中$及其后内容"""
    if '$' in url:
        url = url.split('$')[0]
    return url

# ====================== 源获取 ======================
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
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def sanitize_url(raw):
    u = raw.strip()
    u, _ = urldefrag(u)
    if u.startswith(('http://', 'https://')):
        return u
    return None

def parse_m3u(text):
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            parts = line.split(',', 1)
            if len(parts) >= 2:
                name = clean_m3u_name(parts[1])
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith('#')):
                    j += 1
                if j < len(lines):
                    u = sanitize_url(lines[j])
                    if u and name:
                        out.append((name, u))
                    i = j
        i += 1
    return out

def parse_txt(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if '#genre#' in line or not line or ',' not in line:
            continue
        if '://' not in line:
            continue
        try:
            name, url = line.split(',', 1)
            u = sanitize_url(url)
            name = re.sub(r'^\[[A-Z0-9]+\]\s*', '', name).strip()
            if u and name:
                out.append((name, u))
        except Exception:
            pass
    return out

async def fetch_all(urls):
    res = []
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        for u in urls:
            t = await fetch_text(s, u)
            if not t:
                continue
            if t.lstrip().startswith('#EXTM3U'):
                chs = parse_m3u(t)
                print(f"  M3U源 {u}: {len(chs)} 个频道")
            else:
                chs = parse_txt(t)
                print(f"  TXT源 {u}: {len(chs)} 个频道")
            res.extend(chs)
    return res

# ====================== 连通性测试 ======================
class ConnectivityTester:
    def __init__(self, blacklist: Blacklist):
        self.bl = blacklist
        self.stats = {'total': 0, 'passed': 0, 'failed': 0}
        self.session = None
        self.sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def __aenter__(self):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        conn = aiohttp.TCPConnector(
            limit=MAX_CONCURRENT * 2,
            ttl_dns_cache=600,
            ssl=ssl_ctx,
            force_close=False,
        )
        self.session = aiohttp.ClientSession(
            connector=conn,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(
                total=CHECK_TIMEOUT,
                connect=CHECK_TIMEOUT,
                sock_connect=CHECK_TIMEOUT,
                sock_read=CHECK_TIMEOUT,
            ),
        )
        return self

    async def __aexit__(self, *a):
        await self.session.close()

    async def test_one(self, url: str, name: str):
        if self.bl.contains(url):
            print(f"⏭️  黑名单跳过: {name:<12}| {self.bl.host_of(url)}")
            return False, 0

        clean, _ = urldefrag(url)
        if clean != url:
            url = clean

        async with self.sem:
            start = time.time()
            try:
                try:
                    async with self.session.head(url, allow_redirects=True) as r:
                        status = r.status
                        ms = (time.time() - start) * 1000
                except (aiohttp.ClientResponseError, aiohttp.ClientError):
                    async with self.session.get(url, allow_redirects=True) as r:
                        status = r.status
                        await r.content.read(4096)
                        ms = (time.time() - start) * 1000
                except Exception:
                    raise

                if status == 200:
                    self.stats['total'] += 1
                    self.stats['passed'] += 1
                    print(f"✅ {name:<12}|{url[:80]:<80}|{ms:>7.0f} ms|200")
                    return True, url
                else:
                    self.stats['total'] += 1
                    self.stats['failed'] += 1
                    print(f"❌ {name:<12}|{url[:80]:<80}|{ms:>7.0f} ms|{status} -> 写黑名单")
                    await self.bl.add(url)
                    return False, url

            except asyncio.TimeoutError:
                ms = (time.time() - start) * 1000
                self.stats['total'] += 1
                self.stats['failed'] += 1
                print(f"❌ {name:<12}|{url[:80]:<80}|{ms:>7.0f} ms|超时 -> 写黑名单")
                await self.bl.add(url)
                return False, url
            except Exception as e:
                ms = (time.time() - start) * 1000
                self.stats['total'] += 1
                self.stats['failed'] += 1
                print(f"❌ {name:<12}|{url[:80]:<80}|{ms:>7.0f} ms|{str(e)[:25]} -> 写黑名单")
                await self.bl.add(url)
                return False, url

    async def batch(self, channel_list):
        tasks = []
        for main, url in channel_list:
            tasks.append(self.test_one(url, main))
        results = await asyncio.gather(*tasks)
        passed = []
        for (main, url), (ok, _) in zip(channel_list, results):
            if ok:
                passed.append((main, url))
        return passed, self.stats

# ====================== 测速筛选 ======================
class SpeedFilter:
    def __init__(self, tpl: ChannelTemplate):
        self.tpl = tpl
        self.results = {}  # (category, domain) -> list of dict
        self.passed_groups = set()  # (category, domain) that passed threshold

    def add_url(self, main, url):
        """添加一个通过连通性的URL，进行分辨率解析和测速，并打印详细信息"""
        url = clean_url(url)
        domain = Blacklist.host_of(url)
        cat = self.tpl.main_channels.get(main, '其它频道')
        key = (cat, domain)

        # 分辨率解析
        resolution, w, h = get_resolution_info(url)
        # 测速
        speed = test_speed(url)

        # 打印详细信息
        speed_str = f"{speed:.2f} KB/s" if speed > 0 else "N/A"
        print(f"📊 {main:<16} | {url[:60]:<60} | {resolution:<8} | {speed_str:<12}")

        self.results.setdefault(key, []).append({
            'main': main,
            'url': url,
            'resolution': resolution,
            'speed': speed,
            'domain': domain,
            'cat': cat
        })

    def apply_threshold(self):
        """应用阈值筛选，决定哪些组保留"""
        for key, entries in self.results.items():
            cat, domain = key
            # 找出测速成功的条目（speed > 0）
            speed_entries = [e for e in entries if e['speed'] > 0]
            if not speed_entries:
                # 没有测速成功的，无条件保留（可能是IPv6等）
                self.passed_groups.add(key)
                continue

            # 取前2个测速成功的
            samples = speed_entries[:2]
            all_pass = True
            for sample in samples:
                res = sample['resolution']
                thr = SPEED_THRESHOLD_KBPS.get(res, 1024)
                if sample['speed'] < thr:
                    all_pass = False
                    break
            if all_pass:
                self.passed_groups.add(key)

    def get_filtered_urls(self):
        """返回通过筛选的URL列表，以及按分辨率分类的列表"""
        hd_urls = []   # 高清 (>=720p)
        sd_urls = []   # 标清 (<720p)
        for key, entries in self.results.items():
            if key not in self.passed_groups:
                continue
            for e in entries:
                res = e['resolution']
                priority = RESOLUTION_PRIORITY.get(res, 0)
                if priority >= RESOLUTION_PRIORITY['720p']:  # 720p及以上
                    hd_urls.append((e['main'], e['url']))
                else:
                    sd_urls.append((e['main'], e['url']))
        return hd_urls, sd_urls

# ====================== 输出 ======================
def save_output(hd_urls, sd_urls, tpl, out_dir='freetv'):
    os.makedirs(out_dir, exist_ok=True)
    bj = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y%m%d %H:%M:%S')
    epg = 'https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml'

    # 高清输出
    txt_path = os.path.join(out_dir, 'freetv.txt')
    m3u_path = os.path.join(out_dir, 'freetv.m3u')
    txt_lines = ['#genre#', f'更新时间,{bj}', '']
    m3u_lines = [f'#EXTM3U x-tvg-url="{epg}"']

    # 按分类组织高清
    hd_by_cat = defaultdict(list)
    for main, url in hd_urls:
        cat = tpl.main_channels.get(main, '其它频道')
        hd_by_cat[cat].append((main, url))

    for cat in tpl.categories:
        items = hd_by_cat.get(cat, [])
        if not items:
            continue
        txt_lines.append(f'{cat},#genre#')
        seen_main = set()
        for main, url in items:
            if main not in seen_main:
                seen_main.add(main)
                txt_lines.append(f'{main},{url}')
                logo = tpl.get_logo_url(main)
                m3u_lines.append(
                    f'#EXTINF:-1 tvg-name="{main}" tvg-logo="{logo}" group-title="{cat}", {main}'
                )
                m3u_lines.append(url)
            else:
                # 同一个主名可能有多个URL，只保留第一个
                pass

    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))
    print(f"\n高清输出：{txt_path} ({len(set(m for m,u in hd_urls))} 频道)")

    # 标清输出
    sd_path = os.path.join(out_dir, '标清.txt')
    sd_lines = ['#genre#', f'更新时间,{bj}', '']
    sd_by_cat = defaultdict(list)
    for main, url in sd_urls:
        cat = tpl.main_channels.get(main, '其它频道')
        sd_by_cat[cat].append((main, url))

    for cat in tpl.categories:
        items = sd_by_cat.get(cat, [])
        if not items:
            continue
        sd_lines.append(f'{cat},#genre#')
        seen_main = set()
        for main, url in items:
            if main not in seen_main:
                seen_main.add(main)
                sd_lines.append(f'{main},{url}')

    with open(sd_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(sd_lines))
    print(f"标清输出：{sd_path} ({len(set(m for m,u in sd_urls))} 频道)")

# ====================== 主流程 ======================
async def main():
    print("=" * 96)
    print("IPTV 连通性 + 分辨率 + 测速筛选（阈值自适应）")
    print("=" * 96)

    bl = Blacklist('freetv/blacklist.txt')
    tpl = ChannelTemplate('freetv/dome.txt')
    if not tpl.load():
        return

    source_urls = [
        "https://sub.ottiptv.cc/yylunbo.m3u",
        "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
        "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt",
        "https://raw.githubusercontent.com/lbxxxtw2/iptv/refs/heads/master/output/tv.txt",
        "https://raw.githubusercontent.com/qingtian6325-lang/IPTV/refs/heads/main/mytv.m3u",
    ]

    print("\n从网络源获取频道列表...")
    raw = await fetch_all(source_urls)
    print(f"共获取 {len(raw)} 个源")
    if not raw:
        print("未获取到任何源")
        return

    # 映射到模板主名
    known, unknown = [], []
    names = tpl.template_names()
    for nm, u in raw:
        if nm in names:
            known.append((nm, u))
        else:
            unknown.append((nm, u))
    for nm, u in unknown:
        tpl.add_to_other(nm)
    print(f"已知频道源 {len(known)}，未知归入其它 {len(unknown)}")

    std = [(tpl.get_main(nm), u) for nm, u in known + unknown]
    print(f"待测源总数: {len(std)}")

    # 连通性测试
    async with ConnectivityTester(bl) as tester:
        passed_list, stats = await tester.batch(std)

    print(f"\n连通性通过：{len(passed_list)} 个源")
    if not passed_list:
        print("没有可用的源，退出")
        return

    # 分辨率解析 + 测速 + 阈值筛选
    print("\n开始分辨率解析与测速（并发数{}）...".format(SPEED_CONCURRENT))
    filter_obj = SpeedFilter(tpl)

    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=SPEED_CONCURRENT)
    futures = []
    for main, url in passed_list:
        future = loop.run_in_executor(executor, lambda m=main, u=url: filter_obj.add_url(m, u))
        futures.append(future)

    total = len(futures)
    done = 0
    for coro in asyncio.as_completed(futures):
        await coro
        done += 1
        if done % 20 == 0 or done == total:
            print(f"  进度: {done}/{total}")
    print("分辨率与测速完成")

    # 应用阈值
    filter_obj.apply_threshold()
    hd_urls, sd_urls = filter_obj.get_filtered_urls()
    print(f"筛选后高清源: {len(hd_urls)}, 标清源: {len(sd_urls)}")

    # 输出
    save_output(hd_urls, sd_urls, tpl)

    # 分类统计
    print("\n分类统计（高清）：")
    hd_by_cat = defaultdict(set)
    for main, url in hd_urls:
        cat = tpl.main_channels.get(main, '其它频道')
        hd_by_cat[cat].add(main)
    for cat in tpl.categories:
        cnt = len(hd_by_cat.get(cat, []))
        if cnt:
            print(f"  {cat}: {cnt} 频道")

    print("\n标清分类统计：")
    sd_by_cat = defaultdict(set)
    for main, url in sd_urls:
        cat = tpl.main_channels.get(main, '其它频道')
        sd_by_cat[cat].add(main)
    for cat in tpl.categories:
        cnt = len(sd_by_cat.get(cat, []))
        if cnt:
            print(f"  {cat}: {cnt} 频道")

    executor.shutdown()

if __name__ == '__main__':
    asyncio.run(main())
