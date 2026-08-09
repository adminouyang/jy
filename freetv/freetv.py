#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV频道源测速工具 v3.2
修复：
- 自动移除URL中的#片段（解决#EXTM3U导致的测速失败）
- 添加Referer请求头，提高部分源的可用性
- 增强URL解析容错，过滤明显无效的行
"""

import asyncio
import aiohttp
import ssl
import statistics
import os
import re
import time
from urllib.parse import urlparse, urljoin, urldefrag
from datetime import datetime, timedelta, timezone

# ====================== 全局配置 ======================
SPEED_THRESHOLD = 600          # KB/s
CHECK_TIMEOUT = 5              # 秒
MAX_CONCURRENT = 30            # 最大并发数
DEEP_TEST_SIZE = 786432        # 字节 (~768KB)
STEADY_BYTES = 262144          # 排除前256KB爆发期
MIN_TEST_TIME = 1.5            # 秒

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Connection': 'keep-alive',
    'Referer': 'https://www.google.com/',  # 新增：模拟常见来源
}

# ====================== 黑名单管理 ======================
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

# ====================== 频道列表获取 ======================
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
    """清理URL：移除#片段，去除首尾空格"""
    url = raw_url.strip()
    # 移除#片段（如 #EXTM3U）
    url, _ = urldefrag(url)
    # 如果清理后为空或明显不是URL，返回None
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
    async with aiohttp.ClientSession(headers=HEADERS) as session:
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

# ====================== 异步测速引擎 ======================
class AsyncSpeedTester:
    def __init__(self, blacklist):
        self.blacklist = blacklist
        self.stats = {'total': 0, 'passed': 0, 'failed': 0,
                      'speeds': [], 'max': 0, 'min': float('inf')}
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
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT + 2)
        )
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def _measure_stream(self, stream_response, url_label, channel_name):
        """通用测速函数"""
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
            if downloaded > STEADY_BYTES:
                if steady_start is None:
                    steady_start = now
                steady_downloaded += chunk_len

            if downloaded >= DEEP_TEST_SIZE:
                break
            if (now - test_start) >= MIN_TEST_TIME and downloaded >= 131072:
                break

        total_time = time.time() - test_start
        if total_time <= 0 or downloaded < 16384:
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

    async def test_one(self, url, channel_name):
        """测试单个源，返回速度KB/s，失败返回0"""
        # 检查黑名单
        if self.blacklist.contains(url):
            domain = urlparse(url).netloc
            print(f"⏭️  黑名单跳过: {channel_name:<10}| {domain}")
            return 0.0

        # 预处理URL：移除#片段（已在解析阶段做过，这里再次确保）
        clean_url, _ = urldefrag(url)
        if clean_url != url:
            print(f"🔧 自动移除#片段: {channel_name:<10}| {url[:55]} → {clean_url[:55]}")
            url = clean_url

        async with self.semaphore:
            try:
                start = time.time()
                async with self.session.get(url, timeout=CHECK_TIMEOUT) as resp:
                    ttfb = time.time() - start
                    if ttfb > 2.5:
                        print(f"❌ {channel_name:<10}|{url[:85]:<90}|超时(TTFB={ttfb*1000:.0f}ms)")
                        return 0.0

                    # 判断是否为 HLS 播放列表
                    content_type = resp.headers.get('Content-Type', '')
                    body_preview = await resp.content.read(2048)
                    resp.content.unread_data(body_preview)

                    is_hls = (url.lower().endswith('.m3u8') or
                              'vnd.apple.mpegurl' in content_type or
                              'application/x-mpegURL' in content_type or
                              body_preview.lstrip()[:20].lower().find(b'#extm3u') != -1)

                    if is_hls:
                        # 解析播放列表
                        playlist_text = body_preview.decode('utf-8', errors='ignore')
                        remaining = await resp.content.read()
                        full_playlist = playlist_text + remaining.decode('utf-8', errors='ignore')

                        seg_urls = []
                        for line in full_playlist.splitlines():
                            line = line.strip()
                            if line and not line.startswith('#'):
                                seg_url = urljoin(url, line)
                                seg_url, _ = urldefrag(seg_url)  # 分片URL也可能有#
                                seg_urls.append(seg_url)
                                if len(seg_urls) >= 3:
                                    break

                        if not seg_urls:
                            print(f"❌ {channel_name:<10}|{url[:85]:<90}| m3u8无有效分片")
                            return 0.0

                        final_speed = 0.0
                        for seg_url in seg_urls:
                            try:
                                async with self.session.get(seg_url, timeout=CHECK_TIMEOUT) as seg_resp:
                                    speed, downloaded = await self._measure_stream(seg_resp, seg_url, channel_name)
                                    if speed > 0:
                                        final_speed = speed
                                        break
                                    else:
                                        print(f"⚠️  {channel_name:<10}|{seg_url[:85]:<90}|分片数据不足 ({downloaded}B)")
                            except Exception as e:
                                print(f"⚠️  {channel_name:<10}|{seg_url[:85]:<90}|分片请求失败: {str(e)[:30]}")
                                continue

                        if final_speed <= 0:
                            print(f"❌ {channel_name:<10}|{url[:85]:<90}| 所有分片测速失败")
                            return 0.0

                        self.stats['total'] += 1
                        if final_speed >= SPEED_THRESHOLD:
                            self.stats['passed'] += 1
                        else:
                            self.stats['failed'] += 1
                        self.stats['speeds'].append(final_speed)
                        self.stats['max'] = max(self.stats['max'], final_speed)
                        self.stats['min'] = min(self.stats['min'], final_speed)

                        status = '✅' if final_speed >= SPEED_THRESHOLD else '❌'
                        print(f"{status} {channel_name:<10}|{url[:85]:<90}|速度:{final_speed:>7.1f} KB/s|响应: {ttfb*1000:>5.0f} ms")
                        return final_speed

                    else:
                        # 非HLS直链
                        speed, downloaded = await self._measure_stream(resp, url, channel_name)
                        if speed <= 0:
                            print(f"❌ {channel_name:<10}|{url[:85]:<90}| 数据不足 ({downloaded}B)")
                            return 0.0

                        self.stats['total'] += 1
                        if speed >= SPEED_THRESHOLD:
                            self.stats['passed'] += 1
                        else:
                            self.stats['failed'] += 1
                        self.stats['speeds'].append(speed)
                        self.stats['max'] = max(self.stats['max'], speed)
                        self.stats['min'] = min(self.stats['min'], speed)

                        status = '✅' if speed >= SPEED_THRESHOLD else '❌'
                        print(f"{status}{channel_name:<10}|{url[:85]:<90}|速度:{speed:>7.1f} KB/s|响应:{ttfb*1000:>5.0f} ms")
                        return speed

            except asyncio.TimeoutError:
                print(f"❌ {channel_name:<10}|{url[:85]:<90}| 超时")
                return 0.0
            except Exception as e:
                print(f"❌ {channel_name:<10}|{url[:85]:<90}| 异常: {str(e)[:30]}")
                return 0.0

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

# ====================== 主流程 ======================
async def main():
    print("=" * 85)
    print("IPTV频道源测速工具 v3.2 (修复#片段问题，添加Referer)")
    print("=" * 85)

    blacklist = Blacklist('freetv/blacklist.txt')
    template = ChannelTemplate('freetv/dome.txt')
    if not template.load():
        return

    source_urls = [
        "https://iptv-org.github.io/iptv/index.m3u",
        "https://sub.ottiptv.cc/yylunbo.m3u",
        #"https://raw.githubusercontent.com/haonanren118/IPTV/refs/heads/master/iptv_sources.m3u8",
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
