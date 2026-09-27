#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 频道源测速工具 v3.5
功能：
- 支持多源（本地文件 + 网络源）
- 分辨率解析（ffprobe）并动态调整合格速度阈值
- 分片测速为主，ffmpeg 后备
- 连通性检查：非200状态码时快速测速，低于10KB/s自动加入黑名单
- 标清源（<720p）单独输出到 freetv/标清.txt
- 相同名称的源按速度降序输出
"""

import os
import sys
import json
import time
import re
import subprocess
import tempfile
import threading
from collections import defaultdict
from urllib.parse import urlparse
import requests

# ========== 配置区 ==========
# 文件路径
BASE_DIR = 'freetv'
BLACKLIST_FILE = os.path.join(BASE_DIR, 'blacklist.txt')
TEMPLATE_FILE = os.path.join(BASE_DIR, 'dome.txt')          # 本地源模板
OUTPUT_FILE_TXT = 'freetv.txt'                              # 高清/超清输出
OUTPUT_FILE_SD = os.path.join(BASE_DIR, '标清.txt')         # 标清输出
EPG_URL = 'https://epg.pw/api/epg.xml'                     # 固定 EPG 地址（仅示例）

# 网络源列表（已启用）
NETWORK_SOURCES = [
        "https://sub.ottiptv.cc/yylunbo.m3u",
        "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
        "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt",
        "https://raw.githubusercontent.com/lbxxxtw2/iptv/refs/heads/master/output/tv.txt",
        "https://raw.githubusercontent.com/qingtian6325-lang/IPTV/refs/heads/main/mytv.m3u",
]

# 超时设置（秒）
CONNECT_TIMEOUT = 3
RESOLUTION_TIMEOUT = 5
SPEED_TIMEOUT = 5          # 主测速超时
BACKUP_TIMEOUT = 8         # 后备测速超时
QUICK_CHECK_TIMEOUT = 2    # 连通性快速测速超时

# 分辨率优先级（用于排序和判定标清）
RESOLUTION_PRIORITY = {
    '8K': 100,
    '4K': 90,
    '2160p': 85,
    '1080p': 80,
    '1080i': 75,
    '720p': 70,
    '576p': 60,
    '480p': 50,
    '360p': 40,
    'unknown': 10,
}

# 各分辨率最低合格速度（MB/s）
RESOLUTION_SPEED_THRESHOLD_MB = {
    '8K': 20.0,
    '4K': 5.0,
    '2160p': 5.0,
    '1080p': 1.5,
    '1080i': 1.5,
    '720p': 0.8,
    '576p': 0.5,
    '480p': 0.35,
    '360p': 0.2,
    'unknown': 0.6,       # 默认 600 KB/s
}

# ========== 辅助函数 ==========

def ensure_dir(path):
    """确保目录存在"""
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)

def clean_url(url):
    """移除 $ 及其后面的所有字符"""
    if '$' in url:
        url = url.split('$')[0]
    return url.strip()

def extract_domain(url):
    """提取域名（含端口）"""
    return urlparse(url).netloc

def is_sd(resolution):
    """判断是否为标清（低于720p）"""
    return RESOLUTION_PRIORITY.get(resolution, 0) < 70

def parse_resolution(width, height):
    """根据宽高返回分辨率标签"""
    if not width or not height:
        return 'unknown'
    if width >= 7680 or height >= 4320:
        return '8K'
    if width >= 3840 or height >= 2160:
        return '4K'
    if width >= 2560 or height >= 1440:
        return '1440p'   # 补充
    if width >= 1920 or height >= 1080:
        return '1080p'
    if width >= 1280 or height >= 720:
        return '720p'
    if width >= 960 or height >= 540:
        return '540p'    # 补充
    if width >= 854 or height >= 480:
        return '480p'
    if width >= 640 or height >= 360:
        return '360p'
    return 'unknown'

def get_resolution_info(url, timeout=RESOLUTION_TIMEOUT):
    """使用 ffprobe 获取视频分辨率，返回 (resolution, width, height)"""
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-timeout', str(int(timeout * 1000000)),  # 微秒
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
        if result.returncode != 0:
            return 'unknown', 0, 0
        data = json.loads(result.stdout)
        streams = [s for s in data.get('streams', []) if s.get('codec_type') == 'video']
        if not streams:
            return 'unknown', 0, 0
        w = streams[0].get('width', 0)
        h = streams[0].get('height', 0)
        res = parse_resolution(w, h)
        return res, w, h
    except Exception:
        return 'unknown', 0, 0

def test_speed_by_segment(url, timeout=SPEED_TIMEOUT):
    """
    分片测速（主测速）
    通过 requests 分块下载一段数据，计算平均速度（MB/s）
    """
    try:
        chunk_size = 16384  # 16KB
        total_bytes = 0
        start = time.time()
        with requests.get(url, stream=True, timeout=timeout,
                          headers={'User-Agent': 'Mozilla/5.0'}) as resp:
            if resp.status_code != 200:
                return 0.0
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    total_bytes += len(chunk)
                    elapsed = time.time() - start
                    if elapsed >= timeout:
                        break
        elapsed = time.time() - start
        if elapsed < 0.1 or total_bytes < 10240:  # 至少10KB
            return 0.0
        speed_mb = (total_bytes / elapsed) / (1024 * 1024)
        return round(speed_mb, 2)
    except:
        return 0.0

def test_speed_ffmpeg(url, timeout=SPEED_TIMEOUT):
    """
    后备测速：使用 ffmpeg 下载一段 TS 并计算速度（MB/s）
    """
    try:
        with tempfile.NamedTemporaryFile(delete=True, suffix='.ts') as tmp:
            cmd = [
                'ffmpeg', '-y',
                '-timeout', str(int(timeout * 1000000)),
                '-user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '-i', url,
                '-t', str(timeout),
                '-c', 'copy',
                '-f', 'mpegts',
                tmp.name
            ]
            start = time.time()
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                proc.communicate(timeout=timeout+2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            elapsed = time.time() - start
            size = os.path.getsize(tmp.name)
            if size > 10240 and elapsed > 0.1:
                speed_mb = (size / elapsed) / (1024 * 1024)
                return round(speed_mb, 2)
    except:
        pass
    return 0.0

def check_connectivity(url):
    """
    连通性检查
    返回 True 表示可通过，False 表示不可用且可能加入黑名单
    """
    try:
        r = requests.head(url, timeout=CONNECT_TIMEOUT,
                          headers={'User-Agent': 'Mozilla/5.0'})
        status = r.status_code
    except:
        status = 0

    if status == 200:
        return True

    # 非200 → 快速测速判断
    speed = test_speed_ffmpeg(url, timeout=QUICK_CHECK_TIMEOUT)
    if speed >= 0.01:  # 10 KB/s
        return True
    else:
        # 加入黑名单
        domain = extract_domain(url)
        append_to_blacklist(domain)
        return False

def load_blacklist():
    """加载已有黑名单域名集合"""
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    blacklist.add(line)
    return blacklist

def append_to_blacklist(domain):
    """追加一个域名到黑名单（去重由外部保证）"""
    ensure_dir(BLACKLIST_FILE)
    with open(BLACKLIST_FILE, 'a', encoding='utf-8') as f:
        f.write(domain + '\n')

def is_blacklisted(url, blacklist_set):
    """检查url的域名是否在黑名单中"""
    domain = extract_domain(url)
    return domain in blacklist_set

# ========== 源加载 ==========

def load_local_sources(filepath):
    """从本地 m3u/txt 文件加载频道列表，返回 [(name, url), ...]"""
    sources = []
    if not os.path.exists(filepath):
        return sources
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF:'):
            # 提取频道名
            name_match = re.search(r'tvg-name="([^"]*)"', line)
            if name_match:
                name = name_match.group(1)
            else:
                # 取逗号后的部分
                name = line.split(',')[-1].strip() if ',' in line else 'Unknown'
            i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith('#'):
                    sources.append((name, url))
        i += 1
    return sources

def load_network_sources(url_list):
    """从网络 m3u 地址加载频道列表"""
    sources = []
    for url in url_list:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                content = resp.text
                lines = content.splitlines()
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    if line.startswith('#EXTINF:'):
                        name_match = re.search(r'tvg-name="([^"]*)"', line)
                        if name_match:
                            name = name_match.group(1)
                        else:
                            name = line.split(',')[-1].strip() if ',' in line else 'Unknown'
                        i += 1
                        if i < len(lines):
                            url_line = lines[i].strip()
                            if url_line and not url_line.startswith('#'):
                                sources.append((name, url_line))
                    i += 1
        except:
            continue
    return sources

# ========== 主处理 ==========

def main():
    print("=== IPTV 频道源测速工具 v3.5 ===")
    ensure_dir(BASE_DIR)
    blacklist = load_blacklist()

    # 1. 加载所有源
    all_sources = []
    # 本地模板
    if os.path.exists(TEMPLATE_FILE):
        local = load_local_sources(TEMPLATE_FILE)
        print(f"本地模板加载 {len(local)} 个频道")
        all_sources.extend(local)
    # 网络源
    net = load_network_sources(NETWORK_SOURCES)
    print(f"网络源加载 {len(net)} 个频道")
    all_sources.extend(net)

    if not all_sources:
        print("未找到任何频道源，请检查配置文件。")
        return

    # 2. 预处理：去重、清洗URL、过滤黑名单
    seen = set()
    processed = []
    for name, url in all_sources:
        url_clean = clean_url(url)
        key = (name, url_clean)
        if key in seen:
            continue
        seen.add(key)
        if is_blacklisted(url_clean, blacklist):
            print(f"[黑名单跳过] {name}: {extract_domain(url_clean)}")
            continue
        processed.append((name, url_clean))

    print(f"待测频道数: {len(processed)}")

    # 3. 逐频道测速
    results_hd = []   # 高清/超清 (name, url, speed, resolution)
    results_sd = []   # 标清

    for idx, (name, url) in enumerate(processed, 1):
        print(f"\n[{idx}/{len(processed)}] 正在处理: {name}")
        print(f"    URL: {url[:80]}...")

        # 3.1 连通性检查（含黑名单更新）
        if not check_connectivity(url):
            print("    → 连通性不合格，已跳过")
            continue

        # 3.2 获取分辨率
        resolution, w, h = get_resolution_info(url)
        print(f"    分辨率: {resolution} ({w}x{h})")

        # 3.3 确定阈值
        threshold = RESOLUTION_SPEED_THRESHOLD_MB.get(resolution, 0.6)

        # 3.4 主测速（分片）
        speed = test_speed_by_segment(url, timeout=SPEED_TIMEOUT)
        if speed <= 0:
            # 后备测速
            speed = test_speed_ffmpeg(url, timeout=BACKUP_TIMEOUT)

        if speed <= 0:
            print(f"    测速失败，速度={speed:.2f} MB/s")
            continue

        print(f"    速度: {speed:.2f} MB/s (阈值: {threshold} MB/s)")

        # 3.5 判断是否达标
        if speed < threshold:
            print(f"    低于阈值，丢弃")
            continue

        # 3.6 分类存储
        if is_sd(resolution):
            results_sd.append((name, url, speed, resolution))
        else:
            results_hd.append((name, url, speed, resolution))

    # 4. 排序输出（同一名称按速度降序）
    def sort_key(item):
        # 先按名称字母升序，再按速度降序（负值）
        return (item[0], -item[2])

    results_hd.sort(key=sort_key)
    results_sd.sort(key=sort_key)

    # 5. 写入文件
    # 高清/超清
    with open(OUTPUT_FILE_TXT, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for name, url, speed, res in results_hd:
            f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-logo="" group-title="HD",{name}\n')
            f.write(f'{url}\n')
    print(f"\n高清/超清输出: {OUTPUT_FILE_TXT} ({len(results_hd)} 条)")

    # 标清
    ensure_dir(OUTPUT_FILE_SD)
    with open(OUTPUT_FILE_SD, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for name, url, speed, res in results_sd:
            f.write(f'#EXTINF:-1 tvg-name="{name}" tvg-logo="" group-title="SD",{name}\n')
            f.write(f'{url}\n')
    print(f"标清输出: {OUTPUT_FILE_SD} ({len(results_sd)} 条)")

    print("\n=== 测速完成 ===")

if __name__ == '__main__':
    main()
