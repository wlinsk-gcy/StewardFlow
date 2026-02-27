from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Mapping

from .tool import Tool, ToolRegistry

logger = logging.getLogger(__name__)


def _pick_non_empty(*values: Any) -> str | None:
    for val in values:
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def _coerce_bool(raw: Any, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class AgentRunBrowserConfig:
    enabled: bool
    template_name: str
    account_id: str | None
    access_key_id: str | None
    access_key_secret: str | None
    region_id: str
    delete_on_shutdown: bool

    @classmethod
    def from_sources(
        cls,
        *,
        raw: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "AgentRunBrowserConfig":
        raw_cfg = dict(raw or {})
        env_map = env or {}

        template_name = _pick_non_empty(
            raw_cfg.get("template_name"),
            env_map.get("AGENTRUN_TEMPLATE_NAME"),
        )

        enabled_default = bool(template_name)
        enabled = _coerce_bool(raw_cfg.get("enabled"), default=enabled_default)

        access_key_id = _pick_non_empty(
            raw_cfg.get("access_key_id"),
            env_map.get("AGENTRUN_ACCESS_KEY_ID"),
            env_map.get("ALIBABA_CLOUD_ACCESS_KEY_ID"),
        )
        account_id = _pick_non_empty(
            raw_cfg.get("account_id"),
            env_map.get("AGENTRUN_ACCOUNT_ID"),
            env_map.get("ALIBABA_CLOUD_ACCOUNT_ID"),
        )
        access_key_secret = _pick_non_empty(
            raw_cfg.get("access_key_secret"),
            env_map.get("AGENTRUN_ACCESS_KEY_SECRET"),
            env_map.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET"),
        )
        region_id = _pick_non_empty(
            raw_cfg.get("region_id"),
            env_map.get("AGENTRUN_REGION"),
            env_map.get("AGENTRUN_REGION_ID"),
            env_map.get("ALIBABA_CLOUD_REGION"),
            "cn-hangzhou",
        ) or "cn-hangzhou"

        delete_on_shutdown = _coerce_bool(
            raw_cfg.get("delete_on_shutdown"),
            default=True,
        )
        return cls(
            enabled=enabled and bool(template_name),
            template_name=template_name or "",
            account_id=account_id,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            region_id=region_id,
            delete_on_shutdown=delete_on_shutdown,
        )


class AgentRunBrowserManager:
    def __init__(self, config: AgentRunBrowserConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentrun-browser")
        self._sdk_loaded = False
        self._agentrun_mod = None
        self._sandbox_mod = None
        self._set_config = None
        self._sandbox_factory = None
        self._template_type_browser = None
        self._sandbox = None
        self._browser = None

    async def _run_serial(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._thread, lambda: fn(*args, **kwargs))

    @staticmethod
    def _resolve_first_attr(owner: Any, names: list[str]) -> Any:
        for name in names:
            value = getattr(owner, name, None)
            if value is not None:
                return value
        return None

    @staticmethod
    def _filter_kwargs_for_callable(fn: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            sig = inspect.signature(fn)
        except Exception:
            return dict(kwargs)

        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(kwargs)
        allowed = set(params.keys())
        return {k: v for k, v in kwargs.items() if k in allowed}

    @staticmethod
    def _looks_like_url(value: Any) -> bool:
        return isinstance(value, str) and value.startswith(("http://", "https://", "ws://", "wss://"))

    @staticmethod
    def _url_has_query_key(url: str, key: str) -> bool:
        try:
            query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
            return key in query
        except Exception:
            return False

    @staticmethod
    def _url_with_query_key(url: str, key: str, value: str) -> str:
        try:
            split = urlsplit(url)
            query_pairs = parse_qsl(split.query, keep_blank_values=True)
            query = dict(query_pairs)
            query[key] = value
            new_query = urlencode(query, doseq=True)
            return urlunsplit((split.scheme, split.netloc, split.path, new_query, split.fragment))
        except Exception:
            return url

    @staticmethod
    def _to_plain_mapping(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, Mapping):
            return {str(k): v for k, v in value.items()}
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return {str(k): v for k, v in dumped.items()}
        to_dict = getattr(value, "dict", None)
        if callable(to_dict):
            dumped = to_dict()
            if isinstance(dumped, Mapping):
                return {str(k): v for k, v in dumped.items()}
        return None

    @staticmethod
    def _extract_auth_token(node: Any) -> str | None:
        token_hints = {
            "authorization",
            "auth",
            "access_token",
            "accesstoken",
            "token",
            "bearer",
        }

        seen: set[int] = set()

        def _walk(value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(value, str):
                text = value.strip()
                if text and not text.startswith(("http://", "https://", "ws://", "wss://")):
                    return text
                return None
            if isinstance(value, Mapping):
                value_id = id(value)
                if value_id in seen:
                    return None
                seen.add(value_id)
                for k, v in value.items():
                    key = str(k).lower().replace("-", "_")
                    if any(hint in key for hint in token_hints):
                        hit = _walk(v)
                        if hit:
                            return hit
                for v in value.values():
                    hit = _walk(v)
                    if hit:
                        return hit
                return None
            if isinstance(value, list | tuple):
                value_id = id(value)
                if value_id in seen:
                    return None
                seen.add(value_id)
                for item in value:
                    hit = _walk(item)
                    if hit:
                        return hit
            return None

        return _walk(node)

    def _call_method_variants(self, method_name: str, method: Any) -> tuple[list[Any], list[str]]:
        values: list[Any] = []
        errors: list[str] = []
        if not callable(method):
            values.append(method)
            return values, errors

        call_variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = [((), {})]
        try:
            sig = inspect.signature(method)
            params = [p for p in sig.parameters.values() if p.name != "self"]
            has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
            has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

            bool_kw_hints = (
                "authorization",
                "auth",
                "signed",
                "token",
                "record",
                "live",
            )
            for p in params:
                name = p.name.lower()
                if any(h in name for h in bool_kw_hints):
                    call_variants.append(((), {p.name: True}))
                    call_variants.append(((), {p.name: False}))

            if has_varargs or len(params) >= 1:
                call_variants.append(((True,), {}))
                call_variants.append(((False,), {}))

            if has_kwargs:
                call_variants.append(((), {"authorization": True}))
                call_variants.append(((), {"include_authorization": True}))
                call_variants.append(((), {"with_token": True}))
        except Exception:
            # Signature parsing failure: keep default no-arg call only.
            pass

        seen_calls: set[tuple[tuple[Any, ...], tuple[tuple[str, Any], ...]]] = set()
        for args, kwargs in call_variants:
            key = (args, tuple(sorted(kwargs.items())))
            if key in seen_calls:
                continue
            seen_calls.add(key)
            try:
                values.append(method(*args, **kwargs))
            except TypeError:
                continue
            except Exception as exc:
                errors.append(f"{method_name}{args or ''}{kwargs or ''}: {exc}")
        return values, errors

    @classmethod
    def _find_url_by_key_hint(cls, node: Any, key_hints: tuple[str, ...]) -> str | None:
        if isinstance(node, Mapping):
            for key, val in node.items():
                key_text = str(key).lower()
                if isinstance(val, str) and cls._looks_like_url(val):
                    if any(hint in key_text for hint in key_hints):
                        return val
                hit = cls._find_url_by_key_hint(val, key_hints)
                if hit:
                    return hit
            return None
        if isinstance(node, list | tuple):
            for item in node:
                hit = cls._find_url_by_key_hint(item, key_hints)
                if hit:
                    return hit
        return None

    @classmethod
    def _merge_session_value(cls, target: dict[str, Any], method_name: str, value: Any) -> None:
        if value is None:
            return

        lowered = method_name.lower()
        mapped = cls._to_plain_mapping(value)
        if mapped is not None:
            for key, val in mapped.items():
                target.setdefault(key, val)

            # Normalize common URL fields so frontend can discover VNC/CDP quickly.
            if "vnc_url" not in target:
                vnc_url = cls._find_url_by_key_hint(mapped, ("vnc", "novnc", "viewer"))
                if vnc_url:
                    target["vnc_url"] = vnc_url
            if "cdp_url" not in target:
                cdp_url = cls._find_url_by_key_hint(mapped, ("cdp", "devtools"))
                if cdp_url:
                    target["cdp_url"] = cdp_url
            return

        if isinstance(value, str):
            if cls._looks_like_url(value):
                if "vnc" in lowered or "viewer" in lowered:
                    target.setdefault("vnc_url", value)
                    return
                if "cdp" in lowered or "devtools" in lowered:
                    target.setdefault("cdp_url", value)
                    return
                if "url" in lowered:
                    target.setdefault("url", value)
                    return
            target.setdefault(method_name, value)
            return

        target.setdefault(method_name, value)

    def _invoke_compat(self, fn: Any, kwargs_candidates: list[dict[str, Any]], *, op_name: str) -> Any:
        last_error: Exception | None = None
        for raw_kwargs in kwargs_candidates:
            filtered = self._filter_kwargs_for_callable(fn, raw_kwargs)
            try:
                return fn(**filtered)
            except TypeError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
                break
        if last_error is None:
            raise RuntimeError(f"{op_name} failed: no callable kwargs candidate provided")
        raise RuntimeError(f"{op_name} failed: {last_error}") from last_error

    def _resolve_sandbox_factory(self) -> Any:
        candidate_classes = [
            self._resolve_first_attr(self._sandbox_mod, ["Sandbox", "BrowserSandbox"]),
            self._resolve_first_attr(self._agentrun_mod, ["Sandbox", "BrowserSandbox"]),
        ]
        for cls in candidate_classes:
            if cls is None:
                continue
            create_fn = getattr(cls, "create", None)
            if callable(create_fn):
                return create_fn

        candidate_functions = [
            self._resolve_first_attr(self._sandbox_mod, ["create", "create_sandbox", "create_browser_sandbox"]),
            self._resolve_first_attr(self._agentrun_mod, ["create_sandbox", "create_browser_sandbox"]),
        ]
        for fn in candidate_functions:
            if callable(fn):
                return fn
        return None

    def _resolve_template_type_browser(self) -> Any:
        type_container = self._resolve_first_attr(
            self._sandbox_mod,
            ["TemplateType", "SandboxTemplateType", "BrowserTemplateType"],
        )
        if type_container is None:
            type_container = self._resolve_first_attr(
                self._agentrun_mod,
                ["TemplateType", "SandboxTemplateType", "BrowserTemplateType"],
            )
        if type_container is None:
            return "browser"
        return (
            getattr(type_container, "BROWSER", None)
            or getattr(type_container, "browser", None)
            or "browser"
        )

    def _load_sdk_if_needed(self) -> None:
        if self._sdk_loaded:
            return

        try:
            agentrun_mod = importlib.import_module("agentrun")
            sandbox_mod = importlib.import_module("agentrun.sandbox")
        except Exception as exc:
            raise RuntimeError(
                "agentrun-sdk is required. Install with: pip install \"agentrun-sdk[playwright]\""
            ) from exc

        self._agentrun_mod = agentrun_mod
        self._sandbox_mod = sandbox_mod
        self._set_config = self._resolve_first_attr(
            agentrun_mod,
            [
                "set_config",
                "set_agentrun_config",
                "configure",
                "init",
            ],
        )
        self._sandbox_factory = self._resolve_sandbox_factory()
        self._template_type_browser = self._resolve_template_type_browser()
        if not callable(self._sandbox_factory):
            exports = [n for n in dir(sandbox_mod) if not n.startswith("_")]
            raise RuntimeError(
                "Invalid agentrun-sdk installation: cannot resolve sandbox factory in "
                f"agentrun.sandbox exports={exports[:40]}"
            )
        self._sdk_loaded = True

    def _ensure_ready_sync(self) -> Any:
        self._load_sdk_if_needed()
        if not self.config.account_id:
            raise RuntimeError(
                "AgentRun account id is missing. Set AGENTRUN_ACCOUNT_ID "
                "(or configure account_id in config.yaml)."
            )
        if not self.config.access_key_id or not self.config.access_key_secret:
            raise RuntimeError(
                "AgentRun credentials are missing. Set AGENTRUN_ACCESS_KEY_ID and "
                "AGENTRUN_ACCESS_KEY_SECRET (or configure access_key_id/access_key_secret)."
            )
        # Some AgentRun SDK versions read credentials from environment variables only.
        os.environ["AGENTRUN_ACCOUNT_ID"] = self.config.account_id
        os.environ["ALIBABA_CLOUD_ACCOUNT_ID"] = self.config.account_id
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"] = self.config.access_key_id
        os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = self.config.access_key_secret
        os.environ["AGENTRUN_ACCESS_KEY_ID"] = self.config.access_key_id
        os.environ["AGENTRUN_ACCESS_KEY_SECRET"] = self.config.access_key_secret
        os.environ["AGENTRUN_REGION"] = self.config.region_id
        os.environ["AGENTRUN_REGION_ID"] = self.config.region_id
        os.environ["ALIBABA_CLOUD_REGION"] = self.config.region_id

        if callable(self._set_config):
            self._invoke_compat(
                self._set_config,
                kwargs_candidates=[
                    {
                        "account_id": self.config.account_id,
                        "access_key_id": self.config.access_key_id,
                        "access_key_secret": self.config.access_key_secret,
                        "region_id": self.config.region_id,
                    },
                    {
                        "account_id": self.config.account_id,
                        "ak": self.config.access_key_id,
                        "sk": self.config.access_key_secret,
                        "region_id": self.config.region_id,
                    },
                    {
                        "accountId": self.config.account_id,
                        "accessKeyId": self.config.access_key_id,
                        "accessKeySecret": self.config.access_key_secret,
                        "regionId": self.config.region_id,
                    },
                ],
                op_name="set AgentRun config",
            )

        if self._sandbox is None:
            self._sandbox = self._invoke_compat(
                self._sandbox_factory,
                kwargs_candidates=[
                    {
                        "template_name": self.config.template_name,
                        "template_type": self._template_type_browser,
                    },
                    {
                        "template_name": self.config.template_name,
                        "template_type": "browser",
                    },
                    {
                        "template": self.config.template_name,
                        "template_type": self._template_type_browser,
                    },
                    {
                        "name": self.config.template_name,
                        "template_type": self._template_type_browser,
                    },
                    {
                        "template_name": self.config.template_name,
                    },
                ],
                op_name="create AgentRun sandbox",
            )
            logger.info(
                "AgentRun browser sandbox created, template=%s region=%s",
                self.config.template_name,
                self.config.region_id,
            )

        if self._browser is None:
            data = getattr(self._sandbox, "data", None)
            browser_source = data if data is not None else self._sandbox
            browser_ctor = self._resolve_first_attr(
                browser_source,
                ["sync_playwright", "playwright", "get_playwright", "browser_client"],
            )
            if not callable(browser_ctor):
                raise RuntimeError("Invalid browser sandbox: missing playwright client constructor")
            self._browser = browser_ctor()
        return self._browser

    def _session_payload_sync(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "template_name": self.config.template_name,
            "region_id": self.config.region_id,
        }
        if self._sandbox is None:
            payload["sandbox_id"] = None
            payload["session_config"] = {}
            return payload

        sandbox_id = getattr(self._sandbox, "sandbox_id", None) or getattr(self._sandbox, "id", None)
        payload["sandbox_id"] = sandbox_id
        data = getattr(self._sandbox, "data", None)
        sources = [self._sandbox]
        if data is not None and data is not self._sandbox:
            sources.append(data)

        session_config: dict[str, Any] = {}
        errors: list[str] = []
        method_names = (
            "get_session_config",
            "session_config",
            "get_connection_info",
            "connection_info",
            "get_vnc_url",
            "vnc_url",
            "get_cdp_url",
            "cdp_url",
        )

        for source in sources:
            for method_name in method_names:
                method = getattr(source, method_name, None)
                if method is None:
                    continue
                values, variant_errors = self._call_method_variants(method_name, method)
                if variant_errors:
                    errors.extend(f"{type(source).__name__}.{err}" for err in variant_errors)
                for session_value in values:
                    self._merge_session_value(session_config, method_name, session_value)

        if "vnc_url" not in session_config:
            vnc_url = self._find_url_by_key_hint(session_config, ("vnc", "novnc", "viewer"))
            if vnc_url:
                session_config["vnc_url"] = vnc_url
        if "cdp_url" not in session_config:
            cdp_url = self._find_url_by_key_hint(session_config, ("cdp", "devtools"))
            if cdp_url:
                session_config["cdp_url"] = cdp_url

        token = self._extract_auth_token(session_config)
        if token:
            for key in ("vnc_url", "cdp_url", "automation_url"):
                raw_url = session_config.get(key)
                if not isinstance(raw_url, str):
                    continue
                if self._url_has_query_key(raw_url, "Authorization"):
                    continue
                session_config[key] = self._url_with_query_key(raw_url, "Authorization", token)

        if not session_config and errors:
            session_config["errors"] = errors

        payload["session_config"] = session_config
        return payload

    def _call_browser(self, method_names: list[str], *args: Any) -> Any:
        browser = self._ensure_ready_sync()
        for method_name in method_names:
            method = getattr(browser, method_name, None)
            if callable(method):
                return method(*args)
        raise RuntimeError(f"Browser client method not found: {method_names}")

    async def get_session(self) -> dict[str, Any]:
        async with self._lock:
            await self._run_serial(self._ensure_ready_sync)
            return await self._run_serial(self._session_payload_sync)

    async def open(self, url: str | None = None) -> dict[str, Any]:
        async with self._lock:
            def _op():
                page_ref = self._call_browser(["open", "new_page", "open_page"])
                if url:
                    self._call_browser(["goto", "navigate", "open_url"], url)
                payload = self._session_payload_sync()
                payload["page_ref"] = page_ref
                payload["url"] = url
                return payload
            return await self._run_serial(_op)

    async def goto(self, url: str) -> Any:
        async with self._lock:
            def _op():
                return self._call_browser(["goto", "navigate", "open_url"], url)
            return await self._run_serial(_op)

    async def click(self, selector: str) -> Any:
        async with self._lock:
            def _op():
                return self._call_browser(["click"], selector)
            return await self._run_serial(_op)

    async def fill(self, selector: str, text: str) -> Any:
        async with self._lock:
            def _op():
                return self._call_browser(["fill", "type"], selector, text)
            return await self._run_serial(_op)

    async def wait(self, milliseconds: int) -> Any:
        async with self._lock:
            def _op():
                return self._call_browser(["wait", "sleep"], int(milliseconds))
            return await self._run_serial(_op)

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        async with self._lock:
            def _op():
                return self._call_browser(["evaluate", "exec_js"], script, arg)
            return await self._run_serial(_op)

    async def html_content(self) -> Any:
        async with self._lock:
            def _op():
                return self._call_browser(["html_content", "content", "get_content"])
            return await self._run_serial(_op)

    async def close(self) -> Any:
        async with self._lock:
            def _op():
                if self._browser is None:
                    return {"closed": False, "reason": "browser_not_initialized"}
                close_fn = self._resolve_first_attr(self._browser, ["close", "quit"])
                if callable(close_fn):
                    result = close_fn()
                else:
                    result = {"warning": "browser client has no close/quit"}
                self._browser = None
                return {"closed": True, "result": result}
            return await self._run_serial(_op)

    async def delete_sandbox(self) -> dict[str, Any]:
        async with self._lock:
            def _op():
                if self._sandbox is None:
                    return {"deleted": False, "reason": "sandbox_not_initialized"}
                data = getattr(self._sandbox, "data", None)
                target = data if data is not None else self._sandbox
                delete_fn = self._resolve_first_attr(target, ["delete", "remove", "destroy"])
                if not callable(delete_fn):
                    raise RuntimeError("Sandbox delete() is unavailable")
                delete_fn()
                self._browser = None
                self._sandbox = None
                return {"deleted": True}
            return await self._run_serial(_op)

    async def shutdown(self) -> None:
        async with self._lock:
            if self.config.delete_on_shutdown:
                def _delete_sync():
                    if self._sandbox is None:
                        return
                    data = getattr(self._sandbox, "data", None)
                    target = data if data is not None else self._sandbox
                    delete_fn = self._resolve_first_attr(target, ["delete", "remove", "destroy"])
                    if not callable(delete_fn):
                        return
                    delete_fn()
                    self._browser = None
                    self._sandbox = None
                try:
                    await self._run_serial(_delete_sync)
                except Exception as exc:
                    logger.warning("Failed to delete AgentRun sandbox on shutdown: %s", exc)
            self._thread.shutdown(wait=False)


class _BaseAgentRunBrowserTool(Tool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__()
        self.manager = manager

    @staticmethod
    def _json(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)


class BrowserGetSessionTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_get_session"
        self.description = (
            "Create (or reuse) AgentRun browser sandbox by template name and return session info "
            "(including VNC/CDP URLs when available)."
        )

    async def execute(self, **kwargs) -> str:
        del kwargs
        payload = await self.manager.get_session()
        return self._json(payload)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
                "strict": True,
            },
        }


class BrowserOpenTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_open"
        self.description = "Open browser page in AgentRun sandbox. Optionally navigate to a URL."

    async def execute(self, url: str | None = None, **kwargs) -> str:
        del kwargs
        payload = await self.manager.open(url=url)
        return self._json(payload)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Optional initial URL.",
                        }
                    },
                },
                "strict": True,
            },
        }


class BrowserGotoTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_goto"
        self.description = "Navigate current browser page to URL."

    async def execute(self, url: str, **kwargs) -> str:
        del kwargs
        payload = await self.manager.goto(url)
        return self._json({"url": url, "result": payload})

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Target URL.",
                        }
                    },
                    "required": ["url"],
                },
                "strict": True,
            },
        }


class BrowserClickTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_click"
        self.description = "Click element on the current browser page by CSS selector."

    async def execute(self, selector: str, **kwargs) -> str:
        del kwargs
        payload = await self.manager.click(selector)
        return self._json({"selector": selector, "result": payload})

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector to click.",
                        }
                    },
                    "required": ["selector"],
                },
                "strict": True,
            },
        }


class BrowserFillTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_fill"
        self.description = "Fill input/textarea by CSS selector."

    async def execute(self, selector: str, text: str, **kwargs) -> str:
        del kwargs
        payload = await self.manager.fill(selector, text)
        return self._json({"selector": selector, "result": payload})

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector of the input element.",
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to fill.",
                        },
                    },
                    "required": ["selector", "text"],
                },
                "strict": True,
            },
        }


class BrowserWaitTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_wait"
        self.description = "Wait for a fixed number of milliseconds."

    async def execute(self, milliseconds: int = 500, **kwargs) -> str:
        del kwargs
        payload = await self.manager.wait(milliseconds)
        return self._json({"milliseconds": int(milliseconds), "result": payload})

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "milliseconds": {
                            "type": "integer",
                            "description": "Milliseconds to wait.",
                        }
                    },
                },
                "strict": True,
            },
        }


class BrowserEvaluateTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_evaluate"
        self.description = "Evaluate JavaScript on current page."

    async def execute(self, script: str, arg: Any = None, **kwargs) -> str:
        del kwargs
        payload = await self.manager.evaluate(script, arg)
        return self._json({"result": payload})

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "script": {
                            "type": "string",
                            "description": "JavaScript expression/function text.",
                        },
                        "arg": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "integer"},
                                {"type": "boolean"},
                                {"type": "array"},
                                {"type": "object"},
                                {"type": "null"},
                            ],
                            "description": "Optional argument for script.",
                        },
                    },
                    "required": ["script"],
                },
                "strict": True,
            },
        }


class BrowserHtmlContentTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_html_content"
        self.description = "Get current page full HTML content."

    async def execute(self, **kwargs) -> str:
        del kwargs
        payload = await self.manager.html_content()
        return self._json({"html": payload})

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
                "strict": True,
            },
        }


class BrowserCloseTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_close"
        self.description = "Close the active page/browser session."

    async def execute(self, **kwargs) -> str:
        del kwargs
        payload = await self.manager.close()
        return self._json(payload)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
                "strict": True,
            },
        }


class BrowserDeleteSandboxTool(_BaseAgentRunBrowserTool):
    def __init__(self, manager: AgentRunBrowserManager):
        super().__init__(manager)
        self.name = "browser_delete_sandbox"
        self.description = "Delete current AgentRun browser sandbox instance."
        self.requires_confirmation = True

    async def execute(self, **kwargs) -> str:
        del kwargs
        payload = await self.manager.delete_sandbox()
        return self._json(payload)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
                "strict": True,
            },
        }


def register_agentrun_browser_tools(
    *,
    registry: ToolRegistry,
    raw_config: Mapping[str, Any] | None,
    env: Mapping[str, str] | None,
) -> AgentRunBrowserManager | None:
    cfg = AgentRunBrowserConfig.from_sources(raw=raw_config, env=env)
    if not cfg.enabled:
        if raw_config and raw_config.get("template_name"):
            logger.warning(
                "AgentRun browser toolset is disabled (enabled=false). template=%s",
                raw_config.get("template_name"),
            )
        return None

    manager = AgentRunBrowserManager(cfg)
    registry.register(BrowserGetSessionTool(manager))
    registry.register(BrowserOpenTool(manager))
    registry.register(BrowserGotoTool(manager))
    registry.register(BrowserClickTool(manager))
    registry.register(BrowserFillTool(manager))
    registry.register(BrowserWaitTool(manager))
    registry.register(BrowserEvaluateTool(manager))
    registry.register(BrowserHtmlContentTool(manager))
    registry.register(BrowserCloseTool(manager))
    registry.register(BrowserDeleteSandboxTool(manager))
    logger.info(
        "AgentRun browser tools registered with template=%s region=%s",
        cfg.template_name,
        cfg.region_id,
    )
    return manager
