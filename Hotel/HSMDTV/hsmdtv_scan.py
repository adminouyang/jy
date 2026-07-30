#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HSMDTV 直播源扫描、测速、合并脚本 v2
====================================
功能：
  1. 读取 /Hotel/HSMDTV/ip/hsmd_ip.txt 中的IP:PORT$运营商列表
  2. 对每个IP的D段(1-256)进行并发扫描探测
  3. 将可用的IP按运营商分别保存到 /Hotel/HSMDTV/ip/运营商.txt
  4. 读取 /Hotel/HSMDTV/ 下所有 *_list.txt 频道模板文件
  5. 每个运营商的频道模板用自己的IP进行替换
  6. 对所有频道链接进行可用性验证 + 测速
  7. 合并输出最终的 m3u8 + txt 文件
"""

import os
import sys
import time
import json
import glob
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ==================== 配置 ====================
EPG_URL = os.environ.get("EPG_URL", "https://epg.112114.xyz/pp.xml")
LOGO_BASE_URL = "https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_M3U8 = os.environ.get("OUTPUT_M3U8", os.path.join(SCRIPT_DIR, "hsmd.m3u8"))
OUTPUT_TXT = os.environ.get("OUTPUT_TXT", os.path.join(SCRIPT_DIR, "hsmd.txt"))

MAX_WORKERS = 20
HOST_SPEED_TEST_TIMEOUT = 15
SPEED_TEST_BATCH_SIZE = 60
HSMDTV_TEST_URI = "/newlive/live/hls/1/live.m3u8"

# 路径配置
BASE_DIR = "/Hotel/HSMDTV"
IP_LIST_FILE = os.path.join(BASE_DIR, "ip", "hsmd_ip.txt")
IP_OUTPUT_DIR = os.path.join(BASE_DIR, "ip")

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
    "其他频道": []
}

# ==================== 日志配置 ====================
LOG_FILE = os.path.join(SCRIPT_DIR, "hsmdtv_scan.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==================== 工具函数 ====================

def _get_remaining_timeout(deadline, default_timeout=10):
    """计算剩余超时时间"""
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
        for line in response.text.strip().split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                if line.startswith('http'):
                    return line
                elif line.startswith('/'):
                    base = m3u8_url.split('/')[0] + "//" + m3u8_url.split('/')[2]
                    return base + line
                else:
                    return m3u8_url.rsplit('/', 1)[0] + "/" + line
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
    """测试URL是否可访问，返回True/False"""
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        ok = resp.status_code == 200
        resp.close()
        return ok
    except Exception:
        return False

# ==================== IP扫描模块 ====================

def parse_ip_file(ip_file):
    """
    解析IP文件，返回列表：
    [{"ip": "113.57.140.161", "port": 10081, "isp": "湖北联通"}, ...]
    """
    entries = []
    if not os.path.exists(ip_file):
        logger.error(f"IP文件不存在: {ip_file}")
        return entries

    with open(ip_file, "r", encoding="utf-8") as f:
        for line in f:
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
                logger.warning(f"解析行失败: {line} -> {e}")
    logger.info(f"从 {ip_file} 解析到 {len(entries)} 条IP记录")
    return entries

def scan_d_segment(base_ip, port, isp, timeout=3):
    """
    扫描某IP的D段 (1-256)，返回可用IP列表
    base_ip格式: 113.57.140 (不含最后一段)
    """
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
    """
    对所有IP条目进行D段扫描
    返回: {isp: [{"ip": ip, "port": port}, ...]}
    """
    results = {}
    for entry in ip_entries:
        base_ip = ".".join(entry["ip"].split(".")[:3])
        isp = entry["isp"]
        port = entry["port"]

        logger.info(f"\n{'='*60}")
        logger.info(f"开始扫描: {isp} | 网段: {base_ip}.* | 端口: {port}")
        logger.info(f"{'='*60}")

        available_ips = scan_d_segment(base_ip, port, isp)

        # 保存到运营商文件
        os.makedirs(IP_OUTPUT_DIR, exist_ok=True)
        out_file = os.path.join(IP_OUTPUT_DIR, f"{isp}.txt")
        with open(out_file, "w", encoding="utf-8") as f:
            for ip in available_ips:
                f.write(f"{ip}:{port}${isp}\n")
        logger.info(f"[{isp}] 扫描完成，可用IP: {len(available_ips)} 个，已保存到 {out_file}")

        results[isp] = [{"ip": ip, "port": port} for ip in available_ips]

    return results

# ==================== 测速模块 ====================

def speed_test_ip(ip, port, isp, timeout=HOST_SPEED_TEST_TIMEOUT):
    """对单个IP进行测速，返回dict"""
    test_url = f"http://{ip}:{port}{HSMDTV_TEST_URI}"
    deadline = time.time() + timeout

    ts_url = get_ts_url(test_url, deadline=deadline)
    if ts_url is None:
        return {"ip": ip, "port": port, "speed": -1, "ts_url": None}
    speed = get_download_speed(ts_url, deadline=deadline)
    return {"ip": ip, "port": port, "speed": speed, "ts_url": ts_url}

def batch_speed_test(isp_ip_list, isp_name):
    """批量测速，按速度降序返回"""
    results = []
    logger.info(f"[{isp_name}] 开始测速，共 {len(isp_ip_list)} 个IP")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(speed_test_ip, item["ip"], item["port"], isp_name): item
            for item in isp_ip_list
        }
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            result = future.result()
            if result["speed"] > 0:
                results.append(result)
                logger.info(f"  [{done_count}/{len(isp_ip_list)}] {result['ip']}:{result['port']} -> {result['speed']:.2f} MB/s")
            else:
                logger.debug(f"  [{done_count}/{len(isp_ip_list)}] {result['ip']}:{result['port']} -> 失败")

    results.sort(key=lambda x: x["speed"], reverse=True)
    logger.info(f"[{isp_name}] 测速完成，可用: {len(results)} 个")
    if results:
        logger.info(f"[{isp_name}] 最快: {results[0]['ip']}:{results[0]['port']} @ {results[0]['speed']:.2f} MB/s")
    return results

# ==================== 频道模板模块 ====================

def discover_channel_list_files(base_dir):
    """
    发现 /Hotel/HSMDTV/ 下所有 *_list.txt 文件
    返回: [{"isp": "湖北联通", "file": "/Hotel/HSMDTV/湖北联通_list.txt"}, ...]
    """
    pattern = os.path.join(base_dir, "*_list.txt")
    files = glob.glob(pattern)
    files.sort()

    result = []
    for f in files:
        basename = os.path.basename(f)
        # "湖北联通_list.txt" -> "湖北联通"
        isp = basename.replace("_list.txt", "").strip()
        result.append({"isp": isp, "file": f})
        logger.info(f"发现频道列表: {basename} (运营商: {isp})")

    logger.info(f"共发现 {len(result)} 个频道列表文件")
    return result

def load_channel_template(template_file):
    """
    读取频道模板文件
    返回: [{"name": "CCTV1", "url_template": "http://ip:prot/...", "isp": "湖北联通"}, ...]
    """
    channels = []
    if not os.path.exists(template_file):
        logger.error(f"频道模板文件不存在: {template_file}")
        return channels

    with open(template_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                name, url = line.split(",", 1)
                channels.append({"name": name.strip(), "url_template": url.strip()})
    logger.info(f"  读取到 {len(channels)} 个频道: {template_file}")
    return channels

def replace_ip_in_url(url_template, ip, port):
    """将URL模板中的 ip:prot 替换为实际IP和端口"""
    return url_template.replace("ip:prot", f"{ip}:{port}")

def get_channel_category(channel_name):
    """获取频道所属分类"""
    for cat, channels in CHANNEL_CATEGORIES.items():
        if channel_name in channels:
            return cat
    return "其他频道"

def get_channel_logo(channel_name):
    """获取频道logo URL"""
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

# ==================== 频道验证与链接生成 ====================

def verify_channel_with_isp_ips(channel, isp_ip_results):
    """
    用指定ISP的IP列表验证频道可用性
    返回: [{"name":, "url":, "ip":, "port":, "isp":, "speed":}, ...]
    """
    ch_name = channel["name"]
    url_tpl = channel["url_template"]
    verified_urls = []

    for isp_info in isp_ip_results:
        ip = isp_info["ip"]
        port = isp_info["port"]
        isp = isp_info["isp"]
        speed = isp_info.get("speed", -1)

        url = replace_ip_in_url(url_tpl, ip, port)
        if test_url_availability(url, timeout=5):
            verified_urls.append({
                "name": ch_name,
                "url": url,
                "ip": ip,
                "port": port,
                "isp": isp,
                "speed": speed
            })
    return verified_urls

def verify_all_channels_multi_isp(channel_lists, isp_speed_map):
    """
    对所有运营商的频道模板进行验证
    channel_lists: [{"isp":, "file":, "channels": [...]}]
    isp_speed_map: {"湖北联通": [{"ip":, "port":, "speed":}, ...]}
    返回: 所有验证通过的频道链接（带ISP来源标记）
    """
    all_verified = []  # 所有通过验证的频道链接
    channel_best = {}  # 每个频道名 -> 最佳链接（速度最快）

    for cl in channel_lists:
        isp = cl["isp"]
        channels = cl["channels"]

        if isp not in isp_speed_map or not isp_speed_map[isp]:
            logger.warning(f"[{isp}] 没有可用IP，跳过该运营商的频道验证")
            continue

        # 取该ISP测速最快的前N个IP
        top_ips = isp_speed_map[isp][:10]
        # 附加isp信息
        isp_ip_info = [{"ip": r["ip"], "port": r["port"], "isp": isp, "speed": r["speed"]} for r in top_ips]

        logger.info(f"\n--- 验证 [{isp}] 的 {len(channels)} 个频道 (可用IP: {len(isp_ip_info)}) ---")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(verify_channel_with_isp_ips, ch, isp_ip_info): ch
                for ch in channels
            }
            done = 0
            for future in as_completed(futures):
                done += 1
                ch = futures[future]
                results = future.result()
                if results:
                    all_verified.extend(results)
                    # 保留速度最快的
                    best = max(results, key=lambda x: x["speed"])
                    if ch["name"] not in channel_best or best["speed"] > channel_best[ch["name"]]["speed"]:
                        channel_best[ch["name"]] = best
                    logger.info(f"  [{done}/{len(channels)}] ✓ {ch['name']} -> {len(results)}个可用IP, 最佳: {best['ip']} ({best['speed']:.2f}MB/s)")
                else:
                    logger.warning(f"  [{done}/{len(channels)}] ✗ {ch['name']} -> 全部IP不可用")

    logger.info(f"\n频道验证汇总: 总可用链接 {len(all_verified)} 条, 最佳频道 {len(channel_best)} 个")
    return all_verified, channel_best

# ==================== 合并输出模块 ====================

def generate_final_outputs(all_verified, channel_best, output_m3u8, output_txt):
    """
    生成最终的 m3u8 和 txt 文件
    all_verified: 所有验证通过的链接列表
    channel_best: 每个频道最佳链接
    """
    if not all_verified and not channel_best:
        logger.error("没有可用的频道链接，无法生成输出文件")
        return

    logger.info(f"\n{'='*60}")
    logger.info(f"开始生成输出文件")
    logger.info(f"  总验证链接: {len(all_verified)}")
    logger.info(f"  最佳频道数: {len(channel_best)}")
    logger.info(f"{'='*60}")

    # ---- 生成完整 m3u8（含所有可用IP的链接）----
    with open(output_m3u8, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')

        # 按频道名分组
        from collections import OrderedDict
        channel_groups = OrderedDict()
        for item in all_verified:
            name = item["name"]
            if name not in channel_groups:
                channel_groups[name] = []
            channel_groups[name].append(item)

        for ch_name, links in channel_groups.items():
            category = get_channel_category(ch_name)
            logo = get_channel_logo(ch_name)
            for link in links:
                extinf = (
                    f'#EXTINF:-1 tvg-id="{ch_name}" '
                    f'tvg-name="{ch_name}" '
                    f'tvg-logo="{logo}" '
                    f'group-title="{category}",{ch_name}'
                )
                f.write(extinf + "\n")
                f.write(link["url"] + "\n")

    logger.info(f"✓ m3u8 已保存: {output_m3u8}")

    # ---- 生成简洁 txt（每个频道取最佳IP）----
    with open(output_txt, "w", encoding="utf-8") as f:
        # 按分类排序输出
        for cat in ["央视频道", "卫视频道", "其他频道"]:
            for ch_name in CHANNEL_CATEGORIES.get(cat, []):
                if ch_name in channel_best:
                    link = channel_best[ch_name]
                    f.write(f"{ch_name},{link['url']}\n")
            # 也输出不在分类中的频道
        for ch_name, link in channel_best.items():
            if ch_name not in [c for cat_chs in CHANNEL_CATEGORIES.values() for c in cat_chs]:
                f.write(f"{ch_name},{link['url']}\n")

    logger.info(f"✓ txt 已保存: {output_txt}")

    # ---- 生成按运营商分组的 m3u8 ----
    isp_m3u8 = os.path.join(SCRIPT_DIR, "hsmd_by_isp.m3u8")
    with open(isp_m3u8, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f'#EXTM3U x-tvg-url="{EPG_URL}"\n')

        # 按ISP分组
        isp_groups = {}
        for item in all_verified:
            isp = item.get("isp", "未知")
            if isp not in isp_groups:
                isp_groups[isp] = []
            isp_groups[isp].append(item)

        for isp, links in sorted(isp_groups.items()):
            f.write(f"\n# ===== {isp} ({len(links)} 条链接) =====\n")
            # 按频道分组
            ch_groups = OrderedDict()
            for item in links:
                name = item["name"]
                if name not in ch_groups:
                    ch_groups[name] = []
                ch_groups[name].append(item)

            for ch_name, ch_links in ch_groups.items():
                category = get_channel_category(ch_name)
                logo = get_channel_logo(ch_name)
                for link in ch_links:
                    extinf = (
                        f'#EXTINF:-1 tvg-id="{ch_name}" '
                        f'tvg-name="{ch_name}" '
                        f'tvg-logo="{logo}" '
                        f'group-title="{category}",{ch_name}'
                    )
                    f.write(extinf + "\n")
                    f.write(link["url"] + "\n")

    logger.info(f"✓ 按运营商分组m3u8已保存: {isp_m3u8}")

    # ---- 生成JSON报告 ----
    report = {
        "scan_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_verified_links": len(all_verified),
        "total_unique_channels": len(channel_best),
        "channels_by_isp": {},
        "best_channels": {},
    }
    for item in all_verified:
        isp = item.get("isp", "未知")
        if isp not in report["channels_by_isp"]:
            report["channels_by_isp"][isp] = set()
        report["channels_by_isp"][isp].add(item["name"])

    for isp, ch_set in report["channels_by_isp"].items():
        report["channels_by_isp"][isp] = sorted(list(ch_set))

    for ch_name, link in channel_best.items():
        report["best_channels"][ch_name] = {
            "url": link["url"],
            "ip": link["ip"],
            "port": link["port"],
            "isp": link["isp"],
            "speed_MB_s": round(link["speed"], 2)
        }

    report_file = os.path.join(SCRIPT_DIR, "scan_report.json")
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"✓ 扫描报告已保存: {report_file}")

    # ---- 打印汇总 ----
    print_summary(report)


def print_summary(report):
    """打印最终汇总"""
    logger.info("\n" + "="*60)
    logger.info("📊 最终汇总报告")
    logger.info("="*60)
    logger.info(f"  总验证通过链接: {report['total_verified_links']}")
    logger.info(f"  唯一频道数: {report['total_unique_channels']}")
    logger.info(f"\n  各运营商频道覆盖:")
    for isp, channels in report["channels_by_isp"].items():
        logger.info(f"    [{isp}] {len(channels)} 个频道")
    logger.info(f"\n  输出文件:")
    logger.info(f"    - {OUTPUT_M3U8} (完整列表)")
    logger.info(f"    - {OUTPUT_TXT} (简洁列表)")
    logger.info(f"    - {os.path.join(SCRIPT_DIR, 'hsmd_by_isp.m3u8')} (按运营商分组)")
    logger.info(f"    - {os.path.join(SCRIPT_DIR, 'scan_report.json')} (详细报告)")
    logger.info("="*60)


# ==================== 主流程 ====================

def main():
    logger.info("="*60)
    logger.info("🚀 HSMDTV 直播源扫描器 v2 启动")
    logger.info("="*60)
    start_time = time.time()

    # ---- Step 1: 读取IP列表 ----
    logger.info("\n>>> Step 1: 读取IP列表")
    ip_entries = parse_ip_file(IP_LIST_FILE)
    if not ip_entries:
        logger.error("没有读取到任何IP记录，退出")
        sys.exit(1)

    # 按运营商分组显示
    isp_count = {}
    for e in ip_entries:
        isp_count[e["isp"]] = isp_count.get(e["isp"], 0) + 1
    for isp, cnt in isp_count.items():
        logger.info(f"  {isp}: {cnt} 条")

    # ---- Step 2: D段扫描 ----
    logger.info("\n>>> Step 2: 开始D段扫描 (1-256)")
    scan_results = scan_all_ips(ip_entries)

    # 汇总有结果的运营商
    available_isps = {k: v for k, v in scan_results.items() if v}
    if not available_isps:
        logger.error("扫描后没有任何可用IP，退出")
        sys.exit(1)

    logger.info(f"\n有可用IP的运营商: {len(available_isps)} 个")
    for isp, ips in available_isps.items():
        logger.info(f"  [{isp}] {len(ips)} 个可用IP")

    # ---- Step 3: 批量测速 ----
    logger.info("\n>>> Step 3: 开始批量测速")
    isp_speed_map = {}  # {isp: [{"ip":, "port":, "speed":}, ...]}
    for isp, ip_list in available_isps.items():
        results = batch_speed_test(ip_list, isp)
        isp_speed_map[isp] = results

    # ---- Step 4: 发现所有频道列表文件 ----
    logger.info(f"\n>>> Step 4: 发现频道列表文件")
    logger.info(f"搜索目录: {BASE_DIR}/*_list.txt")
    list_files = discover_channel_list_files(BASE_DIR)

    if not list_files:
        logger.error(f"在 {BASE_DIR} 下没有找到任何 *_list.txt 文件")
        sys.exit(1)

    # 读取所有频道模板
    channel_lists = []
    for lf in list_files:
        isp = lf["isp"]
        channels = load_channel_template(lf["file"])
        if channels:
            channel_lists.append({
                "isp": isp,
                "file": lf["file"],
                "channels": channels
            })

    if not channel_lists:
        logger.error("没有读取到任何频道模板，退出")
        sys.exit(1)

    total_channels = sum(len(cl["channels"]) for cl in channel_lists)
    logger.info(f"共加载 {len(channel_lists)} 个运营商的 {total_channels} 个频道模板")

    # ---- Step 5: 验证频道链接 ----
    logger.info("\n>>> Step 5: 验证频道链接可用性")
    all_verified, channel_best = verify_all_channels_multi_isp(channel_lists, isp_speed_map)

    # ---- Step 6: 生成最终输出 ----
    logger.info("\n>>> Step 6: 生成最终输出文件")
    generate_final_outputs(all_verified, channel_best, OUTPUT_M3U8, OUTPUT_TXT)

    # ---- 完成 ----
    elapsed = time.time() - start_time
    logger.info(f"\n🎉 全部完成！总耗时: {elapsed:.1f} 秒")


if __name__ == "__main__":
    main()
