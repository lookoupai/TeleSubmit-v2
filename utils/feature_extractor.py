"""
特征提取模块
用于从投稿内容中提取可识别特征，支持重复检测
"""
import re
import hashlib
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# 特征版本号，用于后续升级兼容
FINGERPRINT_VERSION = 1


@dataclass
class SubmissionFingerprint:
    """投稿指纹（用于重复检测）"""
    user_id: int
    username: str = ""

    # 从投稿内容提取
    urls: List[str] = field(default_factory=list)
    tg_usernames: List[str] = field(default_factory=list)
    tg_links: List[str] = field(default_factory=list)
    phone_numbers: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    content_hash: str = ""

    # 从用户签名提取
    bio_urls: List[str] = field(default_factory=list)
    bio_tg_links: List[str] = field(default_factory=list)
    bio_contacts: List[str] = field(default_factory=list)

    # 元数据
    submit_time: float = 0.0
    content_length: int = 0
    fingerprint_version: int = FINGERPRINT_VERSION

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            'user_id': self.user_id,
            'username': self.username,
            'urls': self.urls,
            'tg_usernames': self.tg_usernames,
            'tg_links': self.tg_links,
            'phone_numbers': self.phone_numbers,
            'emails': self.emails,
            'content_hash': self.content_hash,
            'bio_urls': self.bio_urls,
            'bio_tg_links': self.bio_tg_links,
            'bio_contacts': self.bio_contacts,
            'submit_time': self.submit_time,
            'content_length': self.content_length,
            'fingerprint_version': self.fingerprint_version
        }

    def get_all_features(self) -> List[tuple]:
        """获取所有特征列表，用于数据库存储"""
        features = []
        for url in self.urls:
            features.append(('url', url))
        for tg_user in self.tg_usernames:
            features.append(('tg_username', tg_user))
        for tg_link in self.tg_links:
            features.append(('tg_link', tg_link))
        for phone in self.phone_numbers:
            features.append(('phone', phone))
        for email in self.emails:
            features.append(('email', email))
        # bio 特征
        for url in self.bio_urls:
            features.append(('bio_url', url))
        for tg_link in self.bio_tg_links:
            features.append(('bio_tg_link', tg_link))
        for contact in self.bio_contacts:
            features.append(('bio_contact', contact))
        return features


