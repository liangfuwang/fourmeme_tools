import os
import json
import logging
import hmac
import hashlib
import base64
import re
import sys
import time
import asyncio
import io
import mimetypes
from datetime import datetime
from functools import partial
from pathlib import Path

# 修复 Windows 终端 Unicode 输出
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import requests
import pymysql
from telethon import TelegramClient, events
from dotenv import load_dotenv
from openai import OpenAI
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct
from PIL import Image, ImageDraw, ImageFont

# --- 配置与环境变量 ---
load_dotenv()

# Telegram 配置
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
PHONE_NUMBER = os.getenv("TG_PHONE_NUMBER", "")

# 推特监控群组 ID
TWITTER_GROUP_ID = os.getenv("TWITTER_MONITOR_GROUP_ID", "")

# DeepBricks AI 配置（OpenAI 兼容 API）
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepbricks.ai/v1/")
AI_MODEL = os.getenv("AI_MODEL", "GPT-5.1")
ENABLE_AI_ANALYSIS = os.getenv("ENABLE_AI_ANALYSIS", "false").lower() == "true"
if AI_API_KEY and ENABLE_AI_ANALYSIS:
    ai_client = OpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
else:
    ai_client = None

# 飞书配置
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_SECRET = os.getenv("FEISHU_SIGN_SECRET", "")

# BSC / Four.meme 配置
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed1.binance.org/")
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

# Four.meme 合约地址
FOURMEME_TOKEN_MANAGER = os.getenv(
    "FOURMEME_TOKEN_MANAGER",
    "0x5c952063c7fc8610FFDB798152D69F0B9550762b"
)
FOURMEME_TOKEN_MANAGER_V2 = os.getenv(
    "FOURMEME_TOKEN_MANAGER_V2",
    "0xec4549cadce5da21df6e6422d448034b5233bfbc"
)

# 自动发币开关
ENABLE_AUTO_CREATE = os.getenv("ENABLE_AUTO_CREATE", "false").lower() == "true"

# 创建代币时的初始买入金额（BNB）
PRE_SALE_AMOUNT = float(os.getenv("PRE_SALE_AMOUNT", "0.01"))

# 捆绑发币配置
ENABLE_BUNDLE_BUY = os.getenv("ENABLE_BUNDLE_BUY", "false").lower() == "true"

# 解析副钱包列表：格式 "私钥1:地址1,私钥2:地址2,..."
BUNDLE_WALLETS = []
_bundle_wallets_str = os.getenv("BUNDLE_WALLETS", "").strip()
if _bundle_wallets_str:
    for pair in _bundle_wallets_str.split(","):
        pair = pair.strip()
        if ":" in pair:
            pk, addr = pair.split(":", 1)
            pk = pk.strip()
            addr = addr.strip()
            if pk and addr:
                BUNDLE_WALLETS.append({"private_key": pk, "address": addr})

# ========== MySQL 数据库配置 ==========
DB_CONFIG = {
    'host': os.getenv('MYSQL_HOST', 'localhost'),
    'user': os.getenv('MYSQL_USER', 'root'),
    'password': os.getenv('MYSQL_PASSWORD', ''),
    'database': os.getenv('MYSQL_DATABASE', 'fourmeme_tools'),
    'charset': 'utf8mb4',
}

# --- 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("fourmeme_tools.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("fourmeme_tools")

# --- 数据库 ---


def _get_db_conn():
    """获取数据库连接"""
    return pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)


