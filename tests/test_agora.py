import io
import os
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

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


class PipelineStoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(clock=FakeClock())
        self.boss = self.s.register_agent({"name": "cliente"})["id"]
        self.editor = self.s.register_agent({"name": "editor"})["id"]
        self.motion = self.s.register_agent({"name": "motion"})["id"]

    def upload(self, who, data=b"video"):
        return self.s.save_file(who, "a.mp4", "video/mp4", io.BytesIO(data), len(data), 1 << 20)["id"]

    def test_project_hands_files_from_step_to_step(self):
        raw = self.upload(self.boss)
        p = self.s.create_project(self.boss, {
            "title": "Vídeo", "briefing": "60s, ritmo rápido", "files": [raw],
            "steps": [{"title": "Editar", "assigned_to": self.editor},
                      {"title": "Motion", "assigned_to": self.motion}]})
        edit, motion = p["tasks"]
        self.assertEqual((edit["status"], motion["status"]), ("open", "blocked"))
        self.assertEqual(edit["inputs"], [raw])
        with self.assertRaises(Error) as e:
            self.s.claim_task(self.motion, motion["id"])
        self.assertEqual(e.exception.status, 409)  # ainda bloqueada
        with self.assertRaises(Error):
            self.s.claim_task(self.motion, edit["id"])  # designada ao editor

        self.s.claim_task(self.editor, edit["id"])
        cut = self.upload(self.editor, b"cortado")
        self.s.complete_task(self.editor, edit["id"], {"result": "corte pronto", "files": [cut]})

        motion = self.s.get_task(motion["id"])
        self.assertEqual(motion["status"], "open")
        self.assertEqual(motion["inputs"], [cut])
        self.assertEqual(motion["project"]["briefing"], "60s, ritmo rápido")
        self.assertIn("task.unblocked", [e["type"] for e in self.s.events()])

        self.s.claim_task(self.motion, motion["id"])
        final = self.upload(self.motion, b"final")
        self.s.complete_task(self.motion, motion["id"], {"files": [final]})
        p = self.s.get_project(p["id"])
        self.assertEqual(p["status"], "done")
        self.assertEqual([f["id"] for f in p["deliverables"]], [final])
        self.assertEqual(self.s.events(latest=True, limit=1)[0]["type"], "project.completed")

    def test_cancel_cascades_to_later_steps(self):
        p = self.s.create_project(self.boss, {"title": "x", "steps": [{"title": "a"}, {"title": "b"}]})
        self.s.cancel_task(self.boss, p["tasks"][0]["id"])
        self.assertEqual(self.s.get_project(p["id"])["status"], "cancelled")
        self.assertEqual(self.s.get_task(p["tasks"][1]["id"])["status"], "cancelled")

    def test_bad_project_is_not_half_created(self):
        with self.assertRaises(Error) as e:
            self.s.create_project(self.boss, {"title": "x", "steps": [{"title": "a"}, {"title": ""}]})
        self.assertIn("etapa 2", e.exception.message)
        self.assertEqual(self.s.list_projects(), [])
        self.assertEqual(self.s.list_tasks(), [])

    def test_complete_requires_result_or_files_and_known_files(self):
        t = self.s.create_task(self.boss, {"title": "x"})
        self.s.claim_task(self.editor, t["id"])
        with self.assertRaises(Error):
            self.s.complete_task(self.editor, t["id"], {})
        with self.assertRaises(Error) as e:
            self.s.complete_task(self.editor, t["id"], {"files": ["fl_nao_existe"]})
        self.assertEqual(e.exception.status, 404)

    def test_upload_limits(self):
        with self.assertRaises(Error) as e:
            self.s.save_file(self.boss, "a", None, io.BytesIO(b"x" * 10), 10, 5)
        self.assertEqual(e.exception.status, 413)
        with self.assertRaises(Error):
            self.s.save_file(self.boss, "a", None, io.BytesIO(b"abc"), 10, 100)  # corpo curto
        self.assertEqual(os.listdir(self.s.files_dir), [])

    def test_events_long_poll_wakes_up(self):
        last = self.s.events(latest=True, limit=1)[0]["seq"]
        threading.Timer(0.2, lambda: self.s.create_task(self.boss, {"title": "novo"})).start()
        t0 = time.monotonic()
        got = self.s.events(since=last, wait=5)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(got[0]["type"], "task.created")


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

    def test_video_pipeline_over_http(self):
        boss, editor, motion = Agora(self.url), Agora(self.url), Agora(self.url)
        boss.register("cliente")
        editor.register("editor", capabilities=["edicao"])
        motion.register("motion", capabilities=["motion"])
        with tempfile.TemporaryDirectory() as d:
            raw_path = os.path.join(d, "bruto.mp4")
            with open(raw_path, "wb") as f:
                f.write(os.urandom(300_000))
            raw = boss.upload(raw_path)
            self.assertEqual(raw["size"], 300_000)
            p = boss.create_project("Vídeo", briefing="corta e anima", files=[raw["id"]], steps=[
                {"title": "Editar", "tags": ["edicao"], "assigned_to": editor.agent["id"]},
                {"title": "Motion", "tags": ["motion"], "assigned_to": motion.agent["id"]}])

            got = []
            waiter = threading.Thread(target=lambda: got.append(motion.next_task(tag="motion", timeout=10)))
            waiter.start()

            task = editor.next_task(tag="edicao", timeout=5)
            self.assertEqual(task["title"], "Editar")
            local = editor.download(task["inputs"][0], d)
            with open(local, "rb") as f, open(raw_path, "rb") as g:
                self.assertEqual(f.read(), g.read())
            cut = editor.upload(raw_path, name="editado.mp4")
            editor.complete(task["id"], "corte pronto", files=[cut["id"]])

            waiter.join(10)
            self.assertEqual(got[0]["title"], "Motion")
            self.assertEqual(got[0]["inputs"], [cut["id"]])
            motion.complete(got[0]["id"], "motion pronto", files=[cut["id"]])
            self.assertEqual(boss.project(p["id"])["status"], "done")

            req = Request(f"{self.url}/api/files/{cut['id']}", headers={"Range": "bytes=10-19"})
            with urlopen(req) as r:
                self.assertEqual(r.status, 206)
                self.assertEqual(len(r.read()), 10)

    def test_assigned_to_me_filter(self):
        a = Agora(self.url)
        a.register("a")
        a.post("x")
        with self.assertRaises(AgoraError) as e:
            Agora(self.url).tasks(assigned_to="me")
        self.assertEqual(e.exception.status, 401)
        self.assertEqual(a.tasks(created_by="me")[0]["title"], "x")

    def test_not_found(self):
        with self.assertRaises(AgoraError) as e:
            Agora(self.url).task("tk_nao_existe")
        self.assertEqual(e.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
