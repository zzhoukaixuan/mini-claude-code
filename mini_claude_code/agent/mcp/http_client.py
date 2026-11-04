"""同步 MCP 接口的 Streamable HTTP 实现：后台任务持有 SDK 上下文，调用方提交请求。

启动、请求和关闭等待分别有上限；异常后停用连接，无自动重试或重连。
不启动本地程序；关闭连接不能保证远端操作已取消，线程清理超时会明确报告。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import math
import anyio
import httpx
from concurrent.futures import Future, TimeoutError as FutureTimeout, InvalidStateError
from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Lock, Thread

from mcp import ClientSession
from mcp.types import PaginatedRequestParams
from mcp.client.streamable_http import streamable_http_client
from mini_claude_code.mcp_configuration import validate_http_endpoint, validate_http_headers
from mini_claude_code.mcp_secrets import register_mcp_secrets, protect_mcp_transport_logs, redact_mcp_text
import json

from .client import MCPClientClosedError, MCPClientError
from .models import MCPToolDefinition, MCPToolResult, copy_json_object


@dataclass
class _Request:
    operation: str  # list/call；队列中的 None 单独表示关闭
    future: Future  # 将结果或异常送回同步调用方，不在调用方线程执行 SDK
    name: str = ""  # 服务端原始工具名
    arguments: dict | None = None  # 已复制的 JSON 参数


class HTTPMCPClient:
    """单调用方同步使用；URL/认证仅由 Harness 提供，不暴露为模型工具参数。

    使用后必须 close。关闭会取消处理并唤醒等待者，但不能撤销工具副作用。
    不读取模型配置、不传模型密钥；仅支持 Streamable HTTP，不兼容旧式 SSE 端点。
    """

    def __init__(self, url: str, *, headers: dict[str, str] | None = None,
                 allow_local_http: bool = False, startup_timeout: float = 20,
                 request_timeout: float = 30, close_timeout: float = 15) -> None:
        for value in (startup_timeout, request_timeout, close_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("MCP 超时必须是正有限秒数")
        self._request_timeout = request_timeout  # 含排队和分页的请求总预算
        self._sdk_read_timeout = max(startup_timeout, request_timeout)  # 初始化不能误用更短的工具预算
        self._close_timeout = close_timeout  # 等待线程退出的上限
        self._stopping = Event()  # 不可逆停止信号，可早于事件循环建立
        self._scope = None  # 取消请求处理，不取消 SDK 上下文清理
        self._pending: set[Future] = set()  # 等待中的同步请求
        self._url = validate_http_endpoint(url, allow_local_http=allow_local_http)
        self._headers = validate_http_headers({} if headers is None else headers)  # 独立 MCP 凭据
        register_mcp_secrets(self._url, self._headers)
        protect_mcp_transport_logs()
        self._ready: Future = Future()  # 初始化完成或启动失败的通知
        self._finished: Future = Future()  # 上下文清理完成通知
        self._lock = Lock()  # 仅保护请求登记与关闭标志，不在等待结果期间持有
        self._closed = False  # 关闭不可逆，即使底层清理报错也不重新开放
        self._loop: asyncio.AbstractEventLoop | None = None  # 由后台线程创建
        self._queue: asyncio.Queue | None = None  # 只在后台事件循环读写
        self._thread = Thread(target=self._thread_main, name="mcp-http", daemon=True)
        self._thread.start()
        try:
            self._ready.result(timeout=startup_timeout)
        except BaseException as error:
            try:
                self.close()
            except MCPClientError as cleanup_error:
                raise MCPClientError("MCP 启动失败，清理未确认完成") from cleanup_error
            if isinstance(error, FutureTimeout):
                raise MCPClientError("MCP 初始化超时，连接已停用") from error
            raise

    def _thread_main(self) -> None:
        failure = None
        try:
            asyncio.run(self._serve())
        except BaseException as error:
            failure = MCPClientError(f"MCP HTTP 连接退出：{type(error).__name__}")
        finally:
            self._stop()
            if not self._ready.done():
                self._ready.set_exception(failure or MCPClientError("MCP 初始化未完成"))
            if failure is None:
                self._finished.set_result(None)
            else:
                self._finished.set_exception(failure)

    @asynccontextmanager
    async def _transport(self):
        # 自主管理 HTTP 客户端，避免 SDK 默认跟随重定向；不开启自动请求重试。
        # 不读取环境代理或认证配置；系统级 TUN 仍可生效。
        async with httpx.AsyncClient(
            headers=self._headers, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(self._sdk_read_timeout),
        ) as http_client:
            async with streamable_http_client(self._url, http_client=http_client) as streams:
                yield streams

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        active = None
        if self._stopping.is_set():
            return
        try:
            # 两个上下文均由这个长期任务进入和退出，不跨 Task 手动调用 __aexit__。
            async with self._transport() as (reader, writer, _get_session_id):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=self._sdk_read_timeout)) as session:
                    with anyio.CancelScope() as scope:
                        self._scope = scope
                        try:
                            if self._stopping.is_set():
                                return
                            await session.initialize()
                            self._ready.set_result(None)
                            while not self._stopping.is_set():
                                try:
                                    active = await asyncio.wait_for(self._queue.get(), timeout=5)
                                except asyncio.TimeoutError:
                                    await session.send_ping()  # 空闲断连也会触发连接退出
                                    continue
                                if active is None or self._stopping.is_set():
                                    break
                                if active.future.done():
                                    continue
                                try:
                                    if active.operation == "list":
                                        result = await self._discover(session)
                                    else:
                                        raw = await session.call_tool(active.name, active.arguments,
                                                                      read_timeout_seconds=timedelta(seconds=self._request_timeout))
                                        if raw.structuredContent is not None or any(block.type != "text" for block in raw.content):
                                            raise MCPClientError("当前只支持纯文本 MCP 结果")
                                        result = MCPToolResult(redact_mcp_text("\n".join(block.text for block in raw.content)), raw.isError)
                                    try:
                                        active.future.set_result(result)
                                    except InvalidStateError:
                                        pass  # 超时或关闭已先行通知调用方
                                except Exception as error:
                                    self._fail(active.future, MCPClientError(f"MCP 请求失败：{type(error).__name__}；连接停用，不自动重试"))
                                    break
                                active = None
                        finally:
                            self._scope = None
                            self._stop()
        finally:
            # 意外退出也要通知等待中的调用方，不让 future 永久悬空。
            if active is not None and not active.future.done():
                self._fail(active.future, MCPClientError("MCP 连接退出，调用结果未知"))
            while not self._queue.empty():
                pending = self._queue.get_nowait()
                if pending is not None and not pending.future.done():
                    self._fail(pending.future, MCPClientClosedError("MCP 连接已退出"))

    @staticmethod
    async def _discover(session: ClientSession) -> tuple[MCPToolDefinition, ...]:
        tools = []
        cursor = None
        seen_cursors = set()
        while True:
            page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
            # 不通过改写工具名/Schema 来隐藏秘密，否则可能破坏参数契约；整批拒绝发布。
            metadata = json.dumps([tool.model_dump(mode="json") for tool in page.tools], ensure_ascii=False)
            if redact_mcp_text(metadata) != metadata:
                raise MCPClientError("MCP 工具目录包含已知连接凭据，拒绝发布")
            tools.extend(MCPToolDefinition(
                tool.name, tool.description or "", tool.inputSchema,
                tool.annotations.model_dump(exclude_none=True) if tool.annotations else {},
            ) for tool in page.tools)
            cursor = page.nextCursor
            if cursor is None:
                return tuple(tools)
            if cursor in seen_cursors:
                raise MCPClientError("MCP 工具分页游标重复")
            seen_cursors.add(cursor)

    def _submit(self, request: _Request):
        with self._lock:
            if self._closed or self._finished.done():
                raise MCPClientClosedError("MCP HTTP 客户端已关闭或退出")
            self._pending.add(request.future)
        try:
            # 排队回调也检查退出状态，覆盖检查与入队之间的连接退出竞态。
            def enqueue() -> None:
                if self._stopping.is_set():
                    if not request.future.done():
                        self._fail(request.future, MCPClientClosedError("MCP 连接已退出"))
                else:
                    self._queue.put_nowait(request)
            try:
                self._loop.call_soon_threadsafe(enqueue)
            except RuntimeError as error:
                self._stop()
                raise MCPClientClosedError("MCP 事件循环已关闭") from error
            try:
                return request.future.result(timeout=self._request_timeout)
            except FutureTimeout as error:
                self._stop()
                try:
                    self.close()
                except MCPClientError as cleanup_error:
                    raise MCPClientError("MCP 请求超时，执行结果未知，清理未确认完成") from cleanup_error
                raise MCPClientError("MCP 请求超时，连接已关闭；执行结果未知，不自动重试") from error
        finally:
            with self._lock:
                self._pending.discard(request.future)

    def list_tools(self) -> tuple[MCPToolDefinition, ...]:
        return self._submit(_Request("list", Future()))

    def call_tool(self, name: str, arguments: dict) -> MCPToolResult:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("MCP 工具名称必须是非空字符串")
        return self._submit(_Request("call", Future(), name, copy_json_object(arguments, "MCP 调用参数")))

    @staticmethod
    def _fail(future: Future, error: Exception) -> None:
        try:
            future.set_exception(error)
        except InvalidStateError:
            pass  # 正常返回与关闭的竞态，保留先完成的结果

    def _stop(self) -> None:
        with self._lock:
            self._closed = True
            self._stopping.set()
            pending = tuple(self._pending)
        for future in pending:
            self._fail(future, MCPClientClosedError("MCP 连接停止，调用结果可能未知"))
        def cancel_processing() -> None:
            if self._scope is not None:
                self._scope.cancel()
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(cancel_processing)
            except RuntimeError:
                pass

    def close(self) -> None:
        self._stop()
        self._thread.join(timeout=self._close_timeout)
        if self._thread.is_alive():
            raise MCPClientError("MCP 关闭超时，连接已禁用但 HTTP/线程清理未确认完成")
        self._finished.result()