def init_db():
    """初始化数据库和表"""
    try:
        db_config_no_db = DB_CONFIG.copy()
        database_name = db_config_no_db.pop('database')

        connection = pymysql.connect(**db_config_no_db)
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(f"USE `{database_name}`")
            logger.info(f"数据库 {database_name} 创建/连接成功")

            create_table_sql = """
            CREATE TABLE IF NOT EXISTS tokens (
                id INT AUTO_INCREMENT PRIMARY KEY,
                tweet_username VARCHAR(255) DEFAULT '' COMMENT '推特用户名',
                tweet_nickname VARCHAR(255) DEFAULT '' COMMENT '推特昵称',
                tweet_content TEXT COMMENT '推文内容',
                tweet_url VARCHAR(1024) DEFAULT '' COMMENT '推文链接',
                tweet_type VARCHAR(50) DEFAULT '' COMMENT '推文类型',
                token_name VARCHAR(255) DEFAULT '' COMMENT '代币名称',
                token_ticker VARCHAR(255) DEFAULT '' COMMENT '代币代号',
                token_description TEXT COMMENT '代币描述',
                image_path VARCHAR(1024) DEFAULT '' COMMENT '图片本地路径',
                image_url VARCHAR(1024) DEFAULT '' COMMENT '图片URL',
                tx_hash VARCHAR(255) DEFAULT '' COMMENT '交易哈希',
                token_address VARCHAR(255) DEFAULT '' COMMENT '代币合约地址',
                status VARCHAR(50) DEFAULT 'pending' COMMENT '状态',
                ai_reason TEXT COMMENT 'AI分析理由',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_status (status),
                INDEX idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
            cursor.execute(create_table_sql)
            connection.commit()
            logger.info("数据库表初始化成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        logger.warning("请检查MySQL服务是否启动，或修改数据库配置")
    finally:
        if 'connection' in locals():
            connection.close()


def save_token_record(record: dict) -> int:
    """保存代币创建记录到数据库"""
    try:
        connection = _get_db_conn()
        with connection.cursor() as cursor:
            cursor.execute('''
                INSERT INTO tokens (
                    tweet_username, tweet_nickname, tweet_content, tweet_url,
                    tweet_type, token_name, token_ticker, token_description,
                    image_path, image_url, tx_hash, token_address, status, ai_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                record.get('tweet_username', ''),
                record.get('tweet_nickname', ''),
                record.get('tweet_content', ''),
                record.get('tweet_url', ''),
                record.get('tweet_type', ''),
                record.get('token_name', ''),
                record.get('token_ticker', ''),
                record.get('token_description', ''),
                record.get('image_path', ''),
                record.get('image_url', ''),
                record.get('tx_hash', ''),
                record.get('token_address', ''),
                record.get('status', 'pending'),
                record.get('ai_reason', '')
            ))
            connection.commit()
            record_id = cursor.lastrowid
            logger.info(f"代币记录已保存: id={record_id}, name={record.get('token_name', '')}")
            return record_id
    except Exception as e:
        logger.error(f"保存代币记录失败: {e}")
        return 0
    finally:
        if 'connection' in locals():
            connection.close()


def update_token_status(record_id: int, status: str, tx_hash: str = "", token_address: str = ""):
    """更新代币创建状态"""
    try:
        connection = _get_db_conn()
        with connection.cursor() as cursor:
            if tx_hash and token_address:
                cursor.execute(
                    'UPDATE tokens SET status = %s, tx_hash = %s, token_address = %s WHERE id = %s',
                    (status, tx_hash, token_address, record_id)
                )
            elif tx_hash:
                cursor.execute(
                    'UPDATE tokens SET status = %s, tx_hash = %s WHERE id = %s',
                    (status, tx_hash, record_id)
                )
            else:
                cursor.execute(
                    'UPDATE tokens SET status = %s WHERE id = %s',
                    (status, record_id)
                )
            connection.commit()
            logger.info(f"代币状态已更新: id={record_id}, status={status}")
    except Exception as e:
        logger.error(f"更新代币状态失败: {e}")
    finally:
        if 'connection' in locals():
            connection.close()


def get_recent_tweets_by_user(username: str, minutes: int = 30) -> list:
    """查询指定用户最近N分钟内的推文记录"""
    try:
        connection = _get_db_conn()
        with connection.cursor() as cursor:
            cursor.execute(
                '''SELECT id, tweet_content, token_name, token_ticker, created_at
                   FROM tokens
                   WHERE tweet_username = %s
                     AND created_at >= NOW() - INTERVAL %s MINUTE
                   ORDER BY created_at DESC''',
                (username, minutes)
            )
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"查询近期推文失败: {e}")
        return []
    finally:
        if 'connection' in locals():
            connection.close()


def _calc_text_similarity(text1: str, text2: str) -> float:
    """计算两段文字的相似度（0~1），基于 difflib.SequenceMatcher"""
    from difflib import SequenceMatcher
    if not text1 or not text2:
        return 0.0
    return SequenceMatcher(None, text1, text2).ratio()


def _extract_text_diff(old_text: str, new_text: str) -> str:
    """提取两段相似文本之间的差异部分（新增/修改的内容）"""
    from difflib import SequenceMatcher
    if not old_text or not new_text:
        return ""
    matcher = SequenceMatcher(None, old_text, new_text)
    diffs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'insert':
            diffs.append(new_text[j1:j2])
        elif tag == 'replace':
            diffs.append(new_text[j1:j2])
    return ''.join(diffs).strip()


def detect_similar_tweet(username: str, content: str, minutes: int = 30) -> dict:
    """
    检测同一用户近期是否发过高度相似的推文（修改推文场景）

    仅适用于"发布推文"场景，回复推文不应使用此函数。
    要求：
    - 两条推文都必须有足够长度（>20字符）才进行比较
    - 相似度 >= 0.75 才认为是修改推文（避免短文本误判）
    - 差异部分必须是有意义的文字（非纯数字/标点/URL片段）

    返回: {"is_similar": bool, "old_content": str, "diff": str, "similarity": float}
    """
    # 内容太短不做修改推文检测（避免短回复之间误匹配）
    if not content or len(content.strip()) <= 20:
        return {"is_similar": False}

    recent = get_recent_tweets_by_user(username, minutes)
    if not recent:
        return {"is_similar": False}

    for row in recent:
        old_content = row.get('tweet_content', '')
        if not old_content or len(old_content.strip()) <= 20:
            continue
        similarity = _calc_text_similarity(old_content, content)
        # 相似度 >= 0.75 且 < 1.0 才认为是修改推文
        if similarity >= 0.75 and similarity < 1.0:
            diff = _extract_text_diff(old_content, content)
            # 差异部分必须是有意义的文字，排除纯数字/标点/URL片段
            if not diff or len(diff.strip()) < 2:
                continue
            diff_clean = re.sub(r'[\d\s\.\-\/:?=&%#@!,;]+', '', diff).strip()
            if not diff_clean:
                logger.info(f"修改推文差异为纯数字/标点（'{diff}'），忽略")
                continue
            logger.info(f"检测到修改推文！相似度: {similarity:.2f}, 差异: '{diff}'")
            return {
                "is_similar": True,
                "old_content": old_content,
                "diff": diff,
                "similarity": similarity
            }

    return {"is_similar": False}


# --- 配置校验 ---
def validate_config():
    """验证必要的环境变量"""
    errors = []
    if API_ID == 0:
        errors.append("TG_API_ID")
    if not API_HASH:
        errors.append("TG_API_HASH")
    if not PHONE_NUMBER:
        errors.append("TG_PHONE_NUMBER")

    if errors:
        logger.error(f"缺少必要的环境变量: {', '.join(errors)}")
        logger.error("请在 .env 文件中配置")
        sys.exit(1)

    if ENABLE_AUTO_CREATE:
        create_errors = []
        if not WALLET_PRIVATE_KEY:
            create_errors.append("WALLET_PRIVATE_KEY")
        if not WALLET_ADDRESS:
            create_errors.append("WALLET_ADDRESS")
        if create_errors:
            logger.warning(f"自动发币已启用但缺少配置: {', '.join(create_errors)}")
            logger.warning("将仅进行分析和通知，不会自动创建代币")


# --- 消息分类解析 ---
def _clean_tweet_content(content):
    """清理推文中的冗余信息，只保留纯正文"""
    lines = content.split('\n')
    clean_lines = []
    skip_rest = False
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in [
            '点击查看图片', '点击查看原文', '点击查看视频',
            '查看引用推文', '时间 ', '🎉来自',
            '回复对象:', '- '
        ]):
            continue
        if re.match(r'^引用\s+.+的推文', stripped):
            skip_rest = True
            continue
        if skip_rest:
            continue
        clean_lines.append(line)

    result = '\n'.join(clean_lines).strip()
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


def parse_tweet_message(text):
    """
    解析 Telegram 群组中的推文消息（Debot.ai 格式），提取类型、发布者、图片链接等信息。

    返回:
        dict: {
            'type': 'publish' | 'reply' | 'retweet' | 'unknown',
            'username': str,
            'nickname': str,
            'content': str,
            'url': str,
            'has_image': bool,
            'image_urls': [str],  # 从消息中提取的图片直链（pbs.twimg.com 等）
        }
    """
    if not text:
        return None

    retweet_match = re.match(r'\[.*?转发推文\]\s*-\s*\[(.+?)\]', text)
    if retweet_match:
        nickname = retweet_match.group(1)
        logger.info(f"跳过转发推文: {nickname}")
        return None

    header_match = re.match(
        r'\[@(\S+)\s+(发布推文|回复推文)\]\s*-\s*\[(.+?)\]',
        text
    )

    if not header_match:
        return None

    username = header_match.group(1)
    action = header_match.group(2)
    nickname = header_match.group(3)

    tweet_type = 'publish' if action == '发布推文' else 'reply'

    content_start = header_match.end()
    content = text[content_start:].strip()

    # 提取推文链接
    tweet_url = ""
    url_match = re.search(r'点击查看原文\s*\((https://x\.com/\S+)\)', content)
    if not url_match:
        url_match = re.search(r'(https://x\.com/\S+/status/\d+)', content)
    if url_match:
        tweet_url = url_match.group(1)
        # 清理 URL 末尾可能的右括号
        tweet_url = tweet_url.rstrip(')')

    # 提取图片直链：只取发推人自己的图片，忽略引用推文中的图片
    # 先找到引用推文的分界线位置，只在分界线之前提取图片
    image_urls = []
    quote_boundary = len(content)
    quote_match = re.search(r'引用\s+.+的推文', content)
    if quote_match:
        quote_boundary = quote_match.start()
    # 也检查 "查看引用推文" 的位置
    view_quote_match = re.search(r'查看引用推文', content)
    if view_quote_match:
        quote_boundary = min(quote_boundary, view_quote_match.start())

    own_content = content[:quote_boundary]
    for m in re.finditer(r'点击查看图片\s*\((https?://[^\s\)]+)\)', own_content):
        img_url = m.group(1)
        if 'pbs.twimg.com' in img_url or 'twimg.com' in img_url:
            image_urls.append(img_url)

    has_image = len(image_urls) > 0

    # 提取回复对象的推文 URL（回复推文时，消息中可能包含被回复推文的链接）
    reply_to_url = ""
    if tweet_type == 'reply':
        # Debot 格式中被回复的推文链接通常在 "回复对象:" 行或引用部分
        # 优先匹配与回复者不同的 x.com 链接（即被回复者的原始推文）
        all_tweet_urls = re.findall(r'(https://x\.com/(\w+)/status/\d+)', content)
        for url_full, url_user in all_tweet_urls:
            url_clean = url_full.rstrip(')')
            # 排除回复者自己的推文链接
            if url_user.lower() != username.lower():
                reply_to_url = url_clean
                break

    # 清理冗余信息
    content = _clean_tweet_content(content)

    return {
        'type': tweet_type,
        'username': username,
        'nickname': nickname,
        'content': content,
        'url': tweet_url,
        'has_image': has_image,
        'image_urls': image_urls,
        'reply_to_url': reply_to_url,
    }


# --- 推文抓取 ---
def fetch_tweet(tweet_url: str) -> dict:
    """
    通过推文 URL 获取推文内容、作者信息和图片

    支持格式:
        https://x.com/username/status/1234567890
        https://twitter.com/username/status/1234567890

    Returns:
        {
            'username': str,       # @用户名
            'nickname': str,       # 显示名称
            'content': str,        # 推文正文
            'url': str,            # 推文原始链接
            'images': [str],       # 图片 URL 列表
            'has_image': bool,     # 是否有图片
            'type': 'publish',
        }
    """
    # 从 URL 提取 username 和 status_id
    match = re.match(r'https?://(?:x\.com|twitter\.com)/(\w+)/status/(\d+)', tweet_url.strip())
    if not match:
        logger.error(f"无效的推文 URL: {tweet_url}")
        return {}

    username = match.group(1)
    status_id = match.group(2)

    # 使用 FxTwitter API（免费、无需认证）
    api_url = f"https://api.fxtwitter.com/{username}/status/{status_id}"
    logger.info(f"获取推文: {api_url}")

    try:
        resp = requests.get(api_url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        })
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 200:
            logger.error(f"FxTwitter API 返回错误: {data}")
            return {}

        tweet = data.get("tweet", {})
        author = tweet.get("author", {})

        # 提取图片
        images = []
        media = tweet.get("media", {})
        for photo in media.get("photos", []):
            img_url = photo.get("url", "")
            if img_url:
                images.append(img_url)

        result = {
            'username': author.get("screen_name", username),
            'nickname': author.get("name", username),
            'content': tweet.get("text", ""),
            'url': tweet.get("url", tweet_url),
            'images': images,
            'has_image': len(images) > 0,
            'type': 'publish',
        }

        logger.info(f"推文获取成功: @{result['username']} ({result['nickname']})")
        logger.info(f"  内容: {result['content'][:100]}...")
        logger.info(f"  图片: {len(images)} 张")

        return result

    except Exception as e:
        logger.error(f"获取推文失败: {e}")
        return {}


def fetch_reply_parent_tweet(tweet_url: str) -> dict:
    """
    通过 FxTwitter API 获取回复推文的原始被回复推文内容和图片。

    先获取回复推文本身的信息，从中提取 replying_to 字段得到被回复的原始推文 URL，
    然后再次调用 API 获取原始推文的完整信息。
    如果传入的是被回复推文的直接 URL，则直接获取。

    Returns:
        {
            'username': str,
            'nickname': str,
            'content': str,
            'url': str,
            'images': [str],
            'has_image': bool,
        }
    """
    if not tweet_url:
        return {}

    # 直接用 fetch_tweet 获取目标推文
    parent_info = fetch_tweet(tweet_url)
    if parent_info:
        logger.info(f"成功获取被回复的原始推文: @{parent_info.get('username', '?')} - {parent_info.get('content', '')[:80]}...")
        return parent_info

    logger.warning(f"无法获取被回复的原始推文: {tweet_url}")
    return {}


def fetch_tweet_image(tweet_url: str) -> tuple:
    """
    获取推文内容和图片（下载到本地）

    Returns:
        (tweet_info: dict, image_path: str)
    """
    tweet_info = fetch_tweet(tweet_url)
    if not tweet_info:
        return {}, ""

    image_path = ""
    if tweet_info['has_image']:
        # 下载第一张图片
        img_url = tweet_info['images'][0]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 保留原始扩展名
        ext = ".jpg"
        if ".png" in img_url:
            ext = ".png"
        filename = f"tweet_{timestamp}{ext}"
        image_path = download_image_from_url(img_url, filename)

    return tweet_info, image_path


# --- 图片处理 ---
IMAGE_DIR = Path("images")
IMAGE_DIR.mkdir(exist_ok=True)


async def download_tweet_image(event) -> str:
    """从 Telegram 消息中下载图片（如果有）"""
    if not event.message.media:
        return ""

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"tweet_{timestamp}.jpg"
        filepath = IMAGE_DIR / filename

        await event.message.download_media(file=str(filepath))
        logger.info(f"推文图片已保存: {filepath}")
        return str(filepath)
    except Exception as e:
        logger.error(f"下载推文图片失败: {e}")
        return ""


def download_image_from_url(url: str, filename: str = "") -> str:
    """从 URL 下载图片"""
    try:
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"tweet_{timestamp}.jpg"
        filepath = IMAGE_DIR / filename

        response = requests.get(url, timeout=30)
        response.raise_for_status()
        with open(filepath, 'wb') as f:
            f.write(response.content)
        logger.info(f"图片已下载: {filepath}")
        return str(filepath)
    except Exception as e:
        logger.error(f"下载图片失败: {e}")
        return ""


def generate_meme_image(text: str, filename: str = "") -> str:
    """
    生成黄色背景+黑色文字的 meme 图片（1024x1024）
    文字自动居中、自动换行、自动撑满画面，风格饱满专业

    服务器需安装中文字体:
        apt-get install -y fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-cjk

    Args:
        text: 要显示在图片上的文字（一般是 token ticker 或 meme 关键词）
        filename: 输出文件名，默认自动生成

    Returns:
        生成的图片文件路径
    """
    if not filename:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"meme_{timestamp}.png"
    filepath = IMAGE_DIR / filename

    img_size = 1024
    bg_color = (234, 182, 18)   # 黄色背景 #EAB612
    text_color = (25, 25, 25)   # 近黑色文字
    padding = 80                # 边距，留出呼吸空间

    img = Image.new('RGB', (img_size, img_size), bg_color)
    draw = ImageDraw.Draw(img)

    display_text = text.strip()

    # 检测是否含中文
    has_cjk = any('\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf' for c in display_text)

    if has_cjk:
        font_paths = [
            "C:/Windows/Fonts/msyhbd.ttc",     # 微软雅黑 Bold
            "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
            "C:/Windows/Fonts/simhei.ttf",     # 黑体
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/System/Library/Fonts/PingFang.ttc",
        ]
    else:
        font_paths = [
            "C:/Windows/Fonts/arialbd.ttf",    # Arial Bold
            "C:/Windows/Fonts/impact.ttf",     # Impact
            "C:/Windows/Fonts/arial.ttf",      # Arial
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]

    def _load_font(size):
        for fp in font_paths:
            try:
                return ImageFont.truetype(fp, size)
            except (IOError, OSError):
                continue
        # 最后尝试用 Pillow 内置 default 字体，但 size 参数不生效
        logger.warning(f"所有字体路径均不可用，使用默认字体（中文可能无法显示）")
        try:
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
        except Exception:
            return ImageFont.load_default()

    max_w = img_size - padding * 2  # 文字最大宽度
    max_h = img_size - padding * 2  # 文字最大高度

    def _wrap_text(txt, font):
        """将文字按画布宽度自动换行"""
        lines = []
        # 中文按字符拆分，英文按单词拆分
        if has_cjk:
            current_line = ""
            for ch in txt:
                test = current_line + ch
                bbox = draw.textbbox((0, 0), test, font=font)
                if bbox[2] - bbox[0] > max_w and current_line:
                    lines.append(current_line)
                    current_line = ch
                else:
                    current_line = test
            if current_line:
                lines.append(current_line)
        else:
            words = txt.split()
            current_line = ""
            for word in words:
                test = f"{current_line} {word}".strip()
                bbox = draw.textbbox((0, 0), test, font=font)
                if bbox[2] - bbox[0] > max_w and current_line:
                    lines.append(current_line)
                    current_line = word
                else:
                    current_line = test
            if current_line:
                lines.append(current_line)
        return lines if lines else [txt]

    def _calc_block_size(lines, font):
        """计算多行文字块的总宽高"""
        total_h = 0
        max_line_w = 0
        line_height = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            lh = bbox[3] - bbox[1]
            max_line_w = max(max_line_w, lw)
            line_height = max(line_height, lh)
        spacing = int(line_height * 0.35)
        total_h = line_height * len(lines) + spacing * (len(lines) - 1)
        return max_line_w, total_h, line_height, spacing

    # 二分法找到最大字号，上限 400 适配 1024 画布
    lo, hi = 48, 400
    best_size = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(mid)
        lines = _wrap_text(display_text, font)
        block_w, block_h, _, _ = _calc_block_size(lines, font)
        if block_w <= max_w and block_h <= max_h:
            best_size = mid
            lo = mid + 1
        else:
            hi = mid - 1

    # 用最佳字号绘制
    font = _load_font(best_size)
    lines = _wrap_text(display_text, font)
    block_w, block_h, line_height, spacing = _calc_block_size(lines, font)

    # 整体居中
    start_y = (img_size - block_h) // 2
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        x = (img_size - lw) // 2
        y = start_y + i * (line_height + spacing)
        draw.text((x, y), line, fill=text_color, font=font)

    img.save(str(filepath), quality=95)
    logger.info(f"生成 meme 图片: {filepath} (文字: {display_text}, 字号: {best_size})")
    return str(filepath)



# --- 飞书通知 ---
def _build_feishu_tweet_alert(content: str = "", tweet_url: str = "",
                               reason: str = "", username: str = "",
                               nickname: str = "", tweet_type: str = ""):
    """
    构建飞书富文本消息 — 推文告警（无 meme 价值时仅通知推文内容）
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    who = f"{nickname}(@{username})" if nickname and username else (nickname or username or "未知")
    type_label = {"tweet": "发布推文", "reply": "回复推文", "retweet": "转发"}.get(tweet_type, tweet_type)
    title = f"📢 推文告警 - {who}"

    lines = []
    lines.append([{"tag": "text", "text": f"👤 {who} {type_label}"}])
    lines.append([{"tag": "text", "text": " "}])

    # 推文内容摘要
    summary = content[:300] + ("..." if len(content) > 300 else "") if content else "（无内容）"
    lines.append([{"tag": "text", "text": f"📄 {summary}"}])
    lines.append([{"tag": "text", "text": " "}])

    if reason:
        lines.append([{"tag": "text", "text": f"💡 {reason}"}])

    if tweet_url:
        lines.append([{"tag": "a", "text": "🔗 推文原文", "href": tweet_url}])

    lines.append([{"tag": "text", "text": f"ℹ️ 未识别到有效 meme，仅做推文告警"}])
    lines.append([{"tag": "text", "text": f"⏰ {now_str}"}])

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": lines
                }
            }
        }
    }


