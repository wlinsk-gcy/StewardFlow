
import json
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from .tool import Tool

logger = logging.getLogger(__name__)

class WebSearch(Tool):
    def __init__(self, api_key: str, paywall_keywords: list[str]):
        super().__init__()
        self.name = "web_search"
        self.description = (
            "A web search engine tool. "
            "Use this when you need to answer questions about current events, verify facts, or find information not in your training data. "
            "Input should be a specific search query. "
            "Returns a list of accessible URLs with titles and snippets."
        )
        self.api_key = api_key
        self.paywall_keywords = paywall_keywords

    def link_valid(self, link):
        """
        Check if a link is valid with optimization for speed.
        Uses stream=True to avoid downloading large files.
        """
        if not link.startswith("http"):
            return "Status: Invalid URL"

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            with requests.get(link, headers=headers, timeout=(3, 5), stream=True) as response:
                status = response.status_code
                if status == 404:
                    return "Status: 404 Not Found"
                elif status == 403:
                    return "Status: 403 Forbidden"
                elif status != 200:
                    return f"Status: {status} {response.reason}"
                # 只读取前 1024 字节来检查 Paywall 关键字，避免下载大文件
                # decode('utf-8', errors='ignore') 防止截断导致乱码报错
                try:
                    content_chunk = next(response.iter_content(1024), b"").decode('utf-8', errors='ignore').lower()
                except Exception:
                    # 如果读取流失败，但连接是通的，暂且认为它可访问但内容无法解析
                    return "Status: OK"

                if any(keyword in content_chunk for keyword in self.paywall_keywords):
                    return "Status: Possible Paywall"

                return "Status: OK"
        except requests.exceptions.RequestException as e:
            # 捕获所有 request 异常（超时、DNS错误等）
            return f"Error: Connection Failed"
        except Exception as e:
            return f"Error: {str(e)}"

    def check_all_links(self, links):
        """
        Check all links concurrently using a ThreadPool.
        """
        results = [""] * len(links) # 预分配结果数组以保持顺序
        # 使用线程池并发请求
        # max_workers=10 表示同时检查10个链接，速度提升约10倍
        with ThreadPoolExecutor(max_workers=10) as executor:
            # 将 link 和 index 一起传进去，以便结果对应
            future_to_index = {executor.submit(self.link_valid, link): i for i, link in enumerate(links)}
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    results[index] = f"Error: {str(exc)}"

        return results

    def execute(self, query: str) -> str:
        if self.api_key is None:
            return "Error: No SerpApi key provided."

        query = query.strip()
        logger.info(f"Searching for: {query}")
        if not query:
            return "Error: No search query provided."

        try:
            url = "https://serpapi.com/search"
            params = {
                "q": query,
                "api_key": self.api_key,
                "num": 50,
                "output": "json"
            }
            response = requests.get(url, params=params)
            response.raise_for_status()

            data = response.json()
            results = []
            if "organic_results" in data and len(data["organic_results"]) > 0:
                organic_results = data["organic_results"][:50]
                links = [result.get("link", "No link available") for result in organic_results]
                statuses = self.check_all_links(links)
                for result, status in zip(organic_results, statuses):
                    if not "OK" in status:
                        continue
                    title = result.get("title", "No title")
                    snippet = result.get("snippet", "No snippet available")
                    link = result.get("link", "No link available")
                    results.append({
                        "title": title,
                        "snippet": snippet,
                        "link": link,
                    })
                return json.dumps(results, ensure_ascii=False)
            else:
                return "No results found for the query."
        except requests.RequestException as e:
            logger.error(f"RequestException: {str(e)}")
            raise RuntimeError(f"Error during web search: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            raise RuntimeError(f"Unexpected error: {str(e)}")

    def schema(self) -> dict:

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query to look up on the web."
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False
                },
                "strict": True
            }
        }

# if __name__ == '__main__':
    # query = "什么是Agent系统中的ReAct设计模式？"
    # execute = WebSearch().execute(query)
    # print(execute)
