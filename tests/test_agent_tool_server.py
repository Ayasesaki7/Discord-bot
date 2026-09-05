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

    async def post(self, *, token: str, session_id: str) -> tuple[int, dict | str]:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.server.endpoint}/v1/tools/draw-image",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "sessionId": session_id,
                    "arguments": {"request": "draw a blue flower"},
                },
            ) as response:
                if response.content_type == "application/json":
                    return response.status, await response.json()
                return response.status, await response.text()

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

    async def post_draw_profile(
        self,
        *,
        token: str,
        session_id: str,
    ) -> tuple[int, dict | str]:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.server.endpoint}/v1/tools/draw-profile",
                headers={"Authorization": f"Bearer {token}"},
                json={"sessionId": session_id},
            ) as response:
                if response.content_type == "application/json":
                    return response.status, await response.json()
                return response.status, await response.text()

    async def post_maintenance(
        self,
        *,
        token: str,
        session_id: str,
        task: str = "improve the drawing tool",
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

    async def post_fortune(
        self,
        *,
        token: str,
        session_id: str,
    ) -> tuple[int, dict | str]:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.server.endpoint}/v1/tools/daily-fortune",
                headers={"Authorization": f"Bearer {token}"},
                json={"sessionId": session_id, "arguments": {}},
            ) as response:
                if response.content_type == "application/json":
                    return response.status, await response.json()
                return response.status, await response.text()

    async def test_rejects_wrong_token(self) -> None:
        status, _body = await self.post(token="wrong", session_id="session-1")
        self.assertEqual(status, 401)

    async def test_rejects_session_without_active_discord_turn(self) -> None:
        status, _body = await self.post(token=self.server.token, session_id="session-1")
        self.assertEqual(status, 403)

    async def test_dispatches_only_to_exact_bound_session(self) -> None:
        received: list[dict[str, object]] = []

        async def handler(arguments: dict[str, object]) -> dict[str, object]:
            received.append(arguments)
            return {"status": "sent", "summary": "image sent"}

        async with self.server.bind_draw_turn("session-private", handler):
            wrong_status, _ = await self.post(
                token=self.server.token,
                session_id="session-public",
            )
            good_status, body = await self.post(
                token=self.server.token,
                session_id="session-private",
            )

        self.assertEqual(wrong_status, 403)
        self.assertEqual(good_status, 200)
        self.assertEqual(body, {"status": "sent", "summary": "image sent"})
        self.assertEqual(received, [{"request": "draw a blue flower"}])

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

    async def test_draw_profile_is_bound_to_exact_requester_session(self) -> None:
        async def handler(_arguments: dict[str, object]) -> dict[str, object]:
            return {"summary": "one artist", "content": "{}", "truncated": False}

        async with self.server.bind_draw_profile_turn("session-user", handler):
            wrong_status, _ = await self.post_draw_profile(
                token=self.server.token,
                session_id="session-other",
            )
            good_status, body = await self.post_draw_profile(
                token=self.server.token,
                session_id="session-user",
            )

        self.assertEqual(wrong_status, 403)
        self.assertEqual(good_status, 200)
        self.assertEqual(body["summary"], "one artist")

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
        self.assertEqual(received, ["improve the drawing tool"])

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

    async def test_fortune_identity_is_fixed_by_exact_turn_binding(self) -> None:
        calls = 0

        async def handler() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "fromCache": False,
                "summary": "steady",
                "sign": "云水签",
                "omen": "中吉",
                "luckScore": 80,
            }

        async with self.server.bind_fortune_turn("session-user", handler):
            wrong_status, _ = await self.post_fortune(
                token=self.server.token,
                session_id="session-other",
            )
            good_status, body = await self.post_fortune(
                token=self.server.token,
                session_id="session-user",
            )

        self.assertEqual(wrong_status, 403)
        self.assertEqual(good_status, 200)
        self.assertEqual(body["luckScore"], 80)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