def _build_feishu_analysis_notify(token_name: str = "", token_ticker: str = "",
                                    description: str = "", tweet_url: str = "",
                                    reason: str = "", username: str = "",
                                    nickname: str = "", tweet_type: str = ""):
    """
    构建飞书富文本消息 — AI 分析结果通知（未自动创建代币时发送）
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"🔍 AI 分析结果 - {token_name}"

    lines = []

    # 推文来源
    who = f"{nickname}(@{username})" if nickname and username else (nickname or username or "未知")
    type_label = {"tweet": "发布推文", "reply": "回复推文", "retweet": "转发"}.get(tweet_type, tweet_type)
    lines.append([{"tag": "text", "text": f"👤 {who} {type_label}"}])

    lines.append([{"tag": "text", "text": " "}])

    # 代币信息
    display_name = f"{token_name}（{token_ticker}）" if token_name != token_ticker else token_name
    lines.append([{"tag": "text", "text": f"💎 Meme: {display_name}"}])
    if description:
        lines.append([{"tag": "text", "text": f"📝 {description[:200]}"}])
    if reason:
        lines.append([{"tag": "text", "text": f"💡 AI理由: {reason}"}])

    lines.append([{"tag": "text", "text": " "}])

    # 链接
    if tweet_url:
        lines.append([{"tag": "a", "text": "🔗 推文原文", "href": tweet_url}])

    lines.append([{"tag": "text", "text": f"⚠️ 自动发币已关闭，仅供参考"}])
    lines.append([{"tag": "text", "text": f"⏰ 时间: {now_str}"}])

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": lines
                }
            }
        }
    }


def _build_feishu_post_result(success: bool, token_name: str = "", token_ticker: str = "",
                               description: str = "", tweet_url: str = "",
                               token_address: str = "", tx_hash: str = "",
                               error: str = "", reason: str = "",
                               bundle_results: list = None):
    """
    构建飞书富文本消息 — 仅在代币创建完成后发送

    成功时展示: 代币信息 + 链接（Four.meme / BSCScan / 推文）+ 地址 + 捆绑买入结果
    失败时展示: 代币信息 + 失败原因 + 推文链接
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if success:
        title = f"✅ 代币创建成功"
    else:
        title = f"❌ 代币创建失败"

    lines = []

    # 代币基本信息
    display_name = f"{token_name}（{token_ticker}）" if token_name != token_ticker else token_name
    lines.append([{"tag": "text", "text": f"💎 {display_name}"}])
    if description:
        lines.append([{"tag": "text", "text": f"📝 {description[:200]}"}])

    lines.append([{"tag": "text", "text": " "}])

    if success:
        # 成功：展示链接
        if tweet_url:
            lines.append([{"tag": "a", "text": "🔗 推文原文", "href": tweet_url}])
        if token_address:
            lines.append([{"tag": "a", "text": f"🔗 GMGN", "href": f"https://gmgn.ai/bsc/token/{token_address.lower()}"}])

        lines.append([{"tag": "text", "text": " "}])

        if token_address:
            lines.append([{"tag": "text", "text": f"📋 代币地址: {token_address}"}])
        if tx_hash:
            lines.append([{"tag": "text", "text": f"📋 交易哈希: {tx_hash}"}])

        # 捆绑买入结果
        if bundle_results:
            lines.append([{"tag": "text", "text": " "}])
            success_count = sum(1 for r in bundle_results if r.get("success"))
            lines.append([{"tag": "text", "text": f"📦 捆绑买入: {success_count}/{len(bundle_results)} 成功"}])
            for br in bundle_results:
                wallet = br.get("wallet", "?")
                wallet_short = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet
                if br.get("success"):
                    br_tx = br.get("tx_hash", "")
                    lines.append([{"tag": "text", "text": f"  ✅ {wallet_short} ({br.get('amount_bnb', '?')} BNB)"}])
                else:
                    lines.append([{"tag": "text", "text": f"  ❌ {wallet_short}: {br.get('error', '未知')[:50]}"}])
    else:
        # 失败：展示原因
        lines.append([{"tag": "text", "text": f"❌ 失败原因: {error or '未知错误'}"}])
        lines.append([{"tag": "text", "text": " "}])
        if tweet_url:
            lines.append([{"tag": "a", "text": "🔗 推文原文", "href": tweet_url}])

    if reason:
        lines.append([{"tag": "text", "text": f"💡 AI理由: {reason}"}])

    lines.append([{"tag": "text", "text": f"⏰ 时间: {now_str}"}])

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": lines
                }
            }
        }
    }


def _send_feishu_sync(post_payload):
    """发送飞书 Webhook 通知（同步）"""
    if not FEISHU_WEBHOOK or not FEISHU_SECRET:
        logger.warning("飞书 Webhook 未配置，跳过通知")
        return

    timestamp = int(datetime.now().timestamp())
    string_to_sign = f"{timestamp}\n{FEISHU_SECRET}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        "".encode("utf-8"),
        digestmod=hashlib.sha256
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")

    payload = {
        "timestamp": str(timestamp),
        "sign": sign,
        **post_payload
    }

    try:
        response = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        response.raise_for_status()
        resp_data = response.json()
        if resp_data.get("code", 0) != 0:
            logger.error(f"飞书返回错误: {resp_data}")
        else:
            logger.info("飞书通知发送成功")
    except Exception as e:
        logger.error(f"飞书通知发送失败: {e}")


async def send_feishu(post_payload):
    """异步发送飞书通知"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_feishu_sync, post_payload)


# --- AI 分析 ---
MAX_RETRIES = 3


def _analyze_tweet_for_meme_sync(text, revised_context=None, reply_parent_context=None):
    """使用 AI 分析推文，提取 meme 关键词并生成代币信息
    
    Args:
        text: 推文内容
        revised_context: 修改推文上下文 {"old_content": str, "diff": str, "similarity": float}，可选
        reply_parent_context: 被回复的原始推文上下文 {"content": str, "images": [str], "username": str, "url": str}，可选
    """
    if not ai_client:
        logger.error("AI 客户端未初始化，请检查 AI_API_KEY")
        return None

    system_prompt = """你是一个加密货币 Meme 币分析专家。你的任务是判断 CZ（赵长鹏）或何一的推文是否具有「金狗 meme」潜力，**只有真正有梗、有爆发力、有社区传播性的推文才值得创建代币**。

⚠️ 核心原则：宁可漏掉，不要硬凑。绝大多数推文（90%以上）都不适合做 meme，应该返回空结果。

⚠️ 最重要的一条：**回复推文几乎不可能是 meme**。CZ/何一每天回复大量推文，99%的回复都是普通互动（good idea / never too late / 👀 / agree 等），这些绝对不是 meme！

## 第零步：先判断是否值得做 meme（必须全部通过才进入下一步）

以下任何一种情况，**直接返回空结果**，不做 meme 提取：

### ❌ 必须跳过的推文类型

1. **纯 emoji 推文**：正文去掉 emoji/表情后无实质文字内容
   例：🙏 → 空；😂🔥 → 空；👀 → 空

2. **日常社交/礼貌性回复**：感谢、点赞、鼓励、**英文**问候、简短评价等日常对话
   例：Thank you! / Great job / 🙏 / Congrats / Well said / good idea / Good point
   ⚠️ 例外：**中文节日祝福/生肖祝福是金狗 meme！** CZ 用中文发的节日、生肖、年份相关祝福词（如"马年快乐""新年快乐""龙年大吉"等）在加密社区极具传播力，这些**不是普通问候，是 meme**！

3. **简短回复/评论（≤10个英文单词的回复）**：CZ经常用一两句话回复别人，这些短回复几乎都不是meme
   例：good idea. → 空（只是简单评价）
   例：Never too late. I started Binance at 40. → 空（只是普通鼓励/回忆）
   例：Intelligence is probably 10% or less in weight. → 空（只是发表观点）
   例：Agree. → 空
   例：Not really. → 空
   例：Let's see. → 空
   ⚠️ 判断标准：这句话如果是你朋友说的，你会去做一个代币吗？如果不会，就不是meme

4. **正经行业讨论/观点输出**：关于交易所、监管、上币、安全、行业趋势、成功公式的严肃讨论
   例：DEX listing all tokens is good. CEX listing all tokens is bad?
   例：For most CEX, there is a balance somewhere...
   例：Binance holds the largest % of most stablecoins...
   例：Not saying we are perfect, but smart people triple check...

5. **个人生活/旅行分享**：普通生活记录，没有梗
   例：Was a little scared on the horse. (Not AI)

6. **回复他人解释/澄清**：正经回答问题、解释情况
   例：I don't know any details of your case with USDT...Give it some time

7. **只提到已有的主流代币/项目**：BTC, ETH, BNB, SOL, DOGE, USDT, USDC 等
   例：#BNB（引用 BNB Futures 上线）

8. **只使用行业通用术语**：FUD, FOMO, HODL, DYOR, DEX, CEX, AML, KYC, Web3, DeFi, NFT 等
   例：讨论 FUD 现象但没有独特表达

9. **引用/转述他人内容、鸡汤、人生建议、励志语录**：CZ/何一只是转发或引用他人的总结、建议、感悟，本人并未创造新梗
   例："成功公式：每天110%-130%，坚持30年" → 空（鸡汤/人生建议）
   例：Never too late. I started Binance at 40. → 空（励志语录，不是meme）
   例：引用他人对自己采访的总结 → 空

10. **数字、百分比、公式类表达、纯数字串**：纯数字（如电话号码、ID号、随机数字、推文URL中的数字）或百分比不具备 meme 传播力
   例：110%-130% → 空；8046687368 → 空；1234567890 → 空

11. **长篇严肃内容的片段截取**：如果原推文是一段严肃的讨论/建议/观点，即使其中有某个短语，也不应该硬提取

12. **常见英文短语/口头禅/俗语**：这些是日常英语表达，不是meme
   例：Never too late → 空（英文俗语）
   例：good idea → 空（日常表达）
   例：Let's go → 空（太常见）
   例：Time will tell → 空（俗语）
   例：We shall see → 空（常见表达）

### ✅ 什么样的推文才值得做 meme

只有同时满足以下**所有**条件的推文才有 meme 价值：
- 包含**新颖的、有梗的、有冲击力的独特表达**（不是日常用语）
- 这个表达能让加密社区兴奋、引发二次传播和模仿
- 适合作为代币名称（短小、有记忆点、独特）
- 不是行业通用词汇、已有代币名、常见英文短语
- **社区会真的因为这个词去创建meme币**（这是最关键的判断标准）

例如：
- 引号/书名号特别强调的新颖短语："六大门派围攻光明顶"
- CZ/何一自创的新词或缩写：DOYR
- 极短且有梗的宣言：yolo / 我踏马来了
- 有趣的自嘲/金句：四川人人都自称老子 → 老子
- **中文节日/生肖祝福**：马年快乐 / 龙年大吉 / 新年快乐 → CZ 用中文发节日祝福在加密圈极具 meme 传播力

### 🔄 特殊场景：修改推文（高概率出 meme！）

如果消息中标注了「修改推文检测」，说明作者在短时间内对同一条**发布推文**做了修改。这种情况出 meme 的概率极高！
- **修改/新增的差异词汇就是 meme 爆点**
- 作者刻意修改的词语往往是引起社区关注的核心梗
- 例：原文 "蜉蝣也可撼树" → 修改为 "蚍蜉也可撼树，蜉蝣也可争光" → meme 是 "蚍蜉" 或 "蜉蝣"
- 重点关注差异部分，从中提取最有 meme 传播力的词
- ⚠️ 注意：差异部分如果是纯数字、标点、URL片段，则不是有效差异，应返回空

### 💬 特殊场景：回复推文 + 被回复的原始推文

如果消息中附带了「被回复的原始推文」内容，说明这是 CZ/何一对某条推文的回复。此时需要**结合被回复的原始推文一起分析**：
- **原始推文的内容和图片也可能包含 meme 关键词**，CZ/何一的回复可能是对原始推文某个梗的认可/强调
- 如果原始推文包含有 meme 潜力的关键词/图片描述，而 CZ/何一通过回复表达了认可或呼应，那这个关键词就值得做 meme
- ⚠️ 但仍然要严格把关：如果 CZ/何一的回复只是普通互动（good idea / agree / 👀 等），即使原始推文有梗，也不要做 meme
- ⚠️ 判断标准：CZ/何一的回复是否对原始推文中的某个梗进行了**放大、二次创作、或明确认可**

### 📚 特殊场景：CZ 新书相关推文（最高优先级 meme！）

CZ（赵长鹏）即将发布新书，书名本身就是一个超级 meme！如果推文中提到以下关键词，**必须高度重视**：
- 关键词："书名"、"新书"、"书"、"book"、"book title"、"book name"、"my book"、"new book"、"autobiography"、"memoir"
- 如果推文包含类似"我的新书叫xxx"、"书名是xxx"、"my book is called xxx"、"the book title is xxx"等句式，**xxx 就是超级金狗 meme**！
- 即使是回复推文中提到书名，也要高度重视
- 书名通常会用引号("")、书名号（《》）等标点强调，直接提取作为 meme
- ⚠️ 如果只是泛泛提到"在写书"、"书快出了"但没有透露具体书名，则不做 meme（没有具体关键词）
- 这是当前阶段的**最高优先级场景**，一旦 CZ 公布书名，该词必须被捕获

## 第一步：提取 meme 核心词（通过第零步后，按优先级匹配，命中即停）

**规则1 - 引号/书名号短语**：推文中用引号("")、书名号（《》）等标点特别强调的短语 → 直接就是 meme。
  例："六大门派围攻光明顶" → 六大门派围攻光明顶
  例：我的新书叫《Erta Moon》 → Erta Moon（📚 新书书名 = 超级金狗！）
  例：My book is called "Binance Rising" → Binance Rising（📚 新书书名 = 超级金狗！）

**规则2 - 全大写缩写词/造词**：推文中出现全大写的独创缩写词（不在黑名单中） → 直接就是 meme。
  例：DOYR！ → DOYR

**规则3 - 极短且有梗的推文（正文≤5个词，去掉 emoji 后有实质文字）**：
  例：yolo😁 → yolo
  注意：纯 emoji（如🙏/👀）不算有实质文字！
  ⚠️ "good idea"、"agree"、"never too late"等虽然短，但是日常用语，不是meme！

**规则3.5 - 中文节日/生肖/年份祝福（金狗 meme！）**：
  CZ/何一用中文发的节日祝福、生肖祝福、年份祝福等，在加密社区极具传播力和 meme 价值，**必须提取**。
  例：马年快乐！ → 马年快乐（✅ 中文生肖祝福 = 金狗！）
  例：龙年大吉 → 龙年大吉（✅ 中文生肖祝福 = 金狗！）
  例：新年快乐 → 新年快乐（✅ 中文节日祝福 = 金狗！）
  例：中秋节快乐 → 中秋节快乐（✅ 中国传统节日 = 金狗！）
  ⚠️ 注意：英文的 Happy New Year / Merry Christmas 是全球通用祝福，**不算 meme**，只有中文祝福才有 meme 价值
  ⚠️ 判断核心：CZ 作为华人用中文发祝福，加密社区会立刻炒作这个词做 meme 币

**规则4 - 中英双语推文**：优先取中文部分的金句/梗词，忽略英文翻译。
  例：2026, Here is new beginnings. 2026，我踏马来了。 → 我踏马来了

**规则5 - 长推文中有独特金句/梗**：从长段文字中找到最有传播力、最有梗、最适合做 meme 的那个最短词汇。
  例：四川人人都自称老子 → 老子
  ⚠️ 但如果长推文只是正经讨论/回复/解释，没有金句，返回空！

**规则6 - 有传播力的新概念**：推文提到一个新颖的、有传播力的概念 → 提取。
  例：Super Cycle incoming → 超级周期
  ⚠️ 但 crypto winter、bull run、never too late 等老概念/俗语不算新颖！

## 第二步：确定 token_name 和 token_ticker

