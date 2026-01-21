import unittest
from core.tools.web_search_use_exa import WebSearch


class TestWebSearch(unittest.TestCase):
    def setUp(self):
        self.tool = WebSearch()

    def test_search(self):
        params = {"query": "OpenAI function calling json schema best practices", "type": "fast"}
        res = self.tool.execute(**params)
        print(res)