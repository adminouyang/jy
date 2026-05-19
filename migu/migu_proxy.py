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
import sys
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import urllib.request
import ssl
from http.client import HTTPConnection
import socket

# 启用详细调试
DEBUG = True

# 忽略SSL证书验证
ssl._create_default_https_context = ssl._create_unverified_context

# 设置更长的超时时间
socket.setdefaulttimeout(30)

# EPG链接和台标链接
EPG_URLS = [
    "https://epg.112114.xyz/pp.xml",
    "https://cdn.jsdelivr.net/gh/develop202/migu_video/playback.xml",
    "https://develop202.github.io/migu_video/playback.xml"
]

LOGO_BASE_URL = "https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/"

def debug_log(message):
    """调试日志"""
    if DEBUG:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {message}", file=sys.stderr)

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

def send_get_request_with_retry(url, headers, max_retries=3):
    """发送GET请求，支持重试"""
    for attempt in range(max_retries):
        try:
            debug_log(f"尝试请求 {url} (第{attempt+1}次)")
            
            # 创建请求对象
            req = urllib.request.Request(url)
            
            # 添加请求头
            for key, value in headers.items():
                req.add_header(key, value)
            
            # 添加更多请求头以模拟真实浏览器
            req.add_header('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')
            req.add_header('Accept-Encoding', 'gzip, deflate, br')
            req.add_header('Connection', 'keep-alive')
            req.add_header('Cache-Control', 'no-cache')
            req.add_header('Pragma', 'no-cache')
            
            # 设置User-Agent池
            user_agents = [
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Linux; Android 10; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
            ]
            req.add_header('User-Agent', random.choice(user_agents))
            
            # 发送请求
            with urllib.request.urlopen(req, timeout=15) as response:
                # 检查响应状态
                if response.status != 200:
                    debug_log(f"HTTP状态码异常: {response.status}")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)  # 指数退避
                        continue
                    return None
                
                # 读取响应内容
                content = response.read()
                
                # 尝试解码
                try:
                    result = content.decode('utf-8')
                except UnicodeDecodeError:
                    try:
                        result = content.decode('gbk')
                    except:
                        result = content.decode('utf-8', errors='ignore')
                
                debug_log(f"请求成功，响应长度: {len(result)}")
                return result
                
        except urllib.error.HTTPError as e:
            debug_log(f"HTTP错误: {e.code} {e.reason}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except urllib.error.URLError as e:
            debug_log(f"URL错误: {e.reason}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except socket.timeout:
            debug_log(f"请求超时")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception as e:
            debug_log(f"请求异常: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
    
    return None

def get_code_point(char):
    """获取字符的Unicode码点"""
    if not char:
        return 0
    try:
        return ord(char)
    except:
        return 0

def migu_encrypted_url(rawUrl):
    """加密URL"""
    factorOfEncryption = [8, 3, 7, 6, 6]
    
    try:
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
    except Exception as e:
        debug_log(f"URL加密失败: {str(e)}")
        return rawUrl

def handle_migu_main_request(cont_id):
    """处理咪咕主请求，获取直播地址"""
    try:
        # 生成签名
        tm, salt_sign = get_sign_config(cont_id)
        salt, sign = salt_sign
        
        # 构建请求URL
        url = (f"https://play.miguvideo.com/playurl/v1/play/playurl?"
               f"contId={cont_id}&dolby=true&isMultiView=true&xh265=true&"
               f"os=13&ott=false&rateType=3&salt={salt}&sign={sign}&"
               f"timestamp={tm}&ua=oneplus-12&vr=true")
        
        debug_log(f"请求URL: {url}")
        
        # 完整的请求头
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
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        
        # 发送请求
        body = send_get_request_with_retry(url, headers, max_retries=3)
        if not body:
            debug_log(f"频道 {cont_id} 请求失败")
            return None
        
        # 解析响应
        try:
            data = json.loads(body)
            debug_log(f"响应数据: {json.dumps(data, ensure_ascii=False)[:200]}...")
            
            # 提取URL
            raw_url = data.get("body", {}).get("urlInfo", {}).get("url", "")
            if not raw_url:
                debug_log(f"频道 {cont_id} 未找到URL")
                return None
            
            debug_log(f"原始URL: {raw_url[:100]}...")
            
            # 加密URL
            ott_url = migu_encrypted_url(str(raw_url))
            if not ott_url or not ott_url.strip():
                debug_log(f"频道 {cont_id} URL加密失败")
                return None
            
            debug_log(f"加密后URL: {ott_url[:100]}...")
            return ott_url
            
        except json.JSONDecodeError as e:
            debug_log(f"JSON解析失败: {str(e)}")
            debug_log(f"响应内容: {body[:500]}")
            return None
        except Exception as e:
            debug_log(f"解析响应异常: {str(e)}")
            return None
            
    except Exception as e:
        debug_log(f"处理请求异常: {str(e)}")
        return None

def get_logo_url(channel_name):
    """根据频道名称获取台标URL"""
    try:
        # 清理频道名称
        clean_name = re.sub(r'[^\w]', '', channel_name)
        # 特殊处理一些常见频道
        logo_map = {
            'CCTV1': 'CCTV1',
            'CCTV2': 'CCTV2',
            'CCTV3': 'CCTV3',
            'CCTV4': 'CCTV4',
            'CCTV5': 'CCTV5',
            'CCTV5+': 'CCTV5PLUS',
            'CCTV6': 'CCTV6',
            'CCTV7': 'CCTV7',
            'CCTV8': 'CCTV8',
            'CCTV9': 'CCTV9',
            'CCTV10': 'CCTV10',
            'CCTV11': 'CCTV11',
            'CCTV12': 'CCTV12',
            'CCTV13': 'CCTV13',
            'CCTV14': 'CCTV14',
            'CCTV15': 'CCTV15',
            'CCTV16': 'CCTV16',
            'CCTV17': 'CCTV17',
        }
        
        logo_name = logo_map.get(channel_name, clean_name)
        return f"{LOGO_BASE_URL}{logo_name}.png"
    except:
        return f"{LOGO_BASE_URL}{channel_name}.png"

def parse_channel_list_with_categories(filename):
    """解析带分类的频道列表文件"""
    categories = []
    current_category = None
    
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                
                # 检查是否是分类行
                if line.endswith(',#genre#'):
                    category_name = line.replace(',#genre#', '').strip()
                    current_category = {
                        'name': category_name,
                        'channels': []
                    }
                    categories.append(current_category)
                    debug_log(f"发现分类: {category_name}")
                # 解析频道行
                elif current_category is not None and 'id=' in line:
                    parts = line.split(',id=')
                    if len(parts) == 2:
                        channel_name = parts[0].strip()
                        channel_id = parts[1].strip()
                        if channel_id:
                            current_category['channels'].append({
                                'name': channel_name,
                                'id': channel_id
                            })
                            debug_log(f"添加频道: {channel_name} (ID: {channel_id})")
                else:
                    debug_log(f"跳过第{line_num}行: {line}")
    
    except Exception as e:
        debug_log(f"解析文件失败: {str(e)}")
    
    return categories

def test_single_channel(channel_id, channel_name="测试频道"):
    """测试单个频道是否能获取地址"""
    print(f"\n{'='*60}")
    print(f"测试频道: {channel_name} (ID: {channel_id})")
    print(f"{'='*60}")
    
    url = handle_migu_main_request(channel_id)
    if url:
        print(f"✓ 测试成功!")
        print(f"直播地址: {url[:100]}...")
        return True
    else:
        print(f"✗ 测试失败!")
        return False

def generate_files_with_categories(categories, output_txt="migu.txt", output_m3u="migu.m3u"):
    """按分类生成文件"""
    print(f"\n开始获取直播地址...")
    print(f"{'='*60}")
    
    # 写入txt文件头部
    with open(output_txt, 'w', encoding='utf-8') as txt_file:
        txt_file.write(f"# 咪咕直播源 - 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        txt_file.write(f"# GitHub Actions自动生成\n")
        txt_file.write(f"# 源文件: migu720plist.txt\n")
        txt_file.write("#" * 60 + "\n\n")
    
    # 写入m3u文件头部
    with open(output_m3u, 'w', encoding='utf-8') as m3u_file:
        m3u_file.write("#EXTM3U\n")
        # 添加多个EPG源
        for i, epg_url in enumerate(EPG_URLS, 1):
            m3u_file.write(f"#EXTINF:-1 tvg-id=\"EPG{i}\" tvg-name=\"电子节目单{i}\" tvg-logo=\"\" group-title=\"EPG\",电子节目单{i}\n")
            m3u_file.write(f"{epg_url}\n")
        m3u_file.write("\n")
    
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
            debug_log(f"分类 {category_name} 没有频道，跳过")
            continue
        
        print(f"\n处理分类: {category_name} ({len(channels)}个频道)")
        print(f"{'-'*40}")
        
        # 写入txt文件分类标题
        with open(output_txt, 'a', encoding='utf-8') as txt_file:
            txt_file.write(f"\n# {category_name}\n")
        
        for channel in channels:
            current_index += 1
            channel_name = channel['name']
            channel_id = channel['id']
            
            print(f"[{current_index}/{total_channels}] {channel_name}...", end=' ', flush=True)
            
            # 获取直播地址
            live_url = handle_migu_main_request(channel_id)
            
            if live_url:
                print("✓")
                
                # 生成台标URL
                logo_url = get_logo_url(channel_name)
                
                # 写入txt文件
                with open(output_txt, 'a', encoding='utf-8') as txt_file:
                    txt_file.write(f"{channel_name},{live_url}\n")
                
                # 写入m3u文件
                with open(output_m3u, 'a', encoding='utf-8') as m3u_file:
                    m3u_file.write(f"#EXTINF:-1 tvg-id=\"{channel_id}\" tvg-name=\"{channel_name}\" tvg-logo=\"{logo_url}\" group-title=\"{category_name}\",{channel_name}\n")
                    m3u_file.write(f"{live_url}\n")
                
                success_count += 1
            else:
                print("✗")
                failed_channels.append(f"{category_name} - {channel_name}")
            
            # 添加延迟，避免请求过于频繁
            time.sleep(0.8)
    
    print(f"\n{'='*60}")
    print(f"生成完成!")
    print(f"总频道数: {total_channels}")
    print(f"成功获取: {success_count} 个频道")
    
    if failed_channels:
        print(f"失败频道: {len(failed_channels)} 个")
        with open("failed_channels.txt", 'w', encoding='utf-8') as f:
            f.write("# 失败的频道列表\n")
            f.write(f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for failed in failed_channels:
                f.write(f"{failed}\n")
        print(f"失败列表已保存到: failed_channels.txt")
    
    print(f"\n已生成文件:")
    print(f"  - {output_txt}")
    print(f"  - {output_m3u}")
    
    return success_count, total_channels

def main():
    """主函数"""
    print("咪咕直播源生成脚本 - GitHub Actions优化版")
    print("=" * 60)
    
    # 频道列表文件名
    channel_list_file = "migu720plist.txt"
    
    # 检查文件是否存在
    if not os.path.exists(channel_list_file):
        print(f"错误: 未找到频道列表文件 {channel_list_file}")
        print("请确保该文件与脚本在同一目录下")
        return
    
    # 测试网络连接
    print("测试网络连接...")
    try:
        import urllib.request
        response = urllib.request.urlopen("https://play.miguvideo.com", timeout=10)
        print(f"✓ 网络连接正常 (状态码: {response.status})")
    except Exception as e:
        print(f"✗ 网络连接失败: {str(e)}")
        print("提示: GitHub Actions可能需要配置代理或检查网络设置")
    
    # 解析带分类的频道列表
    print(f"\n解析频道列表文件: {channel_list_file}")
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
    print(f"读取到 {len(categories)} 个分类，共 {total_channels} 个频道")
    
    # 显示分类信息
    for i, category in enumerate(categories, 1):
        print(f"  分类{i}: {category['name']} - {len(category['channels'])} 个频道")
    
    # 测试前几个频道
    print(f"\n{'='*60}")
    print("开始测试前3个频道...")
    test_count = 0
    test_success = 0
    
    for category in categories:
        for channel in category['channels'][:3]:  # 只测试前3个
            if test_count >= 3:
                break
            if test_single_channel(channel['id'], channel['name']):
                test_success += 1
            test_count += 1
            time.sleep(1)
        if test_count >= 3:
            break
    
    print(f"\n测试结果: {test_success}/{test_count} 成功")
    
    if test_success == 0:
        print("警告: 所有测试频道都失败，可能API已失效或网络受限")
        print("建议:")
        print("1. 检查频道ID是否正确")
        print("2. 检查网络连接")
        print("3. 尝试在本地运行测试")
        user_input = input("\n是否继续生成所有频道? (y/n): ")
        if user_input.lower() != 'y':
            return
    
    # 生成播放列表
    print(f"\n{'='*60}")
    print("开始生成所有频道...")
    
    success_count, total = generate_files_with_categories(categories)
    
    # 生成统计报告
    with open("generation_report.md", 'w', encoding='utf-8') as f:
        f.write("# 咪咕直播源生成报告\n\n")
        f.write(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"总频道数: {total}\n")
        f.write(f"成功获取: {success_count}\n")
        f.write(f"成功率: {success_count/total*100:.1f}%\n\n")
        
        if success_count < total:
            f.write("## 失败频道\n")
            for category in categories:
                failed_in_category = []
                for channel in category['channels']:
                    # 这里需要记录哪些频道失败了，简化处理
                    pass
                if failed_in_category:
                    f.write(f"### {category['name']}\n")
                    for channel in failed_in_category:
                        f.write(f"- {channel}\n")
    
    print(f"\n统计报告已保存到: generation_report.md")

if __name__ == "__main__":
    main()