**核心原则：token_name 和 token_ticker 与 predicted_meme 完全一致。**
- predicted_meme 是中文 → token_name 和 token_ticker 都用中文
- predicted_meme 是英文 → token_name 和 token_ticker 都用英文（保持原始大小写）
- 不做任何翻译、缩写、变形
- ⚠️ 绝对不能把推文URL中的数字、随机字符串作为 token_name

## 黑名单 — 绝对不能作为 meme 的词汇

**主流代币/项目名**：BTC, ETH, BNB, SOL, XRP, ADA, DOGE, SHIB, DOT, AVAX, MATIC, LINK, UNI, AAVE, APE, OP, ARB, SUI, SEI, TIA, JUP, WLD, PEPE, BONK, WIF, FLOKI, TON, TRX, NEAR, ATOM, FTM, INJ, MANA, SAND, APT, USDT, USDC, DAI, BUSD, USD1, Binance, Bitcoin, Ethereum, Solana
**行业通用术语**：FUD, FUDLESS, FOMO, HODL, DYOR, NFA, WAGMI, NGMI, GM, GN, LFG, ATH, ATL, DeFi, NFT, DAO, DEX, CEX, TVL, APY, APR, ICO, IDO, IEO, KYC, AML, Web3, Layer2, L2, DApp, Airdrop, Staking, Mining, Halving, Bull, Bear, Pump, Dump, Rug, Moon, Lambo, Diamond Hands, Paper Hands, Whale, Alpha, Beta, Mainnet, Testnet, Gas, Bridge, Swap, Yield, Liquidity, Farming, Futures, Spot
**通用词/常见短语**：Crypto, Blockchain, Token, Coin, Market, Trading, Price, Chart, Volume, Rally, Bullish, Bearish, Correction, Support, Resistance, Listing, Exchange, News, Balance, Smart, Perfect, Never too late, Good idea, Intelligence, Let's go, Time will tell, Agree, Not really

## 关键注意事项
- 只输出 1 个最核心的 meme，不要输出多个
- **只分析发推人（CZ/何一）本人写的文字**，忽略：引用推文内容、回复对象、点击查看图片/原文/视频、时间戳、来自xxx监控、推文URL
- meme 要尽量短、有冲击力，能做代币名称
- 去掉表情符号、标点符号，只保留纯文字
- **绝大多数推文（90%+）都不适合做 meme，请严格把关，宁可返回空结果**
- ⚠️ **推文URL中的数字（如 2021578380466897368）绝对不是meme内容，切勿提取！**

## 经过验证的实战案例

| 推文原文关键部分 | predicted_meme | 判断依据 |
|----------------|---------------|---------|
| 最近一直在"六大门派围攻光明顶"的剧情里 | 六大门派围攻光明顶 | ✅ 引号强调的独特表达 |
| DOYR！ | DOYR | ✅ 独创大写缩写 |
| yolo😁 | yolo | ✅ 极短有梗 |
| 2026，我踏马来了 | 我踏马来了 | ✅ 中文金句 |
| 四川人人都自称老子 | 老子 | ✅ 长文中有独特金句 |
| Super Cycle incoming | 超级周期 | ✅ 新颖概念 |
| "蜉蝣也可撼树"→修改为"蚍蜉也可撼树，蜉蝣也可争光" | 蚍蜉 | ✅ 修改推文，差异词就是meme爆点 |
| 🙏（回复他人） | （空） | ❌ 纯emoji，无实质内容 |
| 👀（回复他人引用推文） | （空） | ❌ 纯emoji，不是meme |
| good idea.（回复他人建议） | （空） | ❌ 日常短回复，不是meme |
| Never too late. I started Binance at 40.（回复他人提问） | （空） | ❌ 励志鸡汤/常见英文俗语，不是meme |
| Intelligence is probably 10% or less in weight.（回复讨论） | （空） | ❌ 正经观点讨论，不是meme |
| Binance holds the largest % of stablecoins...🤷‍♂️ | （空） | ❌ 正经行业讨论 |
| Was a little scared on the horse. (Not AI) | （空） | ❌ 个人生活分享，无梗 |
| For most CEX, there is a balance somewhere... | （空） | ❌ 正经行业讨论回复 |
| I don't know any details of your case with USDT... | （空） | ❌ 回复他人解释情况 |
| DEX listing all tokens is good. CEX listing bad? 🤷‍♂️ | （空） | ❌ 行业观点讨论 |
| Not saying we are perfect, but smart people... | （空） | ❌ 正经澄清/辩护 |
| For newcomers, Binance represents...they see FUD | （空） | ❌ 行业通用术语FUD |
| #BNB（引用BNB Futures上线ICE消息） | （空） | ❌ 主流代币名BNB |
| "成功公式：每天110%-130%，坚持30年"（引用他人采访总结） | （空） | ❌ 鸡汤/人生建议+数字百分比 |
| 8046687368（推文URL中的数字） | （空） | ❌ 纯数字串/URL片段，绝不是meme |
| 我的新书叫《Erta Moon》 | Erta Moon | ✅ 📚 CZ公布书名 = 超级金狗！书名号强调 |
| My new book is called "Binance Rising" | Binance Rising | ✅ 📚 CZ公布书名 = 超级金狗！引号强调 |
| 书快出了，敬请期待 | （空） | ❌ 只是泛泛提到新书，没有透露具体书名 |
| CZ回复"🔥"（原始推文含meme梗图/关键词） | （空） | ❌ 回复只是简单emoji，不算认可meme |
| CZ回复"This is the way! xxx to the moon"（原始推文提到独特梗词） | xxx | ✅ CZ明确呼应/放大了原始推文的梗 |
| 马年快乐！ | 马年快乐 | ✅ 中文生肖祝福 = 金狗meme！CZ用中文发节日祝福必做 |
| 龙年大吉 | 龙年大吉 | ✅ 中文生肖祝福 = 金狗meme |
| 新年快乐 | 新年快乐 | ✅ 中文节日祝福 = 金狗meme |
| Happy New Year! | （空） | ❌ 英文通用祝福，不是meme |
| Merry Christmas | （空） | ❌ 英文通用祝福，不是meme |

严格按 JSON 格式返回（无其他文字）：
{
    "predicted_meme": "金狗meme名称（最短最有冲击力的形态，去掉标点和表情）。如果不值得做meme则留空字符串",
    "token_name": "和predicted_meme完全一致，留空则空字符串",
    "token_ticker": "和predicted_meme完全一致，留空则空字符串",
    "token_description": "基于推文内容生成的代币描述，50-150字符。留空则空字符串",
    "reason": "一句话说明判断理由（无论是否提取到meme都要填写理由）"
}"""

    user_prompt = f"分析这条推文，提取meme关键词并生成代币信息：\n\n{text}"

    # 如果是回复推文，附加被回复的原始推文上下文
    if reply_parent_context:
        parent_content = reply_parent_context.get("content", "")
        parent_username = reply_parent_context.get("username", "")
        parent_url = reply_parent_context.get("url", "")
        parent_has_image = reply_parent_context.get("has_image", False)
        parent_images = reply_parent_context.get("images", [])

        user_prompt += f"\n\n💬 【被回复的原始推文】CZ/何一回复的是 @{parent_username} 的推文："
        user_prompt += f"\n原始推文内容: {parent_content[:500]}"
        if parent_url:
            user_prompt += f"\n原始推文链接: {parent_url}"
        if parent_has_image:
            user_prompt += f"\n原始推文包含 {len(parent_images)} 张图片"
            user_prompt += f"\n（注意：原始推文的图片可能包含 meme 梗图或关键视觉元素）"
        user_prompt += f"\n请结合 CZ/何一的回复内容和被回复的原始推文一起分析，判断是否有 meme 潜力。"

    # 如果检测到是修改推文，附加上下文给 AI
    if revised_context:
        old_content = revised_context.get("old_content", "")
        diff = revised_context.get("diff", "")
        similarity = revised_context.get("similarity", 0)
        user_prompt += f"\n\n⚠️ 【修改推文检测】此推文是作者短时间内对之前推文的修改版本（相似度 {similarity:.0%}）。"
        user_prompt += f"\n原推文: {old_content[:300]}"
        user_prompt += f"\n差异/新增内容: {diff}"
        user_prompt += f"\n请重点关注修改/新增的部分，这很可能是作者刻意修改的 meme 爆点词汇！"

    # 模型回退列表：主模型失败时依次尝试备选模型
    models_to_try = [AI_MODEL]
    fallback_models = ["GPT-5-Chat", "gemini-2.5-pro", "Claude-HaiKu-4.5", "gemini-2.5-flash", "GPT-4o-2024-08-06"]
    for m in fallback_models:
        if m != AI_MODEL and m not in models_to_try:
            models_to_try.append(m)

    for model in models_to_try:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"尝试使用模型 {model} (第{attempt}次)")
                response = ai_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                )
                content = response.choices[0].message.content
                match = re.search(r'\{.*\}', content, re.DOTALL)
                if match:
                    result = json.loads(match.group())
                    # ticker 保留原文（中文 meme 不做大写转换）
                    if 'token_ticker' in result:
                        result['token_ticker'] = result['token_ticker'].strip()
                    logger.info(f"AI 分析结果 (模型: {model}): {result}")
                    return result
                logger.warning(f"AI 返回内容无法解析为 JSON: {content[:200]}")
                return None
            except json.JSONDecodeError as e:
                logger.error(f"JSON 解析错误: {e}, 响应: {content[:200]}")
                return None
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "quota" in error_str.lower() or "rate" in error_str.lower():
                    wait_time = 20
                    if attempt < MAX_RETRIES:
                        logger.warning(f"AI API 限流 (429)，第{attempt}次重试，等待 {wait_time} 秒...")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.warning(f"模型 {model} 限流，尝试下一个模型...")
                        break
                elif "500" in error_str or "internal" in error_str.lower():
                    logger.warning(f"模型 {model} 服务端错误 (500)，尝试下一个模型...")
                    break
                else:
                    logger.error(f"AI 分析错误 (模型: {model}): {e}")
                    if attempt < MAX_RETRIES:
                        time.sleep(2)
                        continue
                    break

    logger.error(f"所有模型均失败: {models_to_try}")
    return None


async def analyze_tweet_for_meme(text, revised_context=None, reply_parent_context=None):
    """异步分析推文"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _analyze_tweet_for_meme_sync, text, revised_context, reply_parent_context
    )


# --- Four.meme 代币创建 ---

# Four.meme API 基础 URL
FOURMEME_API_BASE = "https://four.meme/meme-api/v1"

# WBNB 合约地址
WBNB_ADDRESS = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"

# BNB Logo URL (Four.meme 使用)
BNB_LOGO_URL = "https://static.four.meme/market/68b871b6-96f7-408c-b8d0-388d804b34275092658264263839640.png"

# Four.meme TokenManager3 合约 ABI（正确的 createToken 函数签名）
FOURMEME_ABI = json.loads('''[
    {
        "inputs": [
            {"internalType": "bytes", "name": "args", "type": "bytes"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"}
        ],
        "name": "createToken",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function"
    }
]''')

# Four.meme TokenManager3 合约 ABI（buyToken 函数签名）
FOURMEME_BUY_ABI = json.loads('''[
    {
        "inputs": [
            {"internalType": "bytes", "name": "args", "type": "bytes"},
            {"internalType": "bytes", "name": "signature", "type": "bytes"}
        ],
        "name": "buyToken",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function"
    }
]''')


