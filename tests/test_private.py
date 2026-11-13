"""Modo privado: sem o código de acesso, nada funciona (nem leitura)."""
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from agora.client import Agora, AgoraError
from agora.server import make_server

CODE = "codigo-secreto-123"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class PrivateModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = make_server(port=0, rate_limit=0, access_code=CODE)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def status(self, path, **kw):
        try:
            with build_opener(NoRedirect).open(Request(self.url + path, **kw)) as r:
                return r.status, r.headers
        except HTTPError as e:
            return e.code, e.headers

    def test_everything_closed_without_code(self):
        for path in ("/api/tasks", "/api/projects", "/llms.txt", "/.well-known/agent.json", "/api/files/fl_x"):
            self.assertEqual(self.status(path)[0], 401, path)
        with self.assertRaises(AgoraError) as e:
            Agora(self.url).register("intruso")
        self.assertEqual(e.exception.status, 401)

    def test_panel_redirects_to_login(self):
        code, headers = self.status("/")
        self.assertEqual((code, headers["Location"]), (303, "/login"))
        self.assertEqual(self.status("/login")[0], 200)
        self.assertEqual(self.status("/healthz")[0], 200)

    def test_agent_with_code_works(self):
        ag = Agora(self.url, access_code=CODE)
        ag.register("meu agente")
        self.assertEqual(ag.post("x")["title"], "x")
        with self.assertRaises(AgoraError):
            Agora(self.url, access_code="errado-errado-errado").stats()

    def test_browser_login_sets_cookie(self):
        code, headers = self.status("/login", data=b"code=errado", method="POST")
        self.assertEqual(code, 401)
        code, headers = self.status("/login", data=f"code={CODE}".encode(), method="POST")
        self.assertEqual(code, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertNotIn(CODE, cookie)  # o cookie não carrega o código em si
        with urlopen(Request(self.url + "/", headers={"Cookie": cookie})) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(self.status("/api/stats", headers={"Cookie": "agora_access=falso"})[0], 401)

    def test_agent_keeps_identity_across_restarts(self):
        first = Agora(self.url, access_code=CODE).join("Editor", capabilities=["edicao"], identity="editor")
        again = Agora(self.url, access_code=CODE).join("Editor v2", capabilities=["edicao"], identity="editor")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["name"], "Editor v2")
        other = Agora(self.url, access_code=CODE).join("Motion", identity="motion")
        self.assertNotEqual(other["id"], first["id"])
        with self.assertRaises(AgoraError) as e:
            Agora(self.url, access_code=CODE)._call("POST", "/api/agents", {"name": "x", "key": "ak_curta"})
        self.assertEqual(e.exception.status, 400)

    def test_short_code_refused(self):
        with self.assertRaises(SystemExit):
            make_server(port=0, access_code="curto")


if __name__ == "__main__":
    unittest.main()
