import threading
import unittest
from urllib.request import urlopen

from agora.client import Agora, AgoraError
from agora.server import make_server
from agora.store import Error, Store


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = Store(clock=self.clock)
        self.a = self.s.register_agent({"name": "a", "capabilities": ["Python"]})["id"]
        self.b = self.s.register_agent({"name": "b"})["id"]

    def test_full_lifecycle(self):
        t = self.s.create_task(self.a, {"title": "x", "tags": ["py"]})
        self.assertEqual(t["status"], "open")
        self.s.claim_task(self.b, t["id"])
        done = self.s.complete_task(self.b, t["id"], {"result": "ok"})
        self.assertEqual((done["status"], done["result"]), ("done", "ok"))

    def test_only_one_claimer(self):
        t = self.s.create_task(self.a, {"title": "x"})
        self.s.claim_task(self.a, t["id"])
        with self.assertRaises(Error) as e:
            self.s.claim_task(self.b, t["id"])
        self.assertEqual(e.exception.status, 409)
        with self.assertRaises(Error):
            self.s.complete_task(self.b, t["id"], {"result": "x"})

    def test_claim_expires(self):
        t = self.s.create_task(self.a, {"title": "x"})
        self.s.claim_task(self.a, t["id"], lease_seconds=10)
        self.clock.t += 11
        self.assertEqual(self.s.get_task(t["id"])["status"], "open")
        self.s.claim_task(self.b, t["id"])
        self.assertIn("task.expired", [e["type"] for e in self.s.events()])

    def test_release_and_renew(self):
        t = self.s.create_task(self.a, {"title": "x"})
        self.s.claim_task(self.a, t["id"], lease_seconds=10)
        renewed = self.s.claim_task(self.a, t["id"], lease_seconds=100)
        self.assertEqual(renewed["claim_expires"], self.clock.t + 100)
        self.assertEqual(self.s.release_task(self.a, t["id"])["status"], "open")

    def test_cancel_only_by_author(self):
        t = self.s.create_task(self.a, {"title": "x"})
        with self.assertRaises(Error) as e:
            self.s.cancel_task(self.b, t["id"])
        self.assertEqual(e.exception.status, 403)
        self.assertEqual(self.s.cancel_task(self.a, t["id"])["status"], "cancelled")

    def test_filters_and_validation(self):
        self.s.create_task(self.a, {"title": "low", "tags": ["py"], "priority": -1})
        self.s.create_task(self.a, {"title": "high", "tags": ["go"], "priority": 5})
        self.assertEqual([t["title"] for t in self.s.list_tasks()], ["high", "low"])
        self.assertEqual([t["title"] for t in self.s.list_tasks(tag="PY")], ["low"])
        self.assertEqual(len(self.s.list_agents(capability="python")), 1)
        for bad in ({}, {"title": "  "}, {"title": "x", "priority": 99}, {"title": "x", "tags": "py"}):
            with self.assertRaises(Error):
                self.s.create_task(self.a, bad)

    def test_events_latest(self):
        for i in range(5):
            self.s.create_task(self.a, {"title": str(i)})
        latest = self.s.events(limit=2, latest=True)
        self.assertEqual([e["data"]["title"] for e in latest], ["3", "4"])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = make_server(port=0, rate_limit=0)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_two_agents_collaborate(self):
        boss, worker = Agora(self.url), Agora(self.url)
        boss.register("chefe")
        worker.register("operario", capabilities=["resumo"])
        task = boss.post("Resumir artigo", tags=["resumo"], priority=3)
        open_ = worker.tasks(status="open", tag="resumo")
        self.assertEqual(open_[0]["id"], task["id"])
        worker.claim(task["id"])
        boss.comment(task["id"], "prazo: hoje")
        worker.complete(task["id"], "resumo pronto")
        final = boss.task(task["id"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["comments"][0]["body"], "prazo: hoje")
        self.assertEqual(worker.me()["name"], "operario")

    def test_auth_required_for_writes(self):
        with self.assertRaises(AgoraError) as e:
            Agora(self.url).post("x")
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(AgoraError) as e:
            Agora(self.url, key="falsa").post("x")
        self.assertEqual(e.exception.status, 401)

    def test_discovery_endpoints(self):
        for path in ("/", "/llms.txt", "/.well-known/agent.json", "/api/stats"):
            with urlopen(self.url + path) as r:
                self.assertEqual(r.status, 200)
        with urlopen(self.url + "/llms.txt") as r:
            self.assertIn(self.url.split("//")[1], r.read().decode())

    def test_not_found(self):
        with self.assertRaises(AgoraError) as e:
            Agora(self.url).task("tk_nao_existe")
        self.assertEqual(e.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
