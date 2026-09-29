from __future__ import annotations

import asyncio
import unittest

import aiohttp

from chat.agent.tool_server import AgentToolServer


class AgentToolServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = AgentToolServer()
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.close()

    async def post_project(
        self,
        *,
        token: str,
        session_id: str,
        action: str = "status",
    ) -> tuple[int, dict | str]:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.server.endpoint}/v1/tools/project",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "sessionId": session_id,
                    "action": action,
                    "arguments": {},
                },
            ) as response:
                if response.content_type == "application/json":
                    return response.status, await response.json()
                return response.status, await response.text()

    async def post_discord(
        self,
        *,
        token: str,
        session_id: str,
        action: str = "context",
    ) -> tuple[int, dict | str]:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.server.endpoint}/v1/tools/discord",
                headers={"Authorization": f"Bearer {token}"},
                json={"sessionId": session_id, "action": action, "arguments": {}},
            ) as response:
                if response.content_type == "application/json":
                    return response.status, await response.json()
                return response.status, await response.text()

    async def post_maintenance(
        self,
        *,
        token: str,
        session_id: str,
        task: str = "improve the music tool",
    ) -> tuple[int, dict | str]:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.server.endpoint}/v1/tools/improve-self",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "sessionId": session_id,
                    "arguments": {"task": task},
                },
            ) as response:
                if response.content_type == "application/json":
                    return response.status, await response.json()
                return response.status, await response.text()

    async def post_maintenance_status(
        self,
        *,
        token: str,
        session_id: str,
        job_id: str,
    ) -> tuple[int, dict | str]:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.server.endpoint}/v1/tools/improve-self/status",
                headers={"Authorization": f"Bearer {token}"},
                json={"sessionId": session_id, "jobId": job_id},
            ) as response:
                if response.content_type == "application/json":
                    return response.status, await response.json()
                return response.status, await response.text()

    async def wait_for_maintenance(
        self,
        *,
        session_id: str,
        job_id: str,
    ) -> tuple[int, dict | str]:
        for _attempt in range(100):
            status, body = await self.post_maintenance_status(
                token=self.server.token,
                session_id=session_id,
                job_id=job_id,
            )
            if status != 202:
                return status, body
            await asyncio.sleep(0.01)
        self.fail("maintenance job did not finish")

    async def test_removed_drawing_routes_are_unavailable(self) -> None:
        async with aiohttp.ClientSession() as session:
            for route in ("draw-image", "draw-profile"):
                async with session.post(
                    f"{self.server.endpoint}/v1/tools/{route}",
                    headers={"Authorization": f"Bearer {self.server.token}"},
                    json={"sessionId": "session-1", "arguments": {}},
                ) as response:
                    self.assertEqual(response.status, 404)
        self.assertFalse(hasattr(self.server, "bind_draw_turn"))
        self.assertFalse(hasattr(self.server, "bind_draw_profile_turn"))

    async def test_rejects_wrong_token(self) -> None:
        status, _body = await self.post_discord(token="wrong", session_id="session-1")
        self.assertEqual(status, 401)

    async def test_rejects_session_without_active_discord_turn(self) -> None:
        status, _body = await self.post_discord(token=self.server.token, session_id="session-1")
        self.assertEqual(status, 403)

    async def test_project_tools_require_separate_owner_authorized_binding(self) -> None:
        received: list[tuple[str, dict[str, object]]] = []

        async def handler(
            action: str,
            arguments: dict[str, object],
        ) -> dict[str, object]:
            received.append((action, arguments))
            return {"summary": "ok", "content": "clean", "truncated": False}

        unbound_status, _ = await self.post_project(
            token=self.server.token,
            session_id="session-private",
        )
        async with self.server.bind_project_turn("session-private", handler):
            wrong_status, _ = await self.post_project(
                token=self.server.token,
                session_id="session-public",
            )
            good_status, body = await self.post_project(
                token=self.server.token,
                session_id="session-private",
            )

        self.assertEqual(unbound_status, 403)
        self.assertEqual(wrong_status, 403)
        self.assertEqual(good_status, 200)
        self.assertEqual(body, {"summary": "ok", "content": "clean", "truncated": False})
        self.assertEqual(received, [("status", {})])

    async def test_discord_tools_require_exact_turn_binding(self) -> None:
        received: list[tuple[str, dict[str, object]]] = []

        async def handler(action: str, arguments: dict[str, object]) -> dict[str, object]:
            received.append((action, arguments))
            return {"summary": "current guild", "content": "{}", "truncated": False}

        unbound_status, _ = await self.post_discord(
            token=self.server.token,
            session_id="session-owner",
        )
        async with self.server.bind_discord_turn("session-owner", handler):
            wrong_status, _ = await self.post_discord(
                token=self.server.token,
                session_id="session-other",
            )
            good_status, body = await self.post_discord(
                token=self.server.token,
                session_id="session-owner",
            )

        self.assertEqual(unbound_status, 403)
        self.assertEqual(wrong_status, 403)
        self.assertEqual(good_status, 200)
        self.assertEqual(body["summary"], "current guild")
        self.assertEqual(received, [("context", {})])

    async def test_natural_maintenance_requires_exact_owner_turn_binding(self) -> None:
        received: list[str] = []

        async def handler(task: str) -> dict[str, object]:
            received.append(task)
            return {
                "status": "completed",
                "summary": "tool updated",
                "reviewRequired": True,
            }

        unbound_status, _ = await self.post_maintenance(
            token=self.server.token,
            session_id="session-owner",
        )
        async with self.server.bind_maintenance_turn("session-owner", handler):
            wrong_status, _ = await self.post_maintenance(
                token=self.server.token,
                session_id="session-other",
            )
            submit_status, submission = await self.post_maintenance(
                token=self.server.token,
                session_id="session-owner",
            )
            self.assertEqual(submit_status, 202)
            self.assertIsInstance(submission, dict)
            job_id = submission["jobId"]
            good_status, body = await self.wait_for_maintenance(
                session_id="session-owner",
                job_id=job_id,
            )

        self.assertEqual(unbound_status, 403)
        self.assertEqual(wrong_status, 403)
        self.assertEqual(good_status, 200)
        self.assertEqual(body["summary"], "tool updated")
        self.assertEqual(received, ["improve the music tool"])

    async def test_maintenance_submission_returns_while_job_is_running(self) -> None:
        release = asyncio.Event()

        async def handler(_task: str) -> dict[str, object]:
            await release.wait()
            return {
                "status": "completed",
                "summary": "finished later",
                "reviewRequired": True,
            }

        async with self.server.bind_maintenance_turn("session-owner", handler):
            submit_status, submission = await self.post_maintenance(
                token=self.server.token,
                session_id="session-owner",
            )
            self.assertEqual(submit_status, 202)
            self.assertIsInstance(submission, dict)
            job_id = submission["jobId"]

            running_status, running = await self.post_maintenance_status(
                token=self.server.token,
                session_id="session-owner",
                job_id=job_id,
            )
            wrong_status, _ = await self.post_maintenance_status(
                token=self.server.token,
                session_id="session-other",
                job_id=job_id,
            )
            self.assertEqual(running_status, 202)
            self.assertEqual(running["status"], "running")
            self.assertEqual(wrong_status, 404)

            release.set()
            done_status, done = await self.wait_for_maintenance(
                session_id="session-owner",
                job_id=job_id,
            )

        self.assertEqual(done_status, 200)
        self.assertEqual(done["summary"], "finished later")

    async def test_maintenance_failure_becomes_a_terminal_tool_result(self) -> None:
        async def handler(_task: str) -> dict[str, object]:
            raise RuntimeError("private failure detail")

        async with self.server.bind_maintenance_turn("session-owner", handler):
            submit_status, submission = await self.post_maintenance(
                token=self.server.token,
                session_id="session-owner",
            )
            self.assertEqual(submit_status, 202)
            self.assertIsInstance(submission, dict)
            done_status, done = await self.wait_for_maintenance(
                session_id="session-owner",
                job_id=submission["jobId"],
            )

        self.assertEqual(done_status, 200)
        self.assertEqual(done["status"], "failed")
        self.assertNotIn("private failure detail", done["summary"])

    async def test_cancel_session_work_stops_active_tool_request(self) -> None:
        started = asyncio.Event()

        async def operation() -> dict[str, object]:
            started.set()
            await asyncio.Event().wait()
            return {"summary": "never", "content": "", "truncated": False}

        running = asyncio.create_task(
            self.server._run_session_call("session-owner", operation())
        )
        await started.wait()

        counts = await self.server.cancel_session_work("session-owner")
        results = await asyncio.gather(running, return_exceptions=True)

        self.assertEqual(counts["toolRequests"], 1)
        self.assertIsInstance(results[0], asyncio.CancelledError)

    async def test_removed_fortune_route_and_binding_are_unavailable(self) -> None:
        self.assertFalse(hasattr(self.server, 'bind_fortune_turn'))
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.server.endpoint}/v1/tools/daily-fortune",
                headers={"Authorization": f"Bearer {self.server.token}"},
                json={"sessionId": "session-user", "arguments": {}},
            ) as response:
                self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
