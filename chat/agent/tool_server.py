from __future__ import annotations

import asyncio
import hmac
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from aiohttp import web

from .dsh_runtime import DshRuntimeError, describe_dsh_error

from .project_tools import ProjectToolError


DrawHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]
ProjectHandler = Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]
DiscordHandler = Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]
DrawProfileHandler = Callable[[dict[str, object]], Awaitable[dict[str, object]]]
MaintenanceHandler = Callable[[str], Awaitable[dict[str, object]]]
FortuneHandler = Callable[[], Awaitable[dict[str, object]]]


@dataclass(slots=True)
class _MaintenanceJob:
    session_id: str
    task: asyncio.Task[dict[str, object]]


class AgentToolServerError(RuntimeError):
    pass


class AgentToolServer:
    """Loopback-only host for dsh tools implemented by the Python bot."""

    def __init__(self) -> None:
        self._token = secrets.token_urlsafe(48)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._endpoint = ""
        self._start_lock = asyncio.Lock()
        self._bindings: dict[str, DrawHandler] = {}
        self._project_bindings: dict[str, ProjectHandler] = {}
        self._discord_bindings: dict[str, DiscordHandler] = {}
        self._draw_profile_bindings: dict[str, DrawProfileHandler] = {}
        self._maintenance_bindings: dict[str, MaintenanceHandler] = {}
        self._maintenance_jobs: dict[str, _MaintenanceJob] = {}
        self._fortune_bindings: dict[str, FortuneHandler] = {}
        self._active_session_tasks: dict[str, set[asyncio.Task[object]]] = {}
        self._bindings_lock = asyncio.Lock()

    @property
    def endpoint(self) -> str:
        if not self._endpoint:
            raise AgentToolServerError("agent tool server has not started")
        return self._endpoint

    @property
    def token(self) -> str:
        return self._token

    async def start(self) -> None:
        async with self._start_lock:
            if self._runner is not None:
                return
            app = web.Application(client_max_size=64 * 1024)
            app.router.add_post("/v1/tools/draw-image", self._handle_draw_image)
            app.router.add_post("/v1/tools/project", self._handle_project)
            app.router.add_post("/v1/tools/discord", self._handle_discord)
            app.router.add_post("/v1/tools/draw-profile", self._handle_draw_profile)
            app.router.add_post("/v1/tools/improve-self", self._handle_improve_self)
            app.router.add_post(
                "/v1/tools/improve-self/status",
                self._handle_improve_self_status,
            )
            app.router.add_post("/v1/tools/daily-fortune", self._handle_daily_fortune)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, host="127.0.0.1", port=0)
            try:
                await site.start()
                server = site._server
                sockets = list(server.sockets if server is not None else [])
                if not sockets:
                    raise AgentToolServerError("agent tool server did not expose a socket")
                port = int(sockets[0].getsockname()[1])
            except Exception:
                await runner.cleanup()
                raise
            self._runner = runner
            self._site = site
            self._endpoint = f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        async with self._start_lock:
            runner = self._runner
            self._runner = None
            self._site = None
            self._endpoint = ""
            async with self._bindings_lock:
                self._bindings.clear()
                self._project_bindings.clear()
                self._discord_bindings.clear()
                self._draw_profile_bindings.clear()
                self._maintenance_bindings.clear()
                self._fortune_bindings.clear()
                maintenance_tasks = [job.task for job in self._maintenance_jobs.values()]
                self._maintenance_jobs.clear()
                session_tasks = [
                    task
                    for tasks in self._active_session_tasks.values()
                    for task in tasks
                ]
                self._active_session_tasks.clear()
            all_tasks = list({*maintenance_tasks, *session_tasks})
            for task in all_tasks:
                task.cancel()
            if all_tasks:
                await asyncio.gather(*all_tasks, return_exceptions=True)
            if runner is not None:
                await runner.cleanup()

    @asynccontextmanager
    async def bind_draw_turn(
        self,
        session_id: str,
        handler: DrawHandler,
    ) -> AsyncIterator[None]:
        if not session_id:
            raise ValueError("session_id is required")
        async with self._bindings_lock:
            if session_id in self._bindings:
                raise AgentToolServerError("a draw handler is already bound to this session")
            self._bindings[session_id] = handler
        try:
            yield
        finally:
            async with self._bindings_lock:
                if self._bindings.get(session_id) is handler:
                    self._bindings.pop(session_id, None)

    @asynccontextmanager
    async def bind_project_turn(
        self,
        session_id: str,
        handler: ProjectHandler,
    ) -> AsyncIterator[None]:
        if not session_id:
            raise ValueError("session_id is required")
        async with self._bindings_lock:
            if session_id in self._project_bindings:
                raise AgentToolServerError("a project handler is already bound to this session")
            self._project_bindings[session_id] = handler
        try:
            yield
        finally:
            async with self._bindings_lock:
                if self._project_bindings.get(session_id) is handler:
                    self._project_bindings.pop(session_id, None)

    @asynccontextmanager
    async def bind_discord_turn(
        self,
        session_id: str,
        handler: DiscordHandler,
    ) -> AsyncIterator[None]:
        if not session_id:
            raise ValueError("session_id is required")
        async with self._bindings_lock:
            if session_id in self._discord_bindings:
                raise AgentToolServerError("a Discord handler is already bound to this session")
            self._discord_bindings[session_id] = handler
        try:
            yield
        finally:
            async with self._bindings_lock:
                if self._discord_bindings.get(session_id) is handler:
                    self._discord_bindings.pop(session_id, None)

    @asynccontextmanager
    async def bind_draw_profile_turn(
        self,
        session_id: str,
        handler: DrawProfileHandler,
    ) -> AsyncIterator[None]:
        if not session_id:
            raise ValueError("session_id is required")
        async with self._bindings_lock:
            if session_id in self._draw_profile_bindings:
                raise AgentToolServerError("a draw-profile handler is already bound to this session")
            self._draw_profile_bindings[session_id] = handler
        try:
            yield
        finally:
            async with self._bindings_lock:
                if self._draw_profile_bindings.get(session_id) is handler:
                    self._draw_profile_bindings.pop(session_id, None)

    @asynccontextmanager
    async def bind_maintenance_turn(
        self,
        session_id: str,
        handler: MaintenanceHandler,
    ) -> AsyncIterator[None]:
        if not session_id:
            raise ValueError("session_id is required")
        async with self._bindings_lock:
            if session_id in self._maintenance_bindings:
                raise AgentToolServerError(
                    "a maintenance handler is already bound to this session"
                )
            self._maintenance_bindings[session_id] = handler
        try:
            yield
        finally:
            async with self._bindings_lock:
                if self._maintenance_bindings.get(session_id) is handler:
                    self._maintenance_bindings.pop(session_id, None)

    @asynccontextmanager
    async def bind_fortune_turn(
        self,
        session_id: str,
        handler: FortuneHandler,
    ) -> AsyncIterator[None]:
        if not session_id:
            raise ValueError("session_id is required")
        async with self._bindings_lock:
            if session_id in self._fortune_bindings:
                raise AgentToolServerError("a fortune handler is already bound to this session")
            self._fortune_bindings[session_id] = handler
        try:
            yield
        finally:
            async with self._bindings_lock:
                if self._fortune_bindings.get(session_id) is handler:
                    self._fortune_bindings.pop(session_id, None)

    async def _handle_draw_image(self, request: web.Request) -> web.Response:
        authorization = request.headers.get("Authorization", "")
        expected = f"Bearer {self._token}"
        if not hmac.compare_digest(authorization, expected):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="request must be an object")
        session_id = payload.get("sessionId")
        arguments = payload.get("arguments")
        if not isinstance(session_id, str) or not isinstance(arguments, dict):
            raise web.HTTPBadRequest(text="invalid tool request")
        async with self._bindings_lock:
            handler = self._bindings.get(session_id)
        if handler is None:
            raise web.HTTPForbidden(text="session has no active Discord turn")
        try:
            result = await self._run_session_call(session_id, handler(dict(arguments)))
        except Exception as exc:
            raise web.HTTPInternalServerError(text="draw execution failed") from exc
        if not isinstance(result, dict):
            raise web.HTTPInternalServerError(text="draw handler returned an invalid result")
        return web.json_response(result)

    async def _handle_project(self, request: web.Request) -> web.Response:
        self._require_authorization(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="request must be an object")
        session_id = payload.get("sessionId")
        action = payload.get("action")
        arguments = payload.get("arguments")
        if (
            not isinstance(session_id, str)
            or not isinstance(action, str)
            or not isinstance(arguments, dict)
        ):
            raise web.HTTPBadRequest(text="invalid tool request")
        async with self._bindings_lock:
            handler = self._project_bindings.get(session_id)
        if handler is None:
            raise web.HTTPForbidden(text="session has no active owner-authorized coding turn")
        try:
            result = await self._run_session_call(
                session_id,
                handler(action, dict(arguments)),
            )
        except ProjectToolError as exc:
            raise web.HTTPBadRequest(text=str(exc)[:500]) from exc
        except Exception as exc:
            raise web.HTTPInternalServerError(text="project operation failed") from exc
        if not isinstance(result, dict):
            raise web.HTTPInternalServerError(text="project handler returned an invalid result")
        return web.json_response(result)

    async def _handle_discord(self, request: web.Request) -> web.Response:
        self._require_authorization(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="request must be an object")
        session_id = payload.get("sessionId")
        action = payload.get("action")
        arguments = payload.get("arguments")
        if not isinstance(session_id, str) or not isinstance(action, str) or not isinstance(arguments, dict):
            raise web.HTTPBadRequest(text="invalid Discord tool request")
        async with self._bindings_lock:
            handler = self._discord_bindings.get(session_id)
        if handler is None:
            raise web.HTTPForbidden(text="session has no active Discord capability turn")
        try:
            result = await self._run_session_call(
                session_id,
                handler(action, dict(arguments)),
            )
        except Exception as exc:
            detail = str(exc).strip()[:500] or exc.__class__.__name__
            raise web.HTTPBadRequest(text=detail) from exc
        if not isinstance(result, dict):
            raise web.HTTPInternalServerError(text="Discord handler returned an invalid result")
        return web.json_response(result)

    async def _handle_draw_profile(self, request: web.Request) -> web.Response:
        self._require_authorization(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        arguments = payload.get("arguments", {}) if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not isinstance(arguments, dict):
            raise web.HTTPBadRequest(text="invalid draw-profile request")
        async with self._bindings_lock:
            handler = self._draw_profile_bindings.get(session_id)
        if handler is None:
            raise web.HTTPForbidden(text="session has no active draw-profile turn")
        try:
            result = await self._run_session_call(session_id, handler(dict(arguments)))
        except Exception as exc:
            raise web.HTTPInternalServerError(text="draw-profile lookup failed") from exc
        if not isinstance(result, dict):
            raise web.HTTPInternalServerError(text="draw-profile handler returned an invalid result")
        return web.json_response(result)

    async def _handle_improve_self(self, request: web.Request) -> web.Response:
        self._require_authorization(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="request must be an object")
        session_id = payload.get("sessionId")
        arguments = payload.get("arguments")
        if not isinstance(session_id, str) or not isinstance(arguments, dict):
            raise web.HTTPBadRequest(text="invalid tool request")
        task = arguments.get("task")
        if not isinstance(task, str) or not task.strip() or len(task.strip()) > 4000:
            raise web.HTTPBadRequest(text="task must contain between 1 and 4000 characters")
        async with self._bindings_lock:
            handler = self._maintenance_bindings.get(session_id)
        if handler is None:
            raise web.HTTPForbidden(
                text="session has no active owner-authorized maintenance turn"
            )
        job_id = secrets.token_urlsafe(24)
        job_task = asyncio.create_task(
            self._run_maintenance_job(handler, task.strip()),
            name=f"atri:maintenance:{job_id[:8]}",
        )
        async with self._bindings_lock:
            self._maintenance_jobs[job_id] = _MaintenanceJob(
                session_id=session_id,
                task=job_task,
            )
        return web.json_response(
            {"jobId": job_id, "status": "running"},
            status=202,
        )

    async def _handle_improve_self_status(self, request: web.Request) -> web.Response:
        self._require_authorization(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="request must be an object")
        session_id = payload.get("sessionId")
        job_id = payload.get("jobId")
        if not isinstance(session_id, str) or not isinstance(job_id, str):
            raise web.HTTPBadRequest(text="invalid maintenance job request")
        async with self._bindings_lock:
            job = self._maintenance_jobs.get(job_id)
        if job is None or not hmac.compare_digest(job.session_id, session_id):
            raise web.HTTPNotFound(text="maintenance job not found")
        if not job.task.done():
            return web.json_response(
                {"jobId": job_id, "status": "running"},
                status=202,
            )

        result = await job.task
        async with self._bindings_lock:
            if self._maintenance_jobs.get(job_id) is job:
                self._maintenance_jobs.pop(job_id, None)
        return web.json_response(result)

    @staticmethod
    async def _run_maintenance_job(
        handler: MaintenanceHandler,
        task: str,
    ) -> dict[str, object]:
        try:
            result = await handler(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = (
                describe_dsh_error(exc, max_chars=600)
                if isinstance(exc, DshRuntimeError)
                else exc.__class__.__name__
            )
            print(
                '[ERROR] ATRI maintenance job failed: '
                f'error_type={exc.__class__.__name__}, detail={detail}'
            )
            return {
                "status": "failed",
                "summary": f"维护 Agent 执行失败：{detail}",
                "reviewRequired": True,
            }
        if not isinstance(result, dict):
            print('[ERROR] ATRI maintenance handler returned an invalid result')
            return {
                "status": "failed",
                "summary": "维护 Agent 返回了无效结果，请查看机器人后台日志。",
                "reviewRequired": True,
            }
        return result

    async def _handle_daily_fortune(self, request: web.Request) -> web.Response:
        self._require_authorization(request)
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="request must be an object")
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str):
            raise web.HTTPBadRequest(text="invalid tool request")
        async with self._bindings_lock:
            handler = self._fortune_bindings.get(session_id)
        if handler is None:
            raise web.HTTPForbidden(text="session has no active Discord fortune turn")
        try:
            result = await self._run_session_call(session_id, handler())
        except Exception as exc:
            raise web.HTTPInternalServerError(text="fortune generation failed") from exc
        if not isinstance(result, dict):
            raise web.HTTPInternalServerError(text="fortune handler returned an invalid result")
        return web.json_response(result)

    def _require_authorization(self, request: web.Request) -> None:
        authorization = request.headers.get("Authorization", "")
        expected = f"Bearer {self._token}"
        if not hmac.compare_digest(authorization, expected):
            raise web.HTTPUnauthorized()

    async def cancel_session_work(self, session_id: str) -> dict[str, int]:
        current = asyncio.current_task()
        async with self._bindings_lock:
            session_tasks = [
                task
                for task in self._active_session_tasks.get(session_id, set())
                if task is not current and not task.done()
            ]
            maintenance_job_ids = [
                job_id
                for job_id, job in self._maintenance_jobs.items()
                if hmac.compare_digest(job.session_id, session_id)
            ]
            maintenance_tasks = []
            for job_id in maintenance_job_ids:
                job = self._maintenance_jobs.pop(job_id, None)
                if job is not None and job.task is not current and not job.task.done():
                    maintenance_tasks.append(job.task)
        tasks = list({*session_tasks, *maintenance_tasks})
        for task in tasks:
            task.cancel()
        if tasks:
            done, _pending = await asyncio.wait(tasks, timeout=15.0)
            for task in done:
                try:
                    task.exception()
                except (asyncio.CancelledError, Exception):
                    pass
        return {
            "toolRequests": len(session_tasks),
            "maintenanceJobs": len(maintenance_tasks),
        }

    async def _run_session_call(
        self,
        session_id: str,
        operation: Awaitable[dict[str, object]],
    ) -> dict[str, object]:
        task = asyncio.current_task()
        if task is None:
            return await operation
        async with self._bindings_lock:
            self._active_session_tasks.setdefault(session_id, set()).add(task)
        try:
            return await operation
        finally:
            async with self._bindings_lock:
                tasks = self._active_session_tasks.get(session_id)
                if tasks is not None:
                    tasks.discard(task)
                    if not tasks:
                        self._active_session_tasks.pop(session_id, None)