class FeatureExtractor:
    """特征提取器"""

    # 正则表达式模式
    PATTERNS = {
        # URL 提取（排除常见的短链接服务和CDN）
        'url': re.compile(
            r'https?://[^\s<>"{}|\\^`\[\]]+',
            re.IGNORECASE
        ),

        # Telegram 用户名 @username
        'tg_username': re.compile(
            r'@([a-zA-Z][a-zA-Z0-9_]{4,31})',
            re.IGNORECASE
        ),

        # Telegram 链接 t.me/xxx 或 telegram.me/xxx
        'tg_link': re.compile(
            r'(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_+]+)',
            re.IGNORECASE
        ),

        # 电话号码（国际格式）
        'phone': re.compile(
            r'(?:\+?[0-9]{1,4}[-.\s]?)?(?:\(?[0-9]{2,4}\)?[-.\s]?)?[0-9]{3,4}[-.\s]?[0-9]{3,4}',
            re.IGNORECASE
        ),

        # 邮箱地址
        'email': re.compile(
            r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            re.IGNORECASE
        ),

        # 微信号
        'wechat': re.compile(
            r'(?:微信|wx|WeChat)[：:\s]*([a-zA-Z0-9_-]+)',
            re.IGNORECASE
        ),

        # QQ 号
        'qq': re.compile(
            r'(?:QQ|qq)[：:\s]*([0-9]{5,12})',
            re.IGNORECASE
        ),
    }

    # 需要过滤的常见无意义 URL
    URL_BLACKLIST = [
        'telegram.org',
        't.me/addstickers',
        't.me/setlanguage',
        'google.com/search',
        'bing.com/search',
    ]

    def __init__(self):
        pass

    def extract_all(self, text: str) -> dict:
        """
        从文本中提取所有特征

        Args:
            text: 要分析的文本

        Returns:
            dict: 包含各类特征的字典
        """
        if not text:
            return {
                'urls': [],
                'tg_usernames': [],
                'tg_links': [],
                'phone_numbers': [],
                'emails': [],
                'wechat': [],
                'qq': []
            }

        result = {
            'urls': self._extract_urls(text),
            'tg_usernames': self._extract_tg_usernames(text),
            'tg_links': self._extract_tg_links(text),
            'phone_numbers': self._extract_phones(text),
            'emails': self._extract_emails(text),
            'wechat': self._extract_wechat(text),
            'qq': self._extract_qq(text)
        }

        return result

    def _extract_urls(self, text: str) -> List[str]:
        """提取 URL"""
        urls = self.PATTERNS['url'].findall(text)
        # 过滤和标准化
        filtered = []
        for url in urls:
            # 移除尾部的标点符号
            url = url.rstrip('.,;:!?)')
            # 过滤黑名单
            if not any(bl in url.lower() for bl in self.URL_BLACKLIST):
                filtered.append(url.lower())
        return list(set(filtered))

    def _extract_tg_usernames(self, text: str) -> List[str]:
        """提取 Telegram 用户名"""
        matches = self.PATTERNS['tg_username'].findall(text)
        # 标准化为小写
        return list(set(m.lower() for m in matches))

    def _extract_tg_links(self, text: str) -> List[str]:
        """提取 Telegram 链接"""
        matches = self.PATTERNS['tg_link'].findall(text)
        # 标准化为小写
        return list(set(m.lower() for m in matches))

    def _extract_phones(self, text: str) -> List[str]:
        """提取电话号码"""
        matches = self.PATTERNS['phone'].findall(text)
        # 标准化：移除所有非数字字符
        normalized = []
        for phone in matches:
            clean = re.sub(r'[^\d+]', '', phone)
            # 至少7位数字才认为是有效电话
            if len(clean.replace('+', '')) >= 7:
                normalized.append(clean)
        return list(set(normalized))

    def _extract_emails(self, text: str) -> List[str]:
        """提取邮箱地址"""
        matches = self.PATTERNS['email'].findall(text)
        return list(set(m.lower() for m in matches))

    def _extract_wechat(self, text: str) -> List[str]:
        """提取微信号"""
        matches = self.PATTERNS['wechat'].findall(text)
        return list(set(m.lower() for m in matches))

    def _extract_qq(self, text: str) -> List[str]:
        """提取 QQ 号"""
        matches = self.PATTERNS['qq'].findall(text)
        return list(set(matches))

    def compute_content_hash(self, text: str) -> str:
        """
        计算内容哈希值（用于相似度比较）

        使用 SimHash 的简化版本：
        1. 分词
        2. 计算每个词的哈希
        3. 组合成最终指纹

        Args:
            text: 文本内容

        Returns:
            str: 64位哈希值的十六进制表示
        """
        if not text:
            return ""

        # 简单分词（按空格和标点）
        words = re.split(r'[\s\n\r\t,，。.!！?？;；:：、]+', text)
        words = [w.strip().lower() for w in words if len(w.strip()) >= 2]

        if not words:
            return hashlib.md5(text.encode('utf-8')).hexdigest()

        # 计算 SimHash
        v = [0] * 64

        for word in words:
            # 计算词的哈希
            word_hash = int(hashlib.md5(word.encode('utf-8')).hexdigest(), 16)
            for i in range(64):
                if word_hash & (1 << i):
                    v[i] += 1
                else:
                    v[i] -= 1

        # 生成最终指纹
        fingerprint = 0
        for i in range(64):
            if v[i] >= 0:
                fingerprint |= (1 << i)

        return format(fingerprint, '016x')

    def compute_simhash_distance(self, hash1: str, hash2: str) -> int:
        """
        计算两个 SimHash 之间的汉明距离

        Args:
            hash1: 第一个哈希值
            hash2: 第二个哈希值

        Returns:
            int: 汉明距离（0-64）
        """
        if not hash1 or not hash2:
            return 64

        try:
            n1 = int(hash1, 16)
            n2 = int(hash2, 16)
            xor = n1 ^ n2
            return bin(xor).count('1')
        except ValueError:
            return 64

    def create_fingerprint(
        self,
        user_id: int,
        username: str,
        content: str,
        bio: Optional[str] = None
    ) -> SubmissionFingerprint:
        """
        创建投稿指纹

        Args:
            user_id: 用户 ID
            username: 用户名
            content: 投稿内容（合并文本、标签、链接等）
            bio: 用户签名（可选）

        Returns:
            SubmissionFingerprint: 投稿指纹对象
        """
        # 提取内容特征
        content_features = self.extract_all(content)

        # 提取签名特征
        bio_features = self.extract_all(bio) if bio else {
            'urls': [], 'tg_usernames': [], 'tg_links': [],
            'phone_numbers': [], 'emails': [], 'wechat': [], 'qq': []
        }

        # 合并签名中的联系方式
        bio_contacts = []
        bio_contacts.extend(bio_features.get('wechat', []))
        bio_contacts.extend(bio_features.get('qq', []))
        bio_contacts.extend(bio_features.get('phone_numbers', []))
        bio_contacts.extend(bio_features.get('emails', []))

        fingerprint = SubmissionFingerprint(
            user_id=user_id,
            username=username,
            urls=content_features['urls'],
            tg_usernames=content_features['tg_usernames'],
            tg_links=content_features['tg_links'],
            phone_numbers=content_features['phone_numbers'],
            emails=content_features['emails'],
            content_hash=self.compute_content_hash(content),
            bio_urls=bio_features.get('urls', []),
            bio_tg_links=bio_features.get('tg_links', []) + bio_features.get('tg_usernames', []),
            bio_contacts=bio_contacts,
            submit_time=datetime.now().timestamp(),
            content_length=len(content) if content else 0,
            fingerprint_version=FINGERPRINT_VERSION
        )

        logger.debug(f"创建指纹: user_id={user_id}, urls={len(fingerprint.urls)}, "
                    f"tg_links={len(fingerprint.tg_links)}, hash={fingerprint.content_hash[:8]}...")

        return fingerprint


# 全局实例
_extractor = None


def get_feature_extractor() -> FeatureExtractor:
    """获取特征提取器单例"""
    global _extractor
    if _extractor is None:
        _extractor = FeatureExtractor()
    return _extractor