class FourMemeAPI:
    """Four.meme 平台 API 客户端"""

    def __init__(self, private_key: str, wallet_address: str, rpc_url: str = ""):
        self.private_key = private_key
        self.wallet_address = Web3.to_checksum_address(wallet_address)
        self.rpc_url = rpc_url or BSC_RPC_URL
        self.access_token = ""
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.account = Account.from_key(self.private_key)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Origin': 'https://four.meme',
            'Referer': 'https://four.meme/',
        })

    def _api_url(self, path: str) -> str:
        return f"{FOURMEME_API_BASE}{path}"

    # ========== Step 1: 认证流程 ==========

    def get_nonce(self) -> str:
        """获取登录 nonce"""
        url = self._api_url("/private/user/nonce/generate")
        payload = {
            "accountAddress": self.wallet_address,
            "networkCode": "BSC",
            "verifyType": "LOGIN"
        }
        logger.info(f"获取 nonce: {self.wallet_address}")
        resp = self.session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0 and data.get("code") != 200:
            raise Exception(f"获取 nonce 失败: {data}")
        nonce = data.get("data", "")
        logger.info(f"获取 nonce 成功: {nonce[:20]}...")
        return nonce

    def sign_message(self, message: str) -> str:
        """使用钱包私钥签署消息"""
        msg = encode_defunct(text=message)
        signed = self.w3.eth.account.sign_message(msg, private_key=self.private_key)
        signature = signed.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        return signature

    def login(self) -> str:
        """完整登录流程：获取 nonce → 签名 → 验证"""
        # 获取 nonce
        nonce = self.get_nonce()

        # 签署消息
        message = f"You are sign in Meme {nonce}"
        signature = self.sign_message(message)
        logger.info(f"消息已签名: {signature[:20]}...")

        # 发送登录请求
        url = self._api_url("/private/user/login/dex")
        payload = {
            "inviteCode": "",
            "langType": "EN",
            "loginIp": "",
            "region": "WEB",
            "verifyInfo": {
                "address": self.wallet_address,
                "networkCode": "BSC",
                "signature": signature,
                "verifyType": "LOGIN"
            },
            "walletName": "MetaMask"
        }

        resp = self.session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0 and data.get("code") != 200:
            raise Exception(f"登录失败: {data}")

        self.access_token = data.get("data", "")
        logger.info(f"登录成功，获取 accessToken: {self.access_token[:20]}...")
        return self.access_token

    # ========== Step 2: 上传图片 ==========

    def upload_image(self, image_path: str) -> str:
        """上传代币 logo 图片到 Four.meme 服务器"""
        if not self.access_token:
            raise Exception("未登录，请先调用 login()")

        url = self._api_url("/private/token/upload")

        # 检测 MIME 类型
        mime_type, _ = mimetypes.guess_type(image_path)
        if not mime_type:
            mime_type = "image/jpeg"

        filename = os.path.basename(image_path)

        with open(image_path, 'rb') as f:
            files = {
                'file': (filename, f, mime_type)
            }
            headers = {
                'Meme-Web-Access': self.access_token,
            }
            logger.info(f"上传图片: {image_path} ({mime_type})")
            resp = self.session.post(url, files=files, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != 0 and data.get("code") != 200:
            raise Exception(f"图片上传失败: {data}")

        image_url = data.get("data", "")
        logger.info(f"图片上传成功: {image_url}")
        return image_url

    # ========== Step 3: 创建代币数据 ==========

    def get_raised_token_config(self, symbol: str = "BNB") -> dict:
        """从 API 获取最新的 raisedToken 配置"""
        url = self._api_url("/public/config")
        resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"获取配置失败: {data}")
            return {}
        configs = data.get("data", [])
        for cfg in configs:
            if cfg.get("symbol") == symbol and cfg.get("status") == "PUBLISH":
                logger.info(f"获取 raisedToken 配置: totalBAmount={cfg.get('totalBAmount')}, deployCost={cfg.get('deployCost')}")
                return cfg
        # 返回第一个 PUBLISH 的
        for cfg in configs:
            if cfg.get("status") == "PUBLISH":
                return cfg
        return configs[0] if configs else {}

    def create_token_data(self, name: str, symbol: str, description: str,
                          image_url: str, twitter: str = "",
                          telegram: str = "", website: str = "",
                          pre_sale: float = 0) -> dict:
        """
        向 Four.meme API 提交代币元数据，获取合约调用参数

        Args:
            pre_sale: 预购金额 (BNB)，默认使用 .env 中的 PRE_SALE_AMOUNT

        返回: {createArg, signature, tokenId, bamount, tamount, ...}
        """
        if pre_sale <= 0:
            pre_sale = PRE_SALE_AMOUNT
        if not self.access_token:
            raise Exception("未登录，请先调用 login()")

        # 获取最新的 raisedToken 配置
        raised_config = self.get_raised_token_config("BNB")
        total_b_amount = raised_config.get("totalBAmount", "18")
        deploy_cost = raised_config.get("deployCost", "0")
        buy_fee = raised_config.get("buyFee", "0.01")
        min_trade_fee = raised_config.get("minTradeFee", "0")
        b0_amount = raised_config.get("b0Amount", "8")
        sale_rate = raised_config.get("saleRate", "0.8")
        logo_url = raised_config.get("logoUrl", BNB_LOGO_URL)

        url = self._api_url("/private/token/create")
        launch_time = int(time.time() * 1000)

        payload = {
            "name": name,
            "shortName": symbol,
            "desc": description,
            "imgUrl": image_url,
            "launchTime": launch_time,
            "label": "Meme",
            "clickFun": False,
            "funGroup": False,
            "lpTradingFee": 0.0025,
            "preSale": pre_sale,
            "raisedAmount": int(total_b_amount),
            "reserveRate": 0,
            "saleRate": float(sale_rate),
            "totalSupply": 1000000000,
            "symbol": "BNB",
            "twitterUrl": twitter,
            "telegramUrl": telegram,
            "website": website,
            "raisedToken": raised_config if raised_config else {
                "b0Amount": b0_amount,
                "buyFee": buy_fee,
                "buyTokenLink": "https://pancakeswap.finance/swap",
                "deployCost": deploy_cost,
                "logoUrl": logo_url,
                "minTradeFee": min_trade_fee,
                "nativeSymbol": "BNB",
                "networkCode": "BSC",
                "platform": "MEME",
                "reservedNumber": 10,
                "saleRate": sale_rate,
                "sellFee": "0.01",
                "status": "PUBLISH",
                "symbol": "BNB",
                "symbolAddress": WBNB_ADDRESS,
                "totalAmount": "1000000000",
                "totalBAmount": total_b_amount,
                "tradeLevel": ["0.1", "0.5", "1"]
            }
        }

        headers = {
            'Meme-Web-Access': self.access_token,
            'Content-Type': 'application/json',
        }

        logger.info(f"创建代币数据: {name} ({symbol}), preSale={pre_sale} BNB")
        resp = self.session.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0 and data.get("code") != 200:
            raise Exception(f"创建代币数据失败: {data}")

        result = data.get("data", {})
        logger.info(f"代币数据创建成功: tokenId={result.get('tokenId')}")
        logger.info(f"  createArg: {str(result.get('createArg', ''))[:60]}...")
        logger.info(f"  signature: {str(result.get('signature', ''))[:60]}...")

        # 计算 value: preSale + deployCost + max(preSale*buyFee, minTradeFee)
        fee = pre_sale * float(buy_fee)
        if fee < float(min_trade_fee):
            fee = float(min_trade_fee)
        value_bnb = pre_sale + float(deploy_cost) + fee
        result['_value_bnb'] = value_bnb
        result['_pre_sale'] = pre_sale
        logger.info(f"  计算 value: preSale={pre_sale} + deployCost={deploy_cost} + fee={fee:.6f} = {value_bnb:.6f} BNB")

        return result

    # ========== Step 4: 调用合约 ==========

    def create_token_on_chain(self, create_arg: str, signature: str, value: int = 0) -> dict:
        """
        调用 Four.meme TokenManager3 合约的 createToken 函数

        Args:
            create_arg: API 返回的编码参数 (hex)
            signature: API 返回的签名 (hex)
            value: 随交易发送的 BNB (wei)

        Returns:
            {tx_hash, token_address, ...}
        """
        if not self.w3.is_connected():
            raise Exception("无法连接 BSC 网络")

        contract_address = Web3.to_checksum_address(FOURMEME_TOKEN_MANAGER)
        contract = self.w3.eth.contract(address=contract_address, abi=FOURMEME_ABI)

        # 确保参数是 bytes 格式
        if isinstance(create_arg, str):
            if create_arg.startswith("0x"):
                create_arg_bytes = bytes.fromhex(create_arg[2:])
            else:
                create_arg_bytes = bytes.fromhex(create_arg)
        else:
            create_arg_bytes = create_arg

        if isinstance(signature, str):
            if signature.startswith("0x"):
                signature_bytes = bytes.fromhex(signature[2:])
            else:
                signature_bytes = bytes.fromhex(signature)
        else:
            signature_bytes = signature

        wallet_address = self.wallet_address
        nonce = self.w3.eth.get_transaction_count(wallet_address)

        # 构建交易
        tx = contract.functions.createToken(
            create_arg_bytes,
            signature_bytes
        ).build_transaction({
            'from': wallet_address,
            'value': value,
            'gas': 2000000,  # 默认较高值，createToken 需要约 1.4M-1.6M gas
            'gasPrice': self.w3.to_wei(3, 'gwei'),
            'nonce': nonce,
            'chainId': 56
        })

        # 估算 gas
        try:
            estimated_gas = self.w3.eth.estimate_gas(tx)
            tx['gas'] = int(estimated_gas * 1.3)
            logger.info(f"估算 Gas: {estimated_gas}, 使用: {tx['gas']}")
        except Exception as e:
            logger.warning(f"Gas 估算失败，使用默认值 1000000: {e}")

        # 签名并发送交易
        signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        tx_hash_hex = "0x" + tx_hash.hex() if not tx_hash.hex().startswith("0x") else tx_hash.hex()
        logger.info(f"交易已发送: {tx_hash_hex}")

        # 等待确认
        logger.info("等待交易确认...")
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt['status'] == 1:
            token_address = ""
            if receipt['logs']:
                token_address = receipt['logs'][0]['address']
                logger.info(f"代币创建成功! 代币地址: {token_address}")
            return {
                "success": True,
                "tx_hash": tx_hash_hex,
                "token_address": token_address,
            }
        else:
            logger.error(f"交易失败! receipt: {dict(receipt)}")
            return {
                "success": False,
                "error": "交易执行失败 (reverted)",
                "tx_hash": tx_hash_hex,
            }

    # ========== Step 5: 买入代币（内盘） ==========

    def get_buy_token_data(self, token_address: str, amount_bnb: float) -> dict:
        """
        通过 Four.meme API 获取买入代币的合约调用参数

        Args:
            token_address: 代币合约地址
            amount_bnb: 买入金额（BNB）

        Returns:
            API 返回的买入参数（包含 createArg, signature 等）
        """
        if not self.access_token:
            raise Exception("未登录，请先调用 login()")

        url = self._api_url("/private/token/buy")
        payload = {
            "tokenAddress": Web3.to_checksum_address(token_address),
            "raisedTokenSymbol": "BNB",
            "payAmount": str(amount_bnb),
        }
        headers = {
            'Meme-Web-Access': self.access_token,
            'Content-Type': 'application/json',
        }

        logger.info(f"获取买入参数: token={token_address[:10]}..., amount={amount_bnb} BNB")
        resp = self.session.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0 and data.get("code") != 200:
            raise Exception(f"获取买入参数失败: {data}")

        result = data.get("data", {})
        logger.info(f"买入参数获取成功")
        return result

    def buy_token_on_chain(self, token_address: str, amount_bnb: float) -> dict:
        """
        在 Four.meme 内盘买入代币

        实现方式：直接向 TokenManager 合约发送 BNB 进行买入
        Four.meme 内盘的买入是通过向合约转账 BNB 实现的

        Args:
            token_address: 代币合约地址
            amount_bnb: 买入金额（BNB）

        Returns:
            {"success": bool, "tx_hash": str, ...}
        """
        if not self.w3.is_connected():
            raise Exception("无法连接 BSC 网络")

        try:
            # 获取买入参数
            buy_data = self.get_buy_token_data(token_address, amount_bnb)

            # 获取费率
            raised_config = self.get_raised_token_config("BNB")
            buy_fee = float(raised_config.get("buyFee", "0.01"))
            min_trade_fee = float(raised_config.get("minTradeFee", "0"))

            # 计算总费用: amount + max(amount*buyFee, minTradeFee)
            fee = amount_bnb * buy_fee
            if fee < min_trade_fee:
                fee = min_trade_fee
            total_bnb = amount_bnb + fee

            # 如果 API 返回了 createArg 和 signature，走合约调用
            create_arg = buy_data.get("createArg", "")
            signature = buy_data.get("signature", "")

            if create_arg and signature:
                # 通过合约方法买入
                contract_address = Web3.to_checksum_address(FOURMEME_TOKEN_MANAGER)
                contract = self.w3.eth.contract(address=contract_address, abi=FOURMEME_BUY_ABI)

                if isinstance(create_arg, str):
                    create_arg_bytes = bytes.fromhex(create_arg[2:] if create_arg.startswith("0x") else create_arg)
                else:
                    create_arg_bytes = create_arg

                if isinstance(signature, str):
                    signature_bytes = bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)
                else:
                    signature_bytes = signature

                value_wei = self.w3.to_wei(total_bnb, 'ether')
                nonce = self.w3.eth.get_transaction_count(self.wallet_address)

                tx = contract.functions.buyToken(
                    create_arg_bytes,
                    signature_bytes
                ).build_transaction({
                    'from': self.wallet_address,
                    'value': value_wei,
                    'gas': 500000,
                    'gasPrice': self.w3.to_wei(3, 'gwei'),
                    'nonce': nonce,
                    'chainId': 56
                })
            else:
                # 如果 API 返回的是其他格式，回退到直接转账方式
                # 使用 Four.meme 合约的 fallback/receive 函数
                # 构建 buyToken(address) 函数调用数据
                token_addr_bytes = bytes.fromhex(Web3.to_checksum_address(token_address)[2:])
                # function selector for buyToken(address) = 0x... (需要具体确认)
                # 改用低级调用方式
                value_wei = self.w3.to_wei(total_bnb, 'ether')
                nonce = self.w3.eth.get_transaction_count(self.wallet_address)
                contract_address = Web3.to_checksum_address(FOURMEME_TOKEN_MANAGER)

                # 编码 buyToken(address token) 的调用数据
                fn_selector = Web3.keccak(text="buyToken(address)")[:4]
                padded_addr = bytes(12) + bytes.fromhex(Web3.to_checksum_address(token_address)[2:])
                call_data = fn_selector + padded_addr

                tx = {
                    'from': self.wallet_address,
                    'to': contract_address,
                    'value': value_wei,
                    'gas': 500000,
                    'gasPrice': self.w3.to_wei(3, 'gwei'),
                    'nonce': nonce,
                    'chainId': 56,
                    'data': call_data,
                }

            # 估算 gas
            try:
                estimated_gas = self.w3.eth.estimate_gas(tx)
                tx['gas'] = int(estimated_gas * 1.3)
                logger.info(f"买入估算 Gas: {estimated_gas}, 使用: {tx['gas']}")
            except Exception as e:
                logger.warning(f"买入 Gas 估算失败，使用默认值: {e}")

            # 签名并发送
            signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_hash_hex = "0x" + tx_hash.hex() if not tx_hash.hex().startswith("0x") else tx_hash.hex()
            logger.info(f"买入交易已发送: {tx_hash_hex}")

            # 等待确认
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt['status'] == 1:
                logger.info(f"买入成功! wallet={self.wallet_address[:10]}..., tx={tx_hash_hex}")
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "wallet": self.wallet_address,
                    "amount_bnb": amount_bnb,
                }
            else:
                logger.error(f"买入交易失败 (reverted): {tx_hash_hex}")
                return {
                    "success": False,
                    "error": "买入交易执行失败 (reverted)",
                    "tx_hash": tx_hash_hex,
                    "wallet": self.wallet_address,
                }
        except Exception as e:
            logger.error(f"买入代币失败 (wallet={self.wallet_address[:10]}...): {e}")
            return {
                "success": False,
                "error": str(e),
                "wallet": self.wallet_address,
            }

    # ========== 完整创建流程 ==========

    def create_token(self, name: str, symbol: str, description: str,
                     image_path: str = "", twitter: str = "",
                     telegram: str = "", website: str = "",
                     pre_sale: float = 0) -> dict:
        """
        完整的代币创建流程：
        1. 登录 Four.meme
        2. 上传图片
        3. 创建代币数据（获取合约参数）
        4. 调用合约创建代币

        Args:
            pre_sale: 预购金额 (BNB)，默认使用 .env 中的 PRE_SALE_AMOUNT
        """
        if pre_sale <= 0:
            pre_sale = PRE_SALE_AMOUNT
        try:
            # Step 1: 登录
            logger.info("=" * 50)
            logger.info("Step 1: 登录 Four.meme...")
            self.login()

            # Step 2: 上传图片
            image_url = ""
            if image_path and os.path.exists(image_path):
                logger.info("Step 2: 上传代币图片...")
                image_url = self.upload_image(image_path)
            else:
                logger.info("Step 2: 跳过图片上传（无图片）")

            # Step 3: 创建代币数据
            logger.info("Step 3: 创建代币数据...")
            token_data = self.create_token_data(
                name=name,
                symbol=symbol,
                description=description,
                image_url=image_url,
                twitter=twitter,
                telegram=telegram,
                website=website,
                pre_sale=pre_sale,
            )

            create_arg = token_data.get("createArg", "")
            signature = token_data.get("signature", "")
            token_id = token_data.get("tokenId", "")
            value_bnb = token_data.get("_value_bnb", 0)

            if not create_arg or not signature:
                raise Exception(f"API 未返回有效的 createArg/signature: {token_data}")

            # 将 BNB 转为 wei
            value_wei = self.w3.to_wei(value_bnb, 'ether')
            logger.info(f"交易 value: {value_bnb:.6f} BNB ({value_wei} wei)")

            # Step 4: 调用合约
            logger.info("Step 4: 调用合约创建代币...")
            result = self.create_token_on_chain(
                create_arg=create_arg,
                signature=signature,
                value=value_wei,
            )

            result["token_name"] = name
            result["token_ticker"] = symbol
            result["token_id"] = token_id
            result["image_url"] = image_url
            return result

        except Exception as e:
            logger.error(f"创建代币失败: {e}")
            return {"success": False, "error": str(e)}


