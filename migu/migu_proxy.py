#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本脚本改编自直播源论坛的PHP脚本：https://bbs.livecodes.vip/
功能：从咪咕视频获取直播源，生成m3u播放列表
更新：去除缓存机制，添加epg和台标链接，按分类保存
"""

import os
import json
import hashlib
import random
import time
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import urllib.request
import ssl

# 忽略SSL证书验证
ssl._create_default_https_context = ssl._create_unverified_context

# EPG链接和台标链接
EPG_URL = "https://cdn.jsdelivr.net/gh/develop202/migu_video/playback.xml,https://ghfast.top/raw.githubusercontent.com/develop202/migu_video/refs/heads/main/playback.xml,https://hk.gh-proxy.org/raw.githubusercontent.com/develop202/migu_video/refs/heads/main/playback.xml,https://develop202.github.io/migu_video/playback.xml,https://raw.githubusercontents.com/develop202/migu_video/refs/heads/main/playback.xml"  # EPG电子节目单链接
LOGO_BASE_URL = "https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/"  # 台标链接基础URL

def get_sign_config(contId):
    """生成签名配置"""
    appVersion = '2600033500'
    saltValue = '16d4328df21a4138859388418bd252c2'
    timestampMs = str(int(time.time() * 1000))
    ver8 = appVersion[:8]
    md5string = hashlib.md5((timestampMs + contId + ver8).encode('utf-8')).hexdigest()
    prefix = random.randint(0, 999999)
    salt = f"{prefix:06d}80"
    text = md5string + saltValue + 'migu' + salt[:4]
    sign = hashlib.md5(text.encode('utf-8')).hexdigest()
    return timestampMs, [salt, sign]

# def send_get_request(url, headers):
#     """发送GET请求"""
#     try:
#         req = urllib.request.Request(url)
#         for key, value in headers.items():
#             req.add_header(key, value)
        
#         with urllib.request.urlopen(req, timeout=10) as response:
#             return response.read().decode('utf-8')
#     except Exception as e:
#         print(f"请求失败: {e}")
#         return None
def send_get_request(url, headers):
    try:
        req = urllib.request.Request(url)
        for key, value in headers.items():
            req.add_header(key, value)
        
        # 添加代理设置
        proxy = urllib.request.ProxyHandler({'http': '61.53.173.35:7788', 'https': '61.53.173.35:7788'})
        opener = urllib.request.build_opener(proxy)
        urllib.request.install_opener(opener)
        
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        print(f"请求失败: {e}")
        return None

def get_code_point(char):
    """获取字符的Unicode码点（兼容函数）"""
    if not char:
        return 0
    try:
        return ord(char)
    except:
        return 0

def migu_encrypted_url(rawUrl):
    """加密URL"""
    factorOfEncryption = [8, 3, 7, 6, 6]
    
    parsed = urlparse(rawUrl)
    if not parsed:
        return rawUrl
    
    query_params = parse_qs(parsed.query)
    
    puData = query_params.get('puData', [''])[0]
    if not puData:
        return rawUrl
    
    params_to_append = []
    
    ddCalcuExists = 'ddCalcu' in query_params and query_params['ddCalcu'][0]
    
    if not ddCalcuExists:
        userid = query_params.get('userid', ['eeeeeeeee'])[0]
        timestamp = query_params.get('timestamp', ['tttttttttttttt'])[0]
        programId = query_params.get('ProgramID', ['ccccccccc'])[0]
        channelId = query_params.get('Channel_ID', ['nnnnnnnnnnnnnnnn'])[0]
        
        userid_chars = list(userid)
        timestamp_chars = list(timestamp)
        programId_chars = list(programId)
        channelId_chars = list(channelId)
        
        ddCalcu = ''
        puLen = len(puData)
        halfLen = puLen // 2
        
        for i in range(halfLen):
            ddCalcu += puData[puLen - 1 - i]
            ddCalcu += puData[i]
            
            if i == 1:
                idx = factorOfEncryption[0] - 1
                charToEncrypt = 'e'
                if idx < len(userid_chars):
                    charToEncrypt = userid_chars[idx]
                
                codePoint = get_code_point(charToEncrypt)
                encryptedVal = codePoint ^ factorOfEncryption[4]
                encryptedVal %= 26
                encryptedVal += 97
                ddCalcu += chr(encryptedVal)
                
            elif i == 2:
                idx = factorOfEncryption[1] - 1
                charToEncrypt = 't'
                if idx < len(timestamp_chars):
                    charToEncrypt = timestamp_chars[idx]
                
                codePoint = get_code_point(charToEncrypt)
                encryptedVal = codePoint ^ factorOfEncryption[4]
                encryptedVal %= 26
                encryptedVal += 97
                ddCalcu += chr(encryptedVal)
                
            elif i == 3:
                idx = factorOfEncryption[2] - 1
                charToEncrypt = 'c'
                if idx < len(programId_chars):
                    charToEncrypt = programId_chars[idx]
                
                codePoint = get_code_point(charToEncrypt)
                encryptedVal = codePoint ^ factorOfEncryption[4]
                encryptedVal %= 26
                encryptedVal += 97
                ddCalcu += chr(encryptedVal)
                
            elif i == 4:
                idx = factorOfEncryption[3] - 1
                charToEncrypt = 'n'
                if idx < len(channelId_chars):
                    charToEncrypt = channelId_chars[idx]
                
                codePoint = get_code_point(charToEncrypt)
                encryptedVal = codePoint ^ factorOfEncryption[4]
                encryptedVal %= 26
                encryptedVal += 97
                ddCalcu += chr(encryptedVal)
        
        if puLen % 2 == 1:
            ddCalcu += puData[halfLen]
        
        params_to_append.append(f"ddCalcu={ddCalcu}")
    
    sv = query_params.get('sv', [''])[0]
    if not sv:
        params_to_append.append("sv=10004")
    
    ct = query_params.get('ct', [''])[0]
    if not ct:
        params_to_append.append("ct=android")
    
    if params_to_append:
        if '?' in rawUrl:
            separator = '&' if rawUrl[-1] not in ['?', '&'] else ''
            return rawUrl + separator + '&'.join(params_to_append)
        else:
            return rawUrl + '?' + '&'.join(params_to_append)
    
    return rawUrl

def handle_migu_main_request(cont_id):
    """处理咪咕主请求，获取直播地址"""
    # 生成签名
    tm, salt_sign = get_sign_config(cont_id)
    salt, sign = salt_sign
    
    # 构建请求URL
    url = (f"https://play.miguvideo.com/playurl/v1/play/playurl?"
           f"contId={cont_id}&dolby=true&isMultiView=true&xh265=true&"
           f"os=13&ott=false&rateType=3&salt={salt}&sign={sign}&"
           f"timestamp={tm}&ua=oneplus-12&vr=true")
    
    # 请求头
    headers = {
        "Host": "play.miguvideo.com",
        "appId": "miguvideo",
        "terminalId": "android",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; oneplus-13 Build/TP1A.220624.014)",
        "MG-BH": "true",
        "appVersionName": "6.3.35",
        "appVersion": "2600033500",
        "Phone-Info": "oneplus-13|13",
        "X-UP-CLIENT-CHANNEL-ID": "2600033500-99000-201600010010028",
        "APP-VERSION-CODE": "260335005",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    
    # 发送请求
    body = send_get_request(url, headers)
    if not body:
        return None
    
    try:
        data = json.loads(body)
        raw_url = data.get("body", {}).get("urlInfo", {}).get("url", "")
        if not raw_url:
            return None
        
        # 加密URL
        ott_url = migu_encrypted_url(str(raw_url))
        if not ott_url or not ott_url.strip():
            return None
        
        return ott_url
    except:
        return None

def get_logo_url(channel_name):
    """根据频道名称获取台标URL"""
    # 清理频道名称，移除特殊字符
    clean_name = re.sub(r'[^\w]', '', channel_name)
    return f"{LOGO_BASE_URL}{clean_name}.png"

def parse_channel_list_with_categories(filename):
    """解析带分类的频道列表文件，返回分类和频道信息"""
    categories = []
    current_category = None
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                # 检查是否是分类行（包含#genre#）
                if line.endswith(',#genre#'):
                    category_name = line.replace(',#genre#', '').strip()
                    current_category = {
                        'name': category_name,
                        'channels': []
                    }
                    categories.append(current_category)
                # 解析频道行：频道名称,id=xxxx
                elif current_category is not None and 'id=' in line:
                    # 解析格式：频道名称,id=xxxx
                    parts = line.split(',id=')
                    if len(parts) == 2:
                        channel_name = parts[0].strip()
                        channel_id = parts[1].strip()
                        current_category['channels'].append({
                            'name': channel_name,
                            'id': channel_id
                        })
    
    except Exception as e:
        print(f"解析文件 {filename} 失败: {e}")
    
    return categories

def generate_files_with_categories(categories, output_txt="migu/migu.txt", output_m3u="migu/migu.m3u"):
    """按分类生成m3u播放列表和文本文件"""
    print(f"开始获取直播地址...")
    print("-" * 50)
    
    # 写入txt文件
    with open(output_txt, 'w', encoding='utf-8') as txt_file:
        txt_file.write(f"# 咪咕直播源 - 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        txt_file.write("#" * 50 + "\n\n")
    
    # 写入m3u文件头部
    with open(output_m3u, 'w', encoding='utf-8') as m3u_file:
        # m3u_file.write(f"#PLAYLIST:咪咕直播源\n")
        m3u_file.write("EXTM3U x-tvg-url=\"" + EPG_URL + "\"\n")
        
    
    total_channels = 0
    success_count = 0
    failed_channels = []
    
    # 统计总频道数
    for category in categories:
        total_channels += len(category['channels'])
    
    current_index = 0
    
    for category in categories:
        category_name = category['name']
        channels = category['channels']
        
        if not channels:
            continue
        
        # 写入txt文件分类标题
        with open(output_txt, 'a', encoding='utf-8') as txt_file:
            txt_file.write(f"\n# {category_name}\n")
        
        for channel in channels:
            current_index += 1
            channel_name = channel['name']
            channel_id = channel['id']
            
            print(f"[{current_index}/{total_channels}] 正在获取: {category_name} - {channel_name} (ID: {channel_id})")
            
            # 获取直播地址
            live_url = handle_migu_main_request(channel_id)
            
            if live_url:
                print(f"  ✓ 成功: {live_url[:80]}...")
                
                # 生成台标URL
                logo_url = get_logo_url(channel_name)
                
                # 写入txt文件
                with open(output_txt, 'a', encoding='utf-8') as txt_file:
                    txt_file.write(f"{channel_name},{live_url}\n")
                
                # 写入m3u文件
                with open(output_m3u, 'a', encoding='utf-8') as m3u_file:
                    m3u_file.write(f"EXTINF:-1 tvg-id=\"{channel_id}\" tvg-name=\"{channel_name}\" tvg-logo=\"{logo_url}\" group-title=\"{category_name}\",{channel_name}\n")
                    m3u_file.write(f"{live_url}\n")
                
                success_count += 1
            else:
                print(f"  ✗ 失败: 无法获取直播地址")
                failed_channels.append(f"{category_name} - {channel_name}")
            
            # 添加延迟，避免请求过于频繁
            time.sleep(2)
    
    print("\n" + "=" * 50)
    print(f"生成完成!")
    print(f"总频道数: {total_channels}")
    print(f"成功获取: {success_count} 个频道")
    if failed_channels:
        print(f"失败频道: {len(failed_channels)} 个")
        for failed in failed_channels:
            print(f"  - {failed}")
    
    print(f"\n已生成文件:")
    print(f"  - {output_txt} (按分类保存)")
    print(f"  - {output_m3u} (M3U播放列表，含EPG和台标链接)")

def main():
    """主函数"""
    # 频道列表文件名
    channel_list_file = "migu/migu720plist.txt"
    
    # 检查文件是否存在
    if not os.path.exists(channel_list_file):
        print(f"错误: 未找到频道列表文件 {channel_list_file}")
        print("请确保该文件与脚本在同一目录下")
        return
    
    # 解析带分类的频道列表
    categories = parse_channel_list_with_categories(channel_list_file)
    
    if not categories:
        print(f"错误: 无法从 {channel_list_file} 中解析出频道信息")
        print("请确保文件格式为:")
        print("  分类名称,#genre#")
        print("  频道1,id=xxxx")
        print("  频道2,id=xxxx")
        return
    
    # 统计频道数
    total_channels = sum(len(category['channels']) for category in categories)
    print(f"从 {channel_list_file} 中读取到 {len(categories)} 个分类，共 {total_channels} 个频道")
    
    # 显示分类信息
    for i, category in enumerate(categories, 1):
        print(f"  分类{i}: {category['name']} - {len(category['channels'])} 个频道")
    
    # 生成播放列表
    generate_files_with_categories(categories)

if __name__ == "__main__":
    main()
