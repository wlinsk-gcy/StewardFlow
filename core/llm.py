import logging
from typing import Dict, Any
from openai import OpenAI,AsyncOpenAI
from .tools.tool import ToolRegistry
from .extractor import THINK_PATTERN, ThinkStreamExtractor
from .json_stream_filter import HybridJSONStreamFilter
from .builder.build import llm_response_schema,build_llm_messages,build_system_prompt
from ws.connection_manager import ConnectionManager
from .protocol import Event, EventType

logger = logging.getLogger(__name__)


class Provider:
    tool_registry: ToolRegistry
    system_prompt: str
    client: OpenAI
    ws_manager: ConnectionManager
    model: str

    def __init__(self, model:str, api_key: str, base_url: str, tool_registry: ToolRegistry, ws_manager: ConnectionManager):
        self.model = model # NVIDIA的免费API接口测试：QWEN系列模型不支持function call
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key
        )
        self.async_client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key
        )
        self.tool_registry = tool_registry
        self.system_prompt = build_system_prompt()
        self.ws_manager = ws_manager

    def generate(self, context: Dict[str, Any]) -> tuple[str, dict]:
        messages = build_llm_messages(context, self.system_prompt)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.2,
            top_p=0.9,
            tools=self.tool_registry.get_all_schemas(),
            response_format=llm_response_schema,
        )
        if response is None:
            raise Exception("OpenAI response is empty.")
        result = response.choices[0].message.content
        # logger.info(f"===result: {result}")
        if response.usage.prompt_tokens_details:
            logger.info(f"====Cache Tokens：{response.usage.prompt_tokens_details.cached_tokens}")
        logger.info(
            f"消耗输入token：{response.usage.prompt_tokens}， \n消耗输出token：{response.usage.completion_tokens}, \n总消耗token：{response.usage.total_tokens}")
        token_info = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens
        }
        think_match = THINK_PATTERN.search(result)
        think_content = think_match.group(1).strip() if think_match else None

        logger.info(f"===think_content: {think_content}")
        result = THINK_PATTERN.sub("", result).strip()
        return result, token_info

    async def stream_generate(self, context: Dict[str, Any]) -> tuple[str, dict]:
        messages = build_llm_messages(context, self.system_prompt)
        extractor = ThinkStreamExtractor()
        json_filter = HybridJSONStreamFilter()

        result = ""

        # 只在真正产生 prompt/answer 时才发 END
        stream_kind = None  # "prompt" | "answer" | None
        ended = False

        # 防止 provider 一直吐空白（\n / spaces）不 stop
        # 暴力熔断，可能导致JSON不完整。
        blank_run = 0
        MAX_BLANK_RUN = 120  # 可按你的模型/网络情况调整

        token_info = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }

        try:
            stream = await self.async_client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.2,
                top_p=0.9,
                tools=self.tool_registry.get_all_schemas(),
                response_format=llm_response_schema,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                # logger.info(f"chunk: {chunk}")
                if not chunk.choices:
                    logger.info(f"[LLM usage] {chunk.usage}")
                    continue

                choice = chunk.choices[0]
                delta = choice.delta
                finish_reason = choice.finish_reason  # Qwen 系列
                if not delta:
                    # 即便 delta 为空，也可能 finish_reason=stop
                    if finish_reason == "stop" and not ended:
                        ended = True
                        if stream_kind in ("prompt", "answer"):
                            event = Event(EventType.END, context.get("agent_id"), context.get("turn_id"),
                                          {"content": "done"})
                            await self.ws_manager.send(event.to_dict(), context.get("client_id"))
                        break
                    continue

                # 处理 reasoning_content（Qwen 会把推理放这），只打日志/trace，不喂 json_filter
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    # 可以按需打印，别推给前端 chat
                    # logger.info(f"[LLM reasoning] {rc}")
                    pass

                # 处理 delta.content（你真正关心的 JSON 输出）
                if not getattr(delta, "content", None):
                    # 没有 content 也检查 stop
                    if finish_reason == "stop" and not ended:
                        ended = True
                        if stream_kind in ("prompt", "answer"):
                            event = Event(EventType.END, context.get("agent_id"), context.get("turn_id"),
                                          {"content": "done"})
                            await self.ws_manager.send(event.to_dict(), context.get("client_id"))
                        break
                    continue

                # 空白熔断（避免一直 \n）
                content = delta.content
                if content.strip():
                    blank_run = 0
                else:
                    blank_run += 1
                    if blank_run >= MAX_BLANK_RUN:
                        logger.warning(f"[LLM stream] too many blank chunks ({blank_run}), force break")
                        if not ended:
                            ended = True
                            # 仍保持你的语义：只有产生过 prompt/answer 才发 END
                            if stream_kind in ("prompt", "answer"):
                                event = Event(EventType.END, context.get("agent_id"), context.get("turn_id"),
                                              {"content": "done", "reason": "blank_run"})
                                await self.ws_manager.send(event.to_dict(), context.get("client_id"))
                        break
                        # 继续等下一块
                    continue

                # normal_parts: <think>标签外的内容
                # think_parts: <think>标签内的内容
                normal_parts, think_parts = extractor.feed(delta.content) # 用于deepseek / minimax等模型
                # think：只打日志
                # for t in think_parts:
                    # logger.info(f"[LLM THINK] {t}")

                # normal：流式输出
                for n in normal_parts:
                    # logger.info(f"[LLM normal_parts] {n}")
                    result += n
                    events = json_filter.feed(n)
                    for event_type, text in events:
                        stream_kind = event_type
                        if event_type == "prompt":
                            # 这里是 action.prompt，即给用户的HITL提示词
                            event = Event(EventType.HITL_REQUEST,
                                          context.get("agent_id"),
                                          context.get("turn_id"),
                                          {"content": text})
                            await self.ws_manager.send(event.to_dict(), context.get("client_id"))
                        elif event_type == "answer":
                            event = Event(EventType.ANSWER,
                                          context.get("agent_id"),
                                          context.get("turn_id"),
                                          {"content": text})
                            await self.ws_manager.send(event.to_dict(), context.get("client_id"))
                # stop：只发一次 END，并且 break（非常关键）
                if finish_reason == "stop" and not ended:
                    ended = True
                    if stream_kind in ("prompt", "answer"):
                        event = Event(EventType.END, context.get("agent_id"), context.get("turn_id"),
                                      {"content": "done"})
                        await self.ws_manager.send(event.to_dict(), context.get("client_id"))
                    break

            return result, token_info
        except Exception as e:
            event = Event(EventType.ERROR,
                          context.get("agent_id"),
                          context.get("turn_id"),
                          {"content": str(e)})
            await self.ws_manager.send(event.to_dict(), context.get("client_id"))
            logger.error(f"[LLM GENERATE ERROR] {e}")
            raise e
