import base64
import random
import time
from typing import Dict, List, Optional, Any

import requests

from common.Logger import logger
from common.config import Config


class GitHubClient:
    GITHUB_API_URL = "https://api.github.com/search/code"

    def __init__(self, tokens: List[str]):
        self.tokens = [token.strip() for token in tokens if token.strip()]
        self._token_ptr = 0

    def _next_token(self) -> Optional[str]:
        if not self.tokens:
            return None

        token = self.tokens[self._token_ptr % len(self.tokens)]
        self._token_ptr += 1

        return token.strip() if isinstance(token, str) else token

    def search_for_keys(self, query: str, max_retries: int = 5) -> Dict[str, Any]:
        all_items = []
        total_count = 0
        expected_total = None
        pages_processed = 0

        # 统计信息
        total_requests = 0
        failed_requests = 0
        rate_limit_hits = 0

        for page in range(1, 11):
            page_result = None
            page_success = False

            for attempt in range(1, max_retries + 1):
                current_token = self._next_token()

                headers = {
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
                }

                if current_token:
                    current_token = current_token.strip()
                    headers["Authorization"] = f"token {current_token}"

                params = {
                    "q": query,
                    "per_page": 100,
                    "page": page
                }

                try:
                    total_requests += 1
                    # 获取随机proxy配置
                    proxies = Config.get_random_proxy()
                    if proxies:
                        response = requests.get(self.GITHUB_API_URL, headers=headers, params=params, timeout=30, proxies=proxies)
                    else:
                        response = requests.get(self.GITHUB_API_URL, headers=headers, params=params, timeout=30)
                    rate_limit_remaining = response.headers.get('X-RateLimit-Remaining')
                    # 只在剩余次数很少时警告
                    if rate_limit_remaining and int(rate_limit_remaining) < 3:
                        logger.warning(f"⚠️ Rate limit low: {rate_limit_remaining} remaining, token: {current_token}")
                    response.raise_for_status()
                    page_result = response.json()
                    page_success = True
                    break

                except requests.exceptions.HTTPError as e:
                    status = e.response.status_code if e.response else None
                    failed_requests += 1
                    if status in (403, 429):
                        rate_limit_hits += 1
                        wait = min(2 ** attempt + random.uniform(0, 1), 60)
                        # 只在严重情况下记录详细日志
                        if attempt >= 3:
                            logger.warning(f"❌ Rate limit hit, status:{status} (attempt {attempt}/{max_retries}) - waiting {wait:.1f}s")
                        time.sleep(wait)
                        continue
                    else:
                        # 其他HTTP错误，只在最后一次尝试时记录
                        if attempt == max_retries:
                            logger.error(f"❌ HTTP {status} error after {max_retries} attempts on page {page}")
                        time.sleep(2 ** attempt)
                        continue

                except requests.exceptions.RequestException as e:
                    failed_requests += 1
                    wait = min(2 ** attempt, 30)

                    # 只在最后一次尝试时记录网络错误
                    if attempt == max_retries:
                        logger.error(f"❌ Network error after {max_retries} attempts on page {page}: {type(e).__name__}")

                    time.sleep(wait)
                    continue

            if not page_success or not page_result:
                if page == 1:
                    # 第一页失败是严重问题
                    logger.error(f"❌ First page failed for query: {query[:50]}...")
                    break
                # 后续页面失败不记录，统计信息会体现
                continue

            pages_processed += 1

            if page == 1:
                total_count = page_result.get("total_count", 0)
                expected_total = min(total_count, 1000)

            items = page_result.get("items", [])
            current_page_count = len(items)

            if current_page_count == 0:
                if expected_total and len(all_items) < expected_total:
                    continue
                else:
                    break

            all_items.extend(items)

            if expected_total and len(all_items) >= expected_total:
                break

            if page < 10:
                sleep_time = random.uniform(Config.GITHUB_PAGE_DELAY_MIN, Config.GITHUB_PAGE_DELAY_MAX)
                logger.info(f"⏳ Processing query: 【{query}】,page {page},item count: {current_page_count},expected total: {expected_total},total count: {total_count},random sleep: {sleep_time:.1f}s")
                time.sleep(sleep_time)

        final_count = len(all_items)

        # 检查数据完整性
        if expected_total and final_count < expected_total:
            discrepancy = expected_total - final_count
            if discrepancy > expected_total * 0.1:  # 超过10%数据丢失
                logger.warning(f"⚠️ Significant data loss: {discrepancy}/{expected_total} items missing ({discrepancy / expected_total * 100:.1f}%)")

        # 主要成功日志 - 一条日志包含所有关键信息
        logger.info(f"🔍 GitHub search complete: query:【{query}】 | page success count:{pages_processed} | items count:{final_count}/{expected_total or '?'} | total requests:{total_requests} ")

        result = {
            "total_count": total_count,
            "incomplete_results": final_count < expected_total if expected_total else False,
            "items": all_items
        }

        return result

    def get_file_content(self, item: Dict[str, Any]) -> Optional[str]:
        repo_full_name = item["repository"]["full_name"]
        file_path = item["path"]

        metadata_url = f"https://api.github.com/repos/{repo_full_name}/contents/{file_path}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
        }

        token = self._next_token()
        if token:
            headers["Authorization"] = f"token {token}"

        try:
            # 获取proxy配置
            proxies = Config.get_random_proxy()

            logger.info(f"🔍 Processing file: {metadata_url}")
            if proxies:
                metadata_response = requests.get(metadata_url, headers=headers, proxies=proxies)
            else:
                metadata_response = requests.get(metadata_url, headers=headers)

            metadata_response.raise_for_status()
            file_metadata = metadata_response.json()

            # 检查是否有base64编码的内容
            encoding = file_metadata.get("encoding")
            content = file_metadata.get("content")
            
            if encoding == "base64" and content:
                try:
                    # 解码base64内容
                    decoded_content = base64.b64decode(content).decode('utf-8')
                    return decoded_content
                except Exception as e:
                    logger.warning(f"⚠️ Failed to decode base64 content: {e}, falling back to download_url")
            
            # 如果没有base64内容或解码失败，使用原有的download_url逻辑
            download_url = file_metadata.get("download_url")
            if not download_url:
                logger.warning(f"⚠️ No download URL found for file: {metadata_url}")
                return None

            if proxies:
                content_response = requests.get(download_url, headers=headers, proxies=proxies)
            else:
                content_response = requests.get(download_url, headers=headers)
            logger.info(f"⏳ checking for keys from:  {download_url},status: {content_response.status_code}")
            content_response.raise_for_status()
            return content_response.text

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Failed to fetch file content: {metadata_url}, {type(e).__name__}")
            return None

    @staticmethod
    def create_instance(tokens: List[str]) -> 'GitHubClient':
        return GitHubClient(tokens)
