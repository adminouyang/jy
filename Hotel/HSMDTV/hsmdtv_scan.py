#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HSMDTV 直播源扫描、测速、合并脚本 v3
====================================
功能：
  1. 读取 hsmd_ip.txt 中的 IP:PORT$运营商 列表
  2. 对每个IP的D段(1-256)进行并发扫描探测
  3. 将可用的IP按运营商分别保存到 ip/运营商.txt
  4. 自动发现所有 *_list.txt 频道模板文件
  5. 每个运营商的频道模板用自己的IP进行替换 + 可用性验证 + 测速
  6. 合并输出最终的 m3u8 + txt 文件

设计原则：
  - 路径全部可配置（环境变量优先），找不到文件时给出明确错误
  - 在 GitHub Actions / Docker / 本地 均可运行
  - 任何一步失败都不应导致空输出被提交
"""

import os
import sys
import time
import json
import glob
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ==================== 配置（环境变量可覆盖）====================
EPG_URL = os.environ.get("EPG_URL", "https://epg.112114.xyz/pp.xml")
LOGO_BASE_URL = os.environ.get(
    "LOGO_BASE_URL",
    "https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/"
)

# 脚本所在目录 & 工作目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.environ.get("WORK_DIR", SCRIPT_DIR)  # GitHub Actions 下为 Hotel/HSMDTV

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "20"))
HOST_SPEED_TEST_TIMEOUT = int(os.environ.get("HOST_SPEED_TEST_TIMEOUT", "15"))
SPEED_TEST_BATCH_SIZE = int(os.environ.get("SPEED_TEST_BATCH_SIZE", "60"))
HSMDTV_TEST_URI = os.environ.get("HSMDTV_TEST_URI", "/newlive/live/hls/1/live.m3u8")

# ---- 路径配置（环境变量优先，否则按 WORK_DIR 推导）----
IP_LIST_FILE = os.environ.get("IP_LIST_FILE") or os.path.join(WORK_DIR, "ip", "hsmd_ip.txt")
IP_OUTPUT_DIR = os.environ.get("IP_OUTPUT_DIR") or os.path.join(WORK_DIR, "ip")

OUTPUT_M3U8 = os.environ.get("OUTPUT_M3U8") or os.path.join(WORK_DIR, "hsmd.m3u8")
OUTPUT_TXT = os.environ.get("OUTPUT_TXT") or os.path.join(WORK_DIR, "hsmd.txt")

LOG_FILE = os.environ.get("LOG_FILE") or os.path.join(WORK_DIR, "logs", "cron.log")

# ==================== 日志配置 ====================
os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else WORK_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# 启动信息
logger.info("="*60)
logger.info("🚀 HSMDTV 直播源扫描器 v3")
logger.info(f"   WORK_DIR     = {WORK_DIR}")
logger.info(f"   IP_LIST_FILE = {IP_LIST_FILE}")
logger.info(f"   OUTPUT_M3U8  = {OUTPUT_M3U8}")
logger.info(f"   OUTPUT_TXT   = {OUTPUT_TXT}")
logger.info(f"   LOG_FILE     = {LOG_FILE}")
logger.info("="*60)

# ==================== 频道分类 ====================
CHANNEL_CATEGORIES = {
    "央视频道": [
        "CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV4欧洲", "CCTV4美洲", "CCTV5", "CCTV5+", "CCTV6", "CCTV7",
        "CCTV8", "CCTV9", "CCTV10", "CCTV11", "CCTV12", "CCTV13", "CCTV14", "CCTV15", "CCTV16", "CCTV17",
        "兵器科技", "风云音乐", "风云足球", "风云剧场", "怀旧剧场", "第一剧场", "女性时尚", "世界地理", "央视台球", "高尔夫网球",
        "央视文化精品", "卫生健康", "电视指南", "老故事", "中学生", "发现之旅", "书法频道", "国学频道", "环球奇观", "CCTV4K",
        "CETV1", "CETV2", "CETV3", "CETV4", "早期教育", "CGTN", "CGTN纪录", "CGTN俄语", "CGTN英语",
    ],
    "卫视频道": [
        "重温经典", "湖南卫视", "浙江卫视", "江苏卫视", "东方卫视", "深圳卫视", "北京卫视", "广东卫视", "广西卫视", "东南卫视",
        "海峡卫视", "海南卫视", "河北卫视", "河南卫视", "湖北卫视", "江西卫视", "四川卫视", "重庆卫视", "贵州卫视", "云南卫视",
        "天津卫视", "安徽卫视", "厦门卫视", "山东卫视", "山东教育卫视", "辽宁卫视", "黑龙江卫视", "吉林卫视", "内蒙古卫视",
        "宁夏卫视", "山西卫视", "陕西卫视", "甘肃卫视", "青海卫视", "新疆卫视", "西藏卫视", "三沙卫视", "兵团卫视", "延边卫视",
        "安多卫视", "康巴卫视", "农林卫视", "大湾区卫视",
    ],  
    "电影频道": ["CHC影迷电影","CHC动作电影","BesTV电影","江苏影视","峨眉电影","华数电影"],
    "江苏频道": ["江苏城市","江苏综艺","江苏体育休闲","南京科教"],
    "四川频道": ["四川乡村","四川文化旅游","四川宣传片"],
    "湖北频道": ["湖北经视"],
    "福建频道": ["福建综合"],
    "少儿频道": ["金鹰卡通","动漫秀场"],
    "其他频道": [],
}

# ==================== 工具函数 ====================
def _get_remaining_timeout(deadline, default_timeout=10):
    if deadline is None:
        return default_timeout
    remaining = deadline - time.time()
    return max(1, min(remaining, default_timeout))

def get_ts_url(m3u8_url, deadline=None):
    """解析 m3u8 获取第一个 TS 分片 URL"""
    try:
        request_timeout = _get_remaining_timeout(deadline, 5)
        if request_timeout <= 0:
            return None
        response = requests.get(m3u8_url, timeout=request_timeout)
        if response.status_code != 200:
            return None
        for line in response.text.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                if line.startswith("http"):
                    return line
                elif line.startswith("/"):
                    base = m3u8_url.split("/")[0] + "//" + m3u8_url.split("/")[2]
                    return base + line
                else:
                    return m3u8_url.rsplit("/", 1)[0] + "/" + line
        return None
    except Exception:
        return None

def get_download_speed(url, deadline=None):
    """测量下载速度 (MB/s)，失败返回 -1"""
    try:
        request_timeout = _get_remaining_timeout(deadline, 10)
        if request_timeout <= 0:
            return -1
        start_time = time.time()
        with requests.get(url, stream=True, timeout=request_timeout) as r:
            r.raise_for_status()
            size = 0
            chunk_size = 8192
            limit_size = 10 * 1024 * 1024
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    size += len(chunk)
                if size > limit_size:
                    break
                if time.time() - start_time > 8:
                    break
                if deadline is not None and time.time() > deadline:
                    break
        duration = time.time() - start_time
        if duration == 0:
            duration = 0.001
        return (size / 1024 / 1024) / duration
    except Exception:
        return -1

def test_url_availability(url, timeout=5):
    """测试URL是否可访问"""
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        ok = resp.status_code == 200
        resp.close()
        return ok
    except Exception:
        return False

# ==================== IP 扫描模块 ====================
def parse_ip_file(ip_file):
    """解析IP文件 → [{"ip","port","isp"}, ...]"""
    entries = []
    if not os.path.exists(ip_file):
        logger.error(f"❌ IP文件不存在: {ip_file}")
        logger.error(f"   请确认文件已上传到仓库，或设置环境变量 IP_LIST_FILE 指向正确路径")
        return entries

    with open(ip_file, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                if "$" in line:
                    addr_part, isp = line.split("$", 1)
                else:
                    addr_part = line
                    isp = "默认"
                addr_part = addr_part.strip()
                isp = isp.strip()
                if ":" in addr_part:
                    ip, port = addr_part.split(":", 1)
                    port = int(port)
                else:
                    ip = addr_part
                    port = 80
                entries.append({"ip": ip, "port": port, "isp": isp})
            except Exception as e:
                logger.warning(f"  解析第{lineno}行失败: {line} -> {e}")
    logger.info(f"✓ 从 {ip_file} 解析到 {len(entries)} 条IP记录")
    return entries

def scan_d_segment(base_ip, port, isp, timeout=3):
    """扫描D段(1-256)，返回可用IP列表"""
    available = []
    base = base_ip.rstrip(".")

    def check_one(d):
        ip = f"{base}.{d}"
        url = f"http://{ip}:{port}{HSMDTV_TEST_URI}"
        if test_url_availability(url, timeout=timeout):
            return ip
        return None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_one, d): d for d in range(1, 257)}
        for future in as_completed(futures):
            result = future.result()
            if result:
                available.append(result)
                logger.info(f"  [✓] {result}:{port} ({isp})")
    return available

def scan_all_ips(ip_entries):
    """对所有IP条目D段扫描，按运营商保存"""
    results = {}
    for entry in ip_entries:
        base_ip = ".".join(entry["ip"].split(".")[:3])
        isp = entry["isp"]
        port = entry["port"]

        logger.info(f"\n{'='*60}")
        logger.info(f"🔍 扫描: {isp} | 网段: {base_ip}.* | 端口: {port}")
        logger.info(f"{'='*60}")

        available_ips = scan_d_segment(base_ip, port, isp)
        os.makedirs(IP_OUTPUT_DIR, exist_ok=True)
        out_file = os.path.join(IP_OUTPUT_DIR, f"{isp}.txt")
        with open(out_file, "w", encoding="utf-8") as f:
            for ip in available_ips:
                f.write(f"{ip}:{port}${isp}\n")
        logger.info(f"[{isp}] 扫描完成，可用IP: {len(available_ips)} 个 → {out_file}")
        results[isp] = [{"ip": ip, "port": port} for ip in available_ips]
    return results

# ==================== 测速模块 ====================
def speed_test_ip(ip, port, isp, timeout=HOST_SPEED_TEST_TIMEOUT):
    test_url = f"http://{ip}:{port}{HSMDTV_TEST_URI}"
    deadline = time.time() + timeout
    ts_url = get_ts_url(test_url, deadline=deadline)
    if ts_url is None:
        return {"ip": ip, "port": port, "speed": -1, "ts_url": None}
    speed = get_download_speed(ts_url, deadline=deadline)
    return {"ip": ip, "port": port, "speed": speed, "ts_url": ts_url}

def batch_speed_test(isp_ip_list, isp_name):
    results = []
    logger.info(f"[{isp_name}] 开始测速，共 {len(isp_ip_list)} 个IP")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(speed_test_ip, item["ip"], item["port"], isp_name): item
            for item in isp_ip_list
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            r = future.result()
            if r["speed"] > 0:
                results.append(r)
                logger.info(f"  [{done}/{len(isp_ip_list)}] {r['ip']}:{r['port']} -> {r['speed']:.2f} MB/s")
    results.sort(key=lambda x: x["speed"], reverse=True)
    logger.info(f"[{isp_name}] 测速完成，可用: {len(results)} 个"
                + (f"，最快: {results[0]['ip']}:{results[0]['port']} @ {results[0]['speed']:.2f} MB/s" if results else ""))
    return results

# ==================== 频道模板模块 ====================
def discover_channel_list_files(base_dir):
    """发现所有 *_list.txt 文件"""
    pattern = os.path.join(base_dir, "*_list.txt")
    files = sorted(glob.glob(pattern))
    result = []
    for f in files:
        basename = os.path.basename(f)
        isp = basename.replace("_list.txt", "").strip()
        result.append({"isp": isp, "file": f})
        logger.info(f"📄 发现频道列表: {basename} (运营商: {isp})")
    logger.info(f"共发现 {len(result)} 个频道列表文件")
    return result

def load_channel_template(template_file):
    channels = []
    if not os.path.exists(template_file):
        logger.error(f"❌ 频道模板不存在: {template_file}")
        return channels
    with open(template_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                name, url = line.split(",", 1)
                channels.append({"name": name.strip(), "url_template": url.strip()})
    logger.info(f"  ✓ 读取到 {len(channels)} 个频道: {template_file}")
    return channels

def replace_ip_in_url(url_template, ip, port):
    return url_template.replace("ip:prot", f"{ip}:{port}")

def get_channel_category(channel_name):
    for cat, channels in CHANNEL_CATEGORIES.items():
        if channel_name in channels:
            return cat
    return "其他频道"

def get_channel_logo(channel_name):
    logo_map = {
        "CCTV1": "cctv1", "CCTV2": "cctv2", "CCTV3": "cctv3", "CCTV4": "cctv4",
        "CCTV5": "cctv5", "CCTV5+": "cctv5plus", "CCTV6": "cctv6", "CCTV7": "cctv7",
        "CCTV8": "cctv8", "CCTV9": "cctv9", "CCTV10": "cctv10", "CCTV11": "cctv11",
        "CCTV12": "cctv12", "CCTV13": "cctv13", "CCTV14": "cctv14", "CCTV15": "cctv15",
        "CCTV16": "cctv16", "CCTV17": "cctv17",
        "湖南卫视": "hunan", "浙江卫视": "zhejiang", "江苏卫视": "jiangsu",
        "东方卫视": "dongfang", "深圳卫视": "shenzhen", "北京卫视": "beijing",
        "广东卫视": "guangdong", "湖北卫视": "hubei", "四川卫视": "sichuan",
    }
    logo_key = logo_map.get(channel_name, channel_name)
    return f"{LOGO_BASE_URL}{logo_key}.png"

# ==================== 频道验证 ====================
def verify_channel_with_isp_ips(channel, isp_ip_results):
    """用指定ISP的IP列表验证单个频道"""
    ch_name = channel["name"]
    url_tpl = channel["url_template"]
    verified = []
    for info in isp_ip_results:
        url = replace_ip_in_url(url_tpl, info["ip"], info["port"])
        if test_url_availability(url, timeout=5):
            verified.append({
                "name": ch_name, "url": url,
                "ip": info["ip"], "port": info["port"],
                "isp": info["isp"], "speed": info.get("speed", -1),
            })
    return verified

def verify_all_channels_multi_isp(channel_lists, isp_speed_map):
    """对所有运营商的频道模板进行验证"""
    all_verified = []
    channel_best = {}

    for cl in channel_lists:
        isp = cl["isp"]
        channels = cl["channels"]
        if isp not in isp_speed_map or not isp_speed_map[isp]:
            logger.warning(f"[{isp}] 没有可用IP，跳过")
            continue

        top_ips = [{"ip": r["ip"], "port": r["port"], "isp": isp, "speed": r["speed"]}
                   for r in isp_speed_map[isp][:10]]

        logger.info(f"\n--- 验证 [{isp}] {len(channels)} 个频道 (可用IP: {len(top_ips)}) ---")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(verify_channel_with_isp_ips, ch, top_ips): ch for ch in channels}
            done = 0
            for future in as_completed(futures):
                done += 1
                ch = futures[future]
                results = future.result()
                if results:
                    all_verified.extend(results)
                    best = max(results, key=lambda x: x["speed"])
                    if ch["name"] not in channel_best or best["speed"] > channel_best[ch["name"]]["speed"]:
                        channel_best[ch["name"]] = best
                    logger.info(f"  [{done}/{len(channels)}] ✓ {ch['name']} -> {len(results)}个可用, 最佳: {best['ip']} ({best['speed']:.2f}MB/s)")
                else:
                    logger.warning(f"  [{done}/{len(channels)}] ✗ {ch['name']} -> 全部IP不可用")
    return all_verified, channel_best

# ==================== 输出模块 ====================

def _build_channel_lookup(all_verified):
    """
    将 all_verified 列表按频道名分组，保持首次出现顺序。
    返回: OrderedDict {ch_name: [links...]}
    """
    ch_groups = OrderedDict()
    for item in all_verified:
        ch_groups.setdefault(item["name"], []).append(item)
    return ch_groups


def _categorize_channels(channel_names):
    """
    将所有频道名按 CHANNEL_CATEGORIES 的顺序归类。
    返回: {分类名: [频道名,...], ...} 仅包含有频道的分类，
          "其他频道" 放在最后。
    每个分类内的频道顺序也遵循 CHANNEL_CATEGORIES 中的定义顺序。
    """
    # 建立 "频道名 -> 所属分类" 的映射（O(1) 查找）
    name_to_cat = {}
    for cat, chs in CHANNEL_CATEGORIES.items():
        for ch in chs:
            name_to_cat[ch] = cat

    # 按 CHANNEL_CATEGORIES 中定义的顺序初始化分类
    categorized = OrderedDict()
    for cat in CHANNEL_CATEGORIES:
        categorized[cat] = []

    # 第一遍：按 channel_names 的首次出现顺序归位（保持 ISP 验证优先级）
    seen = set()
    for ch_name in channel_names:
        cat = name_to_cat.get(ch_name, "其他频道")
        categorized.setdefault(cat, [])
        if ch_name not in seen:
            categorized[cat].append(ch_name)
            seen.add(ch_name)

    # 第二遍：对每个分类内的频道，按 CHANNEL_CATEGORIES 中的定义顺序重排
    # 这样 CCTV1 一定在 CCTV2 前面，不受验证顺序影响
    cat_order = {}  # cat -> {ch_name: index}
    for cat, chs in CHANNEL_CATEGORIES.items():
        cat_order[cat] = {ch: i for i, ch in enumerate(chs)}

    for cat, ch_names in categorized.items():
        if cat in cat_order:
            ch_names.sort(key=lambda c: cat_order[cat].get(c, 9999))
        # "其他频道" 中的频道按字母排序
        else:
            ch_names.sort()

    # 去掉空分类
    return OrderedDict((k, v) for k, v in categorized.items() if v)


def generate_final_outputs(all_verified, channel_best):
    """生成 m3u8 + txt + 按ISP分组m3u8 + JSON报告"""
    if not all_verified and not channel_best:
        logger.error("❌ 没有可用的频道链接，跳过输出")
        return False

    logger.info(f"\n{'='*60}")
    logger.info(f"📝 生成输出文件 (验证链接: {len(all_verified)}, 最佳频道: {len(channel_best)})")

    # 按频道名分组的 OrderedDict（保持验证顺序）
    ch_groups = _build_channel_lookup(all_verified)

    # 按分类归位（保持 CHANNEL_CATEGORIES 中的顺序）
    categorized = _categorize_channels(list(ch_groups.keys()))

    # ---- 完整 m3u8（按分类分组输出）----
    with open(OUTPUT_M3U8, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')
        for cat, ch_names in categorized.items():
            if not ch_names:
                continue
            f.write(f"\n# ===== {cat} ({len(ch_names)} 个频道) =====\n")
            for ch_name in ch_names:
                logo = get_channel_logo(ch_name)
                for link in ch_groups[ch_name]:
                    f.write(f'#EXTINF:-1 tvg-id="{ch_name}" tvg-name="{ch_name}" tvg-logo="{logo}" group-title="{cat}",{ch_name}\n')
                    f.write(link["url"] + "\n")
    logger.info(f"  ✓ {OUTPUT_M3U8}")

    # ---- 简洁 txt（按分类分组 + 分类标题行）----
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
        for cat, ch_names in categorized.items():
            if not ch_names:
                continue
            f.write(f"{cat},#genre#\n")
            for ch_name in ch_names:
                if ch_name in channel_best:
                    link = channel_best[ch_name]
                    f.write(f"{ch_name},{link['url']}\n")
            f.write("\n")  # 分类之间空行
    logger.info(f"  ✓ {OUTPUT_TXT}")

    # ---- 按ISP分组 m3u8 ----
    isp_m3u8 = os.path.join(WORK_DIR, "hsmd_by_isp.m3u8")
    with open(isp_m3u8, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')
        isp_groups = {}
        for item in all_verified:
            isp_groups.setdefault(item.get("isp", "未知"), []).append(item)
        for isp, links in sorted(isp_groups.items()):
            f.write(f"\n# ===== {isp} ({len(links)} 条) =====\n")
            # ISP 分组内部也按分类排序
            isp_ch_groups = OrderedDict()
            for item in links:
                isp_ch_groups.setdefault(item["name"], []).append(item)
            isp_categorized = _categorize_channels(list(isp_ch_groups.keys()))
            for cat, ch_names in isp_categorized.items():
                if not ch_names:
                    continue
                f.write(f"# --- {cat} ---\n")
                for ch_name in ch_names:
                    logo = get_channel_logo(ch_name)
                    for link in isp_ch_groups[ch_name]:
                        f.write(f'#EXTINF:-1 tvg-id="{ch_name}" tvg-name="{ch_name}" tvg-logo="{logo}" group-title="{cat}",{ch_name}\n')
                        f.write(link["url"] + "\n")
    logger.info(f"  ✓ {isp_m3u8}")

    # ---- JSON 报告（按分类分组）----
    report = {
        "scan_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_verified_links": len(all_verified),
        "total_unique_channels": len(channel_best),
        "channels_by_category": {},   # 新增：按分类展示
        "channels_by_isp": {},
        "best_channels": {},
    }
    # 按分类
    for cat, ch_names in categorized.items():
        if ch_names:
            report["channels_by_category"][cat] = ch_names
    # 按ISP
    for item in all_verified:
        isp = item.get("isp", "未知")
        report["channels_by_isp"].setdefault(isp, set()).add(item["name"])
    for isp, s in report["channels_by_isp"].items():
        report["channels_by_isp"][isp] = sorted(list(s))
    # 最佳链接
    for ch, link in channel_best.items():
        report["best_channels"][ch] = {
            "url": link["url"], "ip": link["ip"],
            "port": link["port"], "isp": link["isp"],
            "category": get_channel_category(ch),
            "speed_MB_s": round(link["speed"], 2),
        }
    report_file = os.path.join(WORK_DIR, "scan_report.json")
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"  ✓ {report_file}")

    # ---- 汇总 ----
    logger.info("\n" + "="*60)
    logger.info("📊 汇总报告")
    logger.info(f"  验证通过链接: {report['total_verified_links']}")
    logger.info(f"  唯一频道数:   {report['total_unique_channels']}")
    logger.info(f"\n  按运营商:")
    for isp, chs in report["channels_by_isp"].items():
        logger.info(f"    [{isp}] {len(chs)} 个频道")
    logger.info(f"\n  按分类:")
    for cat, chs in report.get("channels_by_category", {}).items():
        logger.info(f"    [{cat}] {len(chs)} 个频道")
    logger.info("="*60)
    return True

# ==================== 主流程 ====================
def main():
    start = time.time()
    success = True

    # ---- Step 1: 读取IP列表 ----
    logger.info("\n>>> Step 1: 读取IP列表")
    if not os.path.exists(IP_LIST_FILE):
        logger.error(f"❌ IP文件不存在: {IP_LIST_FILE}")
        logger.error("   请检查仓库中是否存在该文件，或设置环境变量 IP_LIST_FILE")
        sys.exit(1)

    ip_entries = parse_ip_file(IP_LIST_FILE)
    if not ip_entries:
        logger.error("❌ 没有读取到任何IP记录")
        sys.exit(1)

    isp_count = {}
    for e in ip_entries:
        isp_count[e["isp"]] = isp_count.get(e["isp"], 0) + 1
    for isp, cnt in isp_count.items():
        logger.info(f"  {isp}: {cnt} 条")

    # ---- Step 2: D段扫描 ----
    logger.info("\n>>> Step 2: D段扫描 (1-256)")
    scan_results = scan_all_ips(ip_entries)
    available = {k: v for k, v in scan_results.items() if v}
    if not available:
        logger.error("❌ 扫描后没有任何可用IP")
        sys.exit(1)

    # ---- Step 3: 测速 ----
    logger.info("\n>>> Step 3: 批量测速")
    isp_speed_map = {isp: batch_speed_test(ips, isp) for isp, ips in available.items()}

    # ---- Step 4: 发现频道列表 ----
    logger.info(f"\n>>> Step 4: 发现频道列表 (*_list.txt)")
    list_files = discover_channel_list_files(WORK_DIR)
    if not list_files:
        logger.error(f"❌ 在 {WORK_DIR} 下没有找到任何 *_list.txt 文件")
        sys.exit(1)

    channel_lists = []
    for lf in list_files:
        chs = load_channel_template(lf["file"])
        if chs:
            channel_lists.append({"isp": lf["isp"], "file": lf["file"], "channels": chs})

    if not channel_lists:
        logger.error("❌ 没有读取到任何频道模板")
        sys.exit(1)

    total_ch = sum(len(c["channels"]) for c in channel_lists)
    logger.info(f"共 {len(channel_lists)} 个运营商, {total_ch} 个频道模板")

    # ---- Step 5: 验证频道链接 ----
    logger.info("\n>>> Step 5: 验证频道链接")
    all_verified, channel_best = verify_all_channels_multi_isp(channel_lists, isp_speed_map)

    # ---- Step 6: 生成输出 ----
    logger.info("\n>>> Step 6: 生成输出文件")
    ok = generate_final_outputs(all_verified, channel_best)
    if not ok:
        sys.exit(1)

    elapsed = time.time() - start
    logger.info(f"\n🎉 全部完成！耗时: {elapsed:.1f}s")
    # 退出码：有输出则0，无输出则1（让GitHub Actions知道是否该提交）
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