def _create_token_on_fourmeme_sync(token_name: str, token_ticker: str,
                                    description: str, image_path: str = "",
                                    twitter: str = "", telegram: str = "",
                                    website: str = "", pre_sale: float = 0) -> dict:
    """
    通过 Four.meme API + 合约创建代币（同步）

    完整流程：
    1. 钱包签名登录 Four.meme
    2. 上传图片到 Four.meme 服务器
    3. 提交代币元数据，获取合约参数
    4. 调用 TokenManager3 合约的 createToken(bytes, bytes)

    Args:
        pre_sale: 预购金额 (BNB)，默认使用 .env 中的 PRE_SALE_AMOUNT
    """
    if pre_sale <= 0:
        pre_sale = PRE_SALE_AMOUNT
    if not WALLET_PRIVATE_KEY or not WALLET_ADDRESS:
        logger.error("钱包未配置，无法创建代币")
        return {"success": False, "error": "钱包未配置"}

    api = FourMemeAPI(
        private_key=WALLET_PRIVATE_KEY,
        wallet_address=WALLET_ADDRESS,
        rpc_url=BSC_RPC_URL,
    )

    return api.create_token(
        name=token_name,
        symbol=token_ticker,
        description=description,
        image_path=image_path,
        twitter=twitter,
        telegram=telegram,
        website=website,
        pre_sale=pre_sale,
    )


def _bundle_buy_single_wallet(wallet_info: dict, token_address: str,
                                amount_bnb: float, rpc_url: str = "") -> dict:
    """单个副钱包买入代币（用于多线程并发）"""
    wallet_addr = wallet_info['address']
    wallet_pk = wallet_info['private_key']

    logger.info(f"捆绑买入: wallet={wallet_addr[:10]}..., amount={amount_bnb} BNB")

    try:
        api = FourMemeAPI(
            private_key=wallet_pk,
            wallet_address=wallet_addr,
            rpc_url=rpc_url or BSC_RPC_URL,
        )
        # 登录
        api.login()
        # 买入
        result = api.buy_token_on_chain(token_address, amount_bnb)
        return result
    except Exception as e:
        logger.error(f"捆绑买入失败 (wallet={wallet_addr[:10]}...): {e}")
        return {
            "success": False,
            "error": str(e),
            "wallet": wallet_addr,
        }


def _bundle_buy_all_wallets(token_address: str, amount_bnb: float,
                             wallets: list = None) -> list:
    """
    所有副钱包并发买入代币

    Args:
        token_address: 代币合约地址
        amount_bnb: 每个钱包的买入金额（BNB）
        wallets: 钱包列表，默认使用 BUNDLE_WALLETS

    Returns:
        [{"success": bool, "wallet": str, "tx_hash": str, ...}, ...]
    """
    if wallets is None:
        wallets = BUNDLE_WALLETS

    if not wallets:
        logger.info("无副钱包配置，跳过捆绑买入")
        return []

    logger.info(f"开始捆绑买入: {len(wallets)} 个副钱包, 每个 {amount_bnb} BNB")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    with ThreadPoolExecutor(max_workers=min(len(wallets), 10)) as executor:
        futures = {}
        for w in wallets:
            future = executor.submit(
                _bundle_buy_single_wallet,
                w, token_address, amount_bnb, BSC_RPC_URL
            )
            futures[future] = w['address']

        for future in as_completed(futures):
            wallet_addr = futures[future]
            try:
                result = future.result()
                results.append(result)
                status = "✅" if result.get("success") else "❌"
                logger.info(f"  {status} {wallet_addr[:10]}... - {result.get('tx_hash', result.get('error', ''))[:40]}")
            except Exception as e:
                logger.error(f"  ❌ {wallet_addr[:10]}... - 异常: {e}")
                results.append({
                    "success": False,
                    "error": str(e),
                    "wallet": wallet_addr,
                })

    success_count = sum(1 for r in results if r.get("success"))
    logger.info(f"捆绑买入完成: {success_count}/{len(results)} 成功")

    return results


def _create_token_with_bundle_sync(token_name: str, token_ticker: str,
                                     description: str, image_path: str = "",
                                     twitter: str = "", telegram: str = "",
                                     website: str = "", pre_sale: float = 0) -> dict:
    """
    创建代币 + 捆绑买入（同步）

    流程：
    1. 主钱包创建代币（含主钱包的 preSale 初始买入）
    2. 如果启用捆绑买入且有副钱包配置，所有副钱包并发买入

    Returns:
        {"success": bool, "tx_hash": str, "token_address": str,
         "bundle_results": [{"success": bool, "wallet": str, ...}, ...]}
    """
    # Step 1: 主钱包创建代币
    result = _create_token_on_fourmeme_sync(
        token_name, token_ticker, description, image_path,
        twitter=twitter, telegram=telegram, website=website,
        pre_sale=pre_sale
    )

    # Step 2: 捆绑买入
    bundle_results = []
    if result.get("success") and ENABLE_BUNDLE_BUY and BUNDLE_WALLETS:
        token_address = result.get("token_address", "")
        if token_address:
            logger.info(f"代币创建成功，开始捆绑买入...")
            buy_amount = pre_sale if pre_sale > 0 else PRE_SALE_AMOUNT
            bundle_results = _bundle_buy_all_wallets(token_address, buy_amount)
        else:
            logger.warning("代币地址未获取到，跳过捆绑买入")

    result["bundle_results"] = bundle_results
    return result


async def create_token_on_fourmeme(token_name: str, token_ticker: str,
                                    description: str, image_path: str = "",
                                    twitter: str = "") -> dict:
    """异步创建代币（支持捆绑买入）"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        partial(_create_token_with_bundle_sync,
                token_name, token_ticker, description, image_path,
                twitter=twitter)
    )


# --- Telegram 客户端（延迟初始化，仅监控模式下创建）---
client = None


def _init_telegram_client():
    """初始化 Telegram 客户端"""
    global client
    if API_ID and API_HASH:
        client = TelegramClient('fourmeme_tools', API_ID, API_HASH)
    else:
        logger.error("Telegram 未配置 (TG_API_ID / TG_API_HASH)")
        return None
    return client


async def message_handler(event):
    """处理 Telegram 消息：监控推文 → AI 分析 → 创建代币 → 飞书通知"""
    try:
        chat_id = str(event.chat_id)
        text = event.message.text

        if not text:
            return

        # === 推特监控群组 ===
        if TWITTER_GROUP_ID and TWITTER_GROUP_ID in chat_id:
            # 解析推文消息
            tweet_info = parse_tweet_message(text)
            if not tweet_info:
                return

            tweet_type = tweet_info['type']
            username = tweet_info['username']
            nickname = tweet_info['nickname']
            content = tweet_info['content']
            tweet_url = tweet_info.get('url', '')
            has_image = tweet_info.get('has_image', False)
            image_urls = tweet_info.get('image_urls', [])
            reply_to_url = tweet_info.get('reply_to_url', '')

            type_label = "发布推文" if tweet_type == 'publish' else "回复推文"
            logger.info(f"检测到 {nickname}(@{username}) {type_label}: {content[:100]}...")

            # 下载推文图片（优先级：消息中图片直链 > fxtwitter > Telegram附件）
            image_path = ""

            # 优先级1：从 Debot 消息中提取的 pbs.twimg.com 图片直链
            if image_urls:
                img_url = image_urls[0]
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                ext = ".png" if ".png" in img_url else ".jpg"
                image_path = download_image_from_url(img_url, f"tweet_{timestamp}{ext}")
                if image_path:
                    logger.info(f"从 Debot 消息提取图片直链下载成功: {image_path}")

            # 优先级2：通过 fxtwitter API 获取高清原图
            if not image_path and tweet_url:
                try:
                    fx_info = fetch_tweet(tweet_url)
                    if fx_info and fx_info.get('has_image'):
                        img_url = fx_info['images'][0]
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        ext = ".png" if ".png" in img_url else ".jpg"
                        image_path = download_image_from_url(img_url, f"tweet_{timestamp}{ext}")
                        if image_path:
                            logger.info(f"通过 fxtwitter 获取推文原图: {image_path}")
                except Exception as e:
                    logger.warning(f"fxtwitter 获取图片失败: {e}")

            # 优先级3：从 Telegram 消息附件下载
            if not image_path and has_image:
                image_path = await download_tweet_image(event)

            # 回复推文：获取被回复的原始推文内容和图片
            reply_parent_info = {}
            if tweet_type == 'reply' and reply_to_url:
                logger.info(f"回复推文，尝试获取被回复的原始推文: {reply_to_url}")
                try:
                    reply_parent_info = fetch_reply_parent_tweet(reply_to_url)
                    if reply_parent_info:
                        parent_content = reply_parent_info.get('content', '')
                        logger.info(f"成功获取原始推文内容: {parent_content[:100]}...")
                    else:
                        logger.warning(f"未能获取被回复的原始推文内容")
                except Exception as e:
                    logger.warning(f"获取被回复原始推文失败: {e}")
            elif tweet_type == 'reply' and not reply_to_url and tweet_url:
                # 如果从消息中没提取到被回复推文URL，尝试通过 FxTwitter API 从回复推文本身获取
                logger.info(f"回复推文未提取到原始推文URL，尝试通过 FxTwitter 获取...")
                try:
                    match = re.match(r'https?://(?:x\.com|twitter\.com)/(\w+)/status/(\d+)', tweet_url.strip())
                    if match:
                        reply_username = match.group(1)
                        reply_status_id = match.group(2)
                        api_url = f"https://api.fxtwitter.com/{reply_username}/status/{reply_status_id}"
                        resp = requests.get(api_url, timeout=15, headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        })
                        resp.raise_for_status()
                        data = resp.json()
                        if data.get("code") == 200:
                            tweet_data = data.get("tweet", {})
                            # FxTwitter 返回 replying_to 或 in_reply_to_screen_name
                            replying_to = tweet_data.get("replying_to") or tweet_data.get("replying_to_status")
                            if replying_to and isinstance(replying_to, str) and replying_to.startswith("http"):
                                reply_parent_info = fetch_reply_parent_tweet(replying_to)
                                if reply_parent_info:
                                    logger.info(f"通过 FxTwitter 获取到原始推文: {reply_parent_info.get('content', '')[:100]}...")
                except Exception as e:
                    logger.warning(f"通过 FxTwitter 获取被回复原始推文失败: {e}")

            # CZ 新书关键词检测：检查推文是否涉及新书/书名相关内容
            _book_keywords = [
                '书名', '新书', '我的书', '出书', '写书', '这本书', '那本书',
                'book title', 'book name', 'my book', 'new book', 'the book',
                'autobiography', 'memoir', '《',
            ]
            content_lower = content.lower()
            is_book_related = any(kw in content_lower for kw in _book_keywords)
            # 也检查被回复的原始推文是否涉及新书
            if not is_book_related and reply_parent_info:
                parent_content_lower = reply_parent_info.get('content', '').lower()
                is_book_related = any(kw in parent_content_lower for kw in _book_keywords)
            if is_book_related:
                logger.info(f"📚 检测到新书相关关键词！推文内容: {content[:100]}...")

            # 中文节日/生肖/祝福关键词检测：CZ用中文发的节日祝福是金狗meme
            _chinese_festival_keywords = [
                '快乐', '大吉', '大利', '新年', '春节', '元宵', '中秋', '端午', '国庆',
                '除夕', '拜年', '恭喜', '祝福', '吉祥', '如意',
                '鼠年', '牛年', '虎年', '兔年', '龙年', '蛇年',
                '马年', '羊年', '猴年', '鸡年', '狗年', '猪年',
            ]
            is_chinese_festival = any(kw in content for kw in _chinese_festival_keywords)
            if is_chinese_festival:
                logger.info(f"🧧 检测到中文节日/生肖祝福关键词！推文内容: {content[:100]}...")

            # AI 分析
            predicted_meme = ""
            token_name = ""
            token_ticker = ""
            token_description = ""
            reason = ""

            # 检测是否为修改推文（仅对"发布推文"做检测，回复推文不做）
            similar_info = {"is_similar": False}
            if tweet_type == 'publish':
                similar_info = detect_similar_tweet(username, content)
            is_revised_tweet = similar_info.get("is_similar", False)
            tweet_diff = similar_info.get("diff", "")
            if is_revised_tweet:
                logger.info(f"🔄 检测到修改推文！差异内容: '{tweet_diff}', 相似度: {similar_info.get('similarity', 0):.2f}")

            # 快速过滤：纯 emoji / 极短无实质内容的推文，跳过 AI 分析
            stripped_content = content.strip()
            # 去掉所有 emoji、表情符号、空白后的纯文字
            text_only = re.sub(
                r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
                r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251'
                r'\U0001f900-\U0001f9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF'
                r'\U00002600-\U000026FF\U0000FE00-\U0000FE0F\U0000200D\U00002640'
                r'\U00002642\U0000200B-\U0000200F\U0000FE0F\U00020000-\U0002FA1F]+',
                '', stripped_content
            ).strip() if stripped_content else ""

            # 回复推文快速过滤：常见短回复/日常用语直接跳过，不调用AI
            # ⚠️ 但如果涉及新书关键词 或 被回复的原始推文有实质内容，不跳过
            _skip_reply_patterns = [
                r'^good\s*(idea|point|one|job|work|move|call|stuff)',
                r'^great\s*(idea|point|job|work|move)',
                r'^(agree|agreed|disagree|exactly|indeed|correct|true|false)',
                r'^(nice|cool|awesome|amazing|wonderful|beautiful|perfect)',
                r'^(thanks|thank\s*you|thx|ty|congrats|congratulations)',
                r'^(yes|no|yep|nope|yeah|nah|ok|okay|sure)',
                r'^(well\s*said|well\s*done|good\s*luck|take\s*care)',
                r'^(never\s*too\s*late|time\s*will\s*tell|we\s*shall\s*see|let\'?s\s*see|let\'?s\s*go)',
                r'^(not\s*really|i\s*think\s*so|i\s*agree|i\s*disagree|fair\s*point)',
                r'^(haha|lol|lmao|rofl|hehe)',
            ]
            is_skip_reply = False
            if tweet_type == 'reply' and text_only and not is_book_related and not is_chinese_festival:
                text_lower = text_only.lower().strip().rstrip('.!?,;:')
                for pattern in _skip_reply_patterns:
                    if re.match(pattern, text_lower, re.IGNORECASE):
                        is_skip_reply = True
                        break
                # 回复推文且内容极短（≤15个英文字符 或 ≤8个中文字符），大概率不是meme
                if not is_skip_reply and len(text_only) <= 15:
                    # 检查是否有中文
                    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in text_only)
                    if not has_cjk:
                        is_skip_reply = True

            if not text_only or len(text_only) <= 2:
                logger.info(f"推文内容为纯 emoji 或无实质文字（'{stripped_content[:50]}'），跳过 AI 分析，仅做推文告警")
                reason = "纯emoji或无实质文字内容，跳过meme解析"
            elif is_skip_reply:
                logger.info(f"回复推文快速过滤命中：'{text_only[:50]}'，跳过 AI 分析")
                reason = f"回复推文为常见日常用语（'{text_only[:30]}'），无meme价值"
            elif ENABLE_AI_ANALYSIS:
                # 构建修改推文上下文（如果检测到）
                revised_ctx = None
                if is_revised_tweet:
                    revised_ctx = {
                        "old_content": similar_info.get("old_content", ""),
                        "diff": tweet_diff,
                        "similarity": similar_info.get("similarity", 0)
                    }
                # 构建回复推文原始推文上下文（如果获取到）
                reply_parent_ctx = None
                if reply_parent_info:
                    reply_parent_ctx = {
                        "content": reply_parent_info.get("content", ""),
                        "username": reply_parent_info.get("username", ""),
                        "url": reply_parent_info.get("url", ""),
                        "images": reply_parent_info.get("images", []),
                        "has_image": reply_parent_info.get("has_image", False),
                    }
                analysis = await analyze_tweet_for_meme(
                    content, revised_context=revised_ctx, reply_parent_context=reply_parent_ctx
                )
                if analysis:
                    predicted_meme = analysis.get("predicted_meme", "")
                    token_name = analysis.get("token_name", predicted_meme)
                    token_ticker = analysis.get("token_ticker", token_name)
                    token_description = analysis.get("token_description", "")
                    reason = analysis.get("reason", "")
                    # AI 主动判定无 meme 价值（黑名单命中等）
                    if not predicted_meme or not token_name:
                        logger.info(f"AI 判定无独特 meme 价值，跳过。理由: {reason}")
                        token_name = ""
                        token_ticker = ""
                else:
                    logger.warning("AI 分析失败，仅发送推文通知")

            # 保存记录到数据库
            record = {
                'tweet_username': username,
                'tweet_nickname': nickname,
                'tweet_content': content,
                'tweet_url': tweet_url,
                'tweet_type': tweet_type,
                'token_name': token_name,
                'token_ticker': token_ticker,
                'token_description': token_description,
                'image_path': image_path,
                'ai_reason': reason,
                'status': 'analyzed'
            }
            record_id = save_token_record(record)

            # 自动创建代币（如果启用且有有效 meme）
            if ENABLE_AUTO_CREATE and token_name and token_ticker:
                # 如果没有推文图片，自动生成黄色 meme 图片
                if not image_path:
                    image_path = generate_meme_image(token_ticker)
                    logger.info(f"推文无图片，已自动生成 meme 图片: {image_path}")

                logger.info(f"开始自动创建代币: {token_name} ({token_ticker})")
                create_result = await create_token_on_fourmeme(
                    token_name, token_ticker, token_description, image_path,
                    twitter=tweet_url
                )

                if create_result.get("success"):
                    tx_hash = create_result.get("tx_hash", "")
                    token_address = create_result.get("token_address", "")
                    bundle_results = create_result.get("bundle_results", [])
                    update_token_status(record_id, "created", tx_hash, token_address)
                    logger.info(f"代币创建成功: {token_name} ({token_ticker}) - {token_address}")
                    if bundle_results:
                        success_count = sum(1 for r in bundle_results if r.get("success"))
                        logger.info(f"捆绑买入: {success_count}/{len(bundle_results)} 成功")

                    # 飞书通知：创建成功
                    post_payload = _build_feishu_post_result(
                        success=True,
                        token_name=token_name, token_ticker=token_ticker,
                        description=token_description, tweet_url=tweet_url,
                        token_address=token_address, tx_hash=tx_hash,
                        reason=reason, bundle_results=bundle_results
                    )
                    await send_feishu(post_payload)
                else:
                    error = create_result.get("error", "未知错误")
                    update_token_status(record_id, "failed")
                    logger.error(f"代币创建失败: {error}")

                    # 飞书通知：创建失败
                    post_payload = _build_feishu_post_result(
                        success=False,
                        token_name=token_name, token_ticker=token_ticker,
                        description=token_description, tweet_url=tweet_url,
                        error=error, reason=reason
                    )
                    await send_feishu(post_payload)
            elif token_name:
                # 有 meme 但未启用自动创建，发送分析结果通知
                logger.info(f"自动发币已关闭，仅通知分析结果: {token_name} ({token_ticker})")
                post_payload = _build_feishu_analysis_notify(
                    token_name=token_name, token_ticker=token_ticker,
                    description=token_description, tweet_url=tweet_url,
                    reason=reason, username=username, nickname=nickname,
                    tweet_type=tweet_type
                )
                await send_feishu(post_payload)
            else:
                # 无有效 meme（纯emoji/行业讨论/AI判定无价值等），发送推文告警
                logger.info(f"无有效 meme，发送推文告警")
                post_payload = _build_feishu_tweet_alert(
                    content=content, tweet_url=tweet_url,
                    reason=reason, username=username, nickname=nickname,
                    tweet_type=tweet_type
                )
                await send_feishu(post_payload)

    except Exception as e:
        logger.error(f"消息处理错误: {e}")


def analyze_and_create(tweet_text: str):
    """
    分析指定推文内容并创建 meme 代币

    支持两种输入:
      1. 推文 URL: python fourmeme_tools.py --tweet "https://x.com/user/status/123"
      2. 推文文本: python fourmeme_tools.py --tweet "推文内容"
    """
    init_db()

    print("=" * 70)
    print("FourMeme Tools - 推文分析 + Meme 代币创建")
    print("=" * 70)

    image_path = ""

    # 判断输入是否为推文 URL
    tweet_url_match = re.match(r'https?://(?:x\.com|twitter\.com)/\w+/status/\d+', tweet_text.strip())
    if tweet_url_match:
        # 通过 URL 获取推文内容和图片
        print(f"\n🔗 检测到推文 URL，正在获取推文数据...")
        tweet_info, image_path = fetch_tweet_image(tweet_text.strip())
        if not tweet_info:
            print("❌ 获取推文失败")
            return

        content = tweet_info['content']
        username = tweet_info['username']
        nickname = tweet_info['nickname']
        tweet_type = tweet_info['type']
        tweet_url = tweet_info.get('url', tweet_text.strip())

        print(f"[推文] {nickname}(@{username})")
        if image_path:
            print(f"[图片] 已下载推文图片: {image_path}")
        else:
            print(f"[图片] 推文无图片，稍后自动生成")
    else:
        # 解析 Telegram 格式推文（Debot.ai 格式）
        tweet_info = parse_tweet_message(tweet_text)
        if tweet_info:
            content = tweet_info['content']
            username = tweet_info['username']
            nickname = tweet_info['nickname']
            tweet_type = tweet_info['type']
            tweet_url = tweet_info.get('url', '')
            image_urls = tweet_info.get('image_urls', [])
            type_label = "发布推文" if tweet_type == 'publish' else "回复推文"
            print(f"[推文] {nickname}(@{username}) - {type_label}")

            # 从 Debot 消息提取的图片直链下载
            if image_urls:
                img_url = image_urls[0]
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                ext = ".png" if ".png" in img_url else ".jpg"
                image_path = download_image_from_url(img_url, f"tweet_{timestamp}{ext}")
                if image_path:
                    print(f"[图片] 从消息提取图片直链下载成功: {image_path}")

            # 直链没下载到，尝试 fxtwitter
            if not image_path and tweet_url:
                try:
                    fx_info = fetch_tweet(tweet_url)
                    if fx_info and fx_info.get('has_image'):
                        fx_img_url = fx_info['images'][0]
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        ext = ".png" if ".png" in fx_img_url else ".jpg"
                        image_path = download_image_from_url(fx_img_url, f"tweet_{timestamp}{ext}")
                        if image_path:
                            print(f"[图片] 通过 fxtwitter 下载推文原图: {image_path}")
                except Exception as e:
                    logger.warning(f"fxtwitter 获取图片失败: {e}")

            if not image_path:
                print(f"[图片] 推文无图片，稍后自动生成")
        else:
            content = tweet_text.strip()
            username = "manual"
            nickname = "手动输入"
            tweet_type = "manual"
            tweet_url = ""
            print(f"[推文] 手动输入的推文内容")

    print(f"[内容] {content[:200]}{'...' if len(content) > 200 else ''}")
    print("-" * 70)

    # Step 1: AI 分析
    print("\n📊 Step 1: AI Meme 分析...")
    analysis = _analyze_tweet_for_meme_sync(content)

    if not analysis:
        print("❌ AI 分析失败，无法继续创建代币")
        return

    predicted_meme = analysis.get('predicted_meme', '')
    token_name = analysis.get('token_name', predicted_meme)
    token_ticker = analysis.get('token_ticker', token_name)
    token_description = analysis.get('token_description', '')
    reason = analysis.get('reason', '')

    print(f"\n{'='*70}")
    print(f"  💎 Meme:        【{predicted_meme}】")
    print(f"  🏷️  Token Name:  {token_name}")
    print(f"  📊 Ticker:      {token_ticker}")
    print(f"  📝 Description: {token_description}")
    print(f"  💡 理由:        {reason}")
    print(f"{'='*70}")

    # 保存分析记录
    record = {
        'tweet_username': username,
        'tweet_nickname': nickname,
        'tweet_content': content,
        'tweet_url': tweet_url,
        'tweet_type': tweet_type,
        'token_name': token_name,
        'token_ticker': token_ticker,
        'token_description': token_description,
        'ai_reason': reason,
        'status': 'analyzed'
    }
    record_id = save_token_record(record)

    # Step 2: 创建代币
    if not WALLET_PRIVATE_KEY or not WALLET_ADDRESS:
        print("\n⚠️  钱包未配置，跳过代币创建")
        print("   请在 .env 中配置 WALLET_PRIVATE_KEY 和 WALLET_ADDRESS")
        return

    print(f"\n🚀 Step 2: 在 Four.meme 创建代币 {token_name} ({token_ticker})...")

    # 图片处理：有推文图片用推文图片，否则自动生成
    if not image_path:
        image_path = generate_meme_image(token_ticker)
        print(f"   🎨 已自动生成 meme 图片: {image_path}")
    else:
        print(f"   🖼️  使用推文图片: {image_path}")

    # 检查余额
    try:
        w3 = Web3(Web3.HTTPProvider(BSC_RPC_URL))
        wallet = Web3.to_checksum_address(WALLET_ADDRESS)
        balance = w3.eth.get_balance(wallet)
        balance_bnb = w3.from_wei(balance, 'ether')
        print(f"   钱包: {WALLET_ADDRESS}")
        print(f"   余额: {balance_bnb:.6f} BNB")
        if balance_bnb < 0.001:
            print(f"   ❌ 余额不足")
            return
    except Exception as e:
        print(f"   ❌ 网络错误: {e}")
        return

    result = _create_token_with_bundle_sync(token_name, token_ticker, token_description, image_path,
                                             twitter=tweet_url)

    if result.get("success"):
        tx_hash = result['tx_hash']
        token_address = result.get('token_address', '')
        bundle_results = result.get('bundle_results', [])
        print(f"\n{'='*70}")
        print(f"✅ 代币创建成功!")
        print(f"{'='*70}")
        print(f"  交易哈希:  {tx_hash}")
        print(f"  代币地址:  {token_address or '等待解析...'}")
        print(f"  BSCScan:   https://bscscan.com/tx/{tx_hash}")
        if token_address:
            print(f"  Four.meme:  https://four.meme/token/{token_address}")

        # 打印捆绑买入结果
        if bundle_results:
            print(f"\n  📦 捆绑买入结果 ({len(bundle_results)} 个副钱包):")
            for br in bundle_results:
                status = "✅" if br.get("success") else "❌"
                wallet = br.get("wallet", "?")[:10] + "..."
                info = br.get("tx_hash", br.get("error", ""))[:30]
                print(f"    {status} {wallet} - {info}")

        update_token_status(record_id, "created", tx_hash, token_address)

        # 飞书通知：创建成功
        post_payload = _build_feishu_post_result(
            success=True,
            token_name=token_name, token_ticker=token_ticker,
            description=token_description, tweet_url=tweet_url,
            token_address=token_address, tx_hash=tx_hash,
            reason=reason, bundle_results=bundle_results
        )
        _send_feishu_sync(post_payload)
    else:
        error = result.get('error', '未知错误')
        print(f"\n❌ 创建失败: {error}")
        update_token_status(record_id, "failed")

        # 飞书通知：创建失败
        post_payload = _build_feishu_post_result(
            success=False,
            token_name=token_name, token_ticker=token_ticker,
            description=token_description, tweet_url=tweet_url,
            error=error, reason=reason
        )
        _send_feishu_sync(post_payload)


def create_token_manual():
    """
    手动创建代币（需要钱包配置）

    用法：
      python fourmeme_tools.py --create --name "代币名称" --ticker SYMBOL --desc "描述" --image images/logo.jpg
      python fourmeme_tools.py --create   # 交互式输入
    """
    init_db()

    if not WALLET_PRIVATE_KEY or not WALLET_ADDRESS:
        print("❌ 请先在 .env 中配置 WALLET_PRIVATE_KEY 和 WALLET_ADDRESS")
        return

    # 检查余额
    try:
        w3 = Web3(Web3.HTTPProvider(BSC_RPC_URL))
        if w3.is_connected():
            wallet = Web3.to_checksum_address(WALLET_ADDRESS)
            balance = w3.eth.get_balance(wallet)
            balance_bnb = w3.from_wei(balance, 'ether')
            print(f"🔗 已连接 BSC 网络")
            print(f"💰 钱包地址: {WALLET_ADDRESS}")
            print(f"💰 BNB 余额: {balance_bnb:.6f} BNB")

            if balance_bnb < 0.001:
                print(f"⚠️  余额不足，建议至少有 0.005 BNB 用于 Gas 费")
                return
        else:
            print("❌ 无法连接 BSC 网络，请检查 BSC_RPC_URL")
            return
    except Exception as e:
        print(f"❌ 网络连接失败: {e}")
        return

    # 解析命令行参数
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--create', action='store_true')
    parser.add_argument('--name', type=str, default='')
    parser.add_argument('--ticker', type=str, default='')
    parser.add_argument('--desc', type=str, default='')
    parser.add_argument('--image', type=str, default='')
    parser.add_argument('--twitter', type=str, default='')
    parser.add_argument('--telegram', type=str, default='')
    parser.add_argument('--website', type=str, default='')
    args, _ = parser.parse_known_args()

    if args.name:
        token_name = args.name
        token_ticker = args.ticker or token_name
        description = args.desc or f"{token_name} - A community meme token on BSC"
        image_path = args.image
        twitter = args.twitter
        telegram_url = args.telegram
        website = args.website
    else:
        token_name = input("代币名称 (默认 TestMeme): ").strip() or "TestMeme"
        token_ticker = input("股票代码 (默认 TMEME): ").strip() or "TMEME"
        description = input("描述 (默认): ").strip() or f"{token_name} - A community meme token on BSC"
        image_path = input("图片路径 (可选，回车跳过): ").strip()
        twitter = input("Twitter 链接 (可选): ").strip()
        telegram_url = input("Telegram 链接 (可选): ").strip()
        website = input("网站链接 (可选): ").strip()

    token_ticker = token_ticker.strip()

    # 解析图片路径（支持 URL 和本地路径）
    if image_path and (image_path.startswith("http://") or image_path.startswith("https://")):
        # 从 URL 下载图片
        print(f"🌐 从 URL 下载图片...")
        image_path = download_image_from_url(image_path)
        if image_path:
            print(f"   ✅ 图片下载成功: {image_path}")
        else:
            print(f"   ⚠️ 图片下载失败，将自动生成")
    elif image_path and not os.path.isabs(image_path):
        image_path = os.path.join(os.path.dirname(__file__) or '.', image_path)
    if image_path and not os.path.exists(image_path):
        print(f"⚠️  图片文件不存在: {image_path}")
        image_path = ""

    # 没有指定图片时，自动生成黄色 meme 图片
    if not image_path:
        image_path = generate_meme_image(token_ticker)
        print(f"🎨 已自动生成 meme 图片: {image_path}")

    print(f"\n{'='*60}")
    print(f"🚀 准备创建代币 (Four.meme API + 合约)")
    print(f"{'='*60}")
    print(f"  代币名称:  {token_name}")
    print(f"  股票代码:  {token_ticker}")
    print(f"  描述:      {description[:80]}{'...' if len(description) > 80 else ''}")
    print(f"  图片:      {image_path or '无'}")
    print(f"  Twitter:   {twitter or '无'}")
    print(f"  合约:      {FOURMEME_TOKEN_MANAGER}")
    print(f"{'='*60}")

    result = _create_token_with_bundle_sync(
        token_name, token_ticker, description, image_path,
        twitter=twitter, telegram=telegram_url, website=website
    )

    if result.get("success"):
        tx_hash = result['tx_hash']
        token_address = result.get('token_address', '')
        bundle_results = result.get('bundle_results', [])
        print(f"\n{'='*60}")
        print(f"✅ 代币创建成功!")
        print(f"{'='*60}")
        print(f"  交易哈希:  {tx_hash}")
        print(f"  代币地址:  {token_address or '等待解析...'}")
        print(f"  BSCScan:   https://bscscan.com/tx/{tx_hash}")
        if token_address:
            print(f"  Four.meme:  https://four.meme/token/{token_address}")

        # 打印捆绑买入结果
        if bundle_results:
            print(f"\n  📦 捆绑买入结果 ({len(bundle_results)} 个副钱包):")
            for br in bundle_results:
                status = "✅" if br.get("success") else "❌"
                wallet = br.get("wallet", "?")[:10] + "..."
                info = br.get("tx_hash", br.get("error", ""))[:30]
                print(f"    {status} {wallet} - {info}")

        # 保存记录
        record = {
            'token_name': token_name,
            'token_ticker': token_ticker,
            'token_description': description,
            'image_path': image_path,
            'image_url': result.get('image_url', ''),
            'tx_hash': tx_hash,
            'token_address': token_address,
            'status': 'created',
            'tweet_type': 'manual'
        }
        record_id = save_token_record(record)
        print(f"  记录 ID:   {record_id}")

        # 飞书通知：创建成功
        post_payload = _build_feishu_post_result(
            success=True,
            token_name=token_name, token_ticker=token_ticker,
            description=description, tweet_url=twitter,
            token_address=token_address, tx_hash=tx_hash,
            bundle_results=bundle_results
        )
        _send_feishu_sync(post_payload)
    else:
        error = result.get('error', '未知错误')
        print(f"\n❌ 创建失败: {error}")
        if 'tx_hash' in result:
            print(f"   交易哈希: {result['tx_hash']}")
            print(f"   BSCScan: https://bscscan.com/tx/{result['tx_hash']}")

        # 飞书通知：创建失败
        post_payload = _build_feishu_post_result(
            success=False,
            token_name=token_name, token_ticker=token_ticker,
            description=description, tweet_url=twitter,
            error=error
        )
        _send_feishu_sync(post_payload)


def check_wallet():
    """检查钱包配置和余额"""
    print("=" * 50)
    print("FourMeme Tools - 钱包检查")
    print("=" * 50)

    if not WALLET_ADDRESS:
        print("❌ WALLET_ADDRESS 未配置")
        return
    if not WALLET_PRIVATE_KEY:
        print("❌ WALLET_PRIVATE_KEY 未配置")
        return

    print(f"钱包地址: {WALLET_ADDRESS}")

    try:
        w3 = Web3(Web3.HTTPProvider(BSC_RPC_URL))
        if not w3.is_connected():
            print(f"❌ 无法连接 BSC 网络: {BSC_RPC_URL}")
            return

        wallet = Web3.to_checksum_address(WALLET_ADDRESS)
        balance = w3.eth.get_balance(wallet)
        balance_bnb = w3.from_wei(balance, 'ether')
        chain_id = w3.eth.chain_id
        block = w3.eth.block_number

        print(f"网络:     BSC (Chain ID: {chain_id})")
        print(f"RPC:      {BSC_RPC_URL}")
        print(f"区块高度: {block}")
        print(f"BNB 余额: {balance_bnb:.6f} BNB")

        if balance_bnb >= 0.005:
            print(f"✅ 余额充足，可以创建代币")
        elif balance_bnb > 0:
            print(f"⚠️  余额较低，建议至少 0.005 BNB")
        else:
            print(f"❌ 余额为 0，请先充值 BNB")

        # 检查 Four.meme API 连通性
        print(f"\nFour.meme API:")
        try:
            r = requests.get(f"{FOURMEME_API_BASE}/public/health", timeout=5)
            print(f"  API 状态: {'✅ 正常' if r.status_code < 500 else '❌ 异常'}")
        except Exception:
            print(f"  API 状态: ⚠️ 无法连接")
        print(f"  合约地址: {FOURMEME_TOKEN_MANAGER}")

        # 捆绑买入配置
        print(f"\n捆绑发币:")
        print(f"  捆绑买入: {'✅ 已启用' if ENABLE_BUNDLE_BUY else '❌ 已关闭'}")
        print(f"  副钱包数: {len(BUNDLE_WALLETS)} 个")
        if BUNDLE_WALLETS:
            for i, bw in enumerate(BUNDLE_WALLETS):
                addr = bw['address']
                try:
                    bw_balance = w3.eth.get_balance(Web3.to_checksum_address(addr))
                    bw_bnb = w3.from_wei(bw_balance, 'ether')
                    print(f"  副钱包{i+1}: {addr[:10]}...{addr[-6:]} ({bw_bnb:.6f} BNB)")
                except Exception:
                    print(f"  副钱包{i+1}: {addr[:10]}...{addr[-6:]} (余额查询失败)")
            print(f"  每钱包买入: {PRE_SALE_AMOUNT} BNB")

    except Exception as e:
        print(f"❌ 检查失败: {e}")


# --- 主入口 ---
async def main():
    """主入口"""
    validate_config()
    init_db()

    logger.info("FourMeme Tools 启动中...")
    logger.info(f"推特监控群组 ID: {TWITTER_GROUP_ID or '未设置'}")
    logger.info(f"AI Meme 分析: {'已启用' if ENABLE_AI_ANALYSIS else '已关闭'}")
    logger.info(f"自动发币: {'已启用' if ENABLE_AUTO_CREATE else '已关闭'}")
    logger.info(f"捆绑买入: {'已启用' if ENABLE_BUNDLE_BUY else '已关闭'} ({len(BUNDLE_WALLETS)} 个副钱包)")

    if ENABLE_AUTO_CREATE:
        if WALLET_ADDRESS:
            logger.info(f"钱包地址: {WALLET_ADDRESS[:10]}...{WALLET_ADDRESS[-6:]}")
        else:
            logger.warning("钱包地址未配置，自动发币将不可用")

    _init_telegram_client()
    if not client:
        logger.error("Telegram 客户端初始化失败，退出")
        sys.exit(1)

    client.add_event_handler(message_handler, events.NewMessage)

    await client.start(phone=PHONE_NUMBER)
    logger.info("Telegram 客户端已连接，开始监听消息...")
    logger.info("监控规则: 只处理「发布推文」和「回复推文」，转发不处理")

    await client.run_until_disconnected()


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print("""
FourMeme Tools - Meme 代币自动发射工具

用法：
  python fourmeme_tools.py                          # 启动监控模式（Telegram + AI + 自动发币）
  python fourmeme_tools.py --tweet "https://x.com/user/status/123"  # 通过推文URL获取内容+图片，分析并创建代币
  python fourmeme_tools.py --tweet "推文内容"         # 分析推文文本 + 自动创建代币
  python fourmeme_tools.py --create                  # 手动创建代币（交互式）
  python fourmeme_tools.py --create --name YOLO --ticker YOLO --desc "YOLO meme token"
  python fourmeme_tools.py --wallet                  # 检查钱包余额和配置

环境变量: 参考 .env 文件
""")
    elif "--wallet" in sys.argv:
        check_wallet()
    elif "--tweet" in sys.argv:
        # 获取 --tweet 后面的内容
        idx = sys.argv.index("--tweet")
        if idx + 1 < len(sys.argv):
            tweet_content = sys.argv[idx + 1]
            analyze_and_create(tweet_content)
        else:
            print("❌ 请提供推文内容: python fourmeme_tools.py --tweet \"推文内容\"")
    elif "--create" in sys.argv:
        create_token_manual()
    else:
        asyncio.run(main())
