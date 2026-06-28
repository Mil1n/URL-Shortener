import io
import json
import os
import tempfile
import unittest

os.environ["SHORTENER_API_KEY"] = "test-key"

from src import server


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        server.DB_PATH = self.tmp.name
        server.API_KEY = "test-key"
        server.BASE_URL = "http://sho.rt"
        server._RATE_LIMITS.clear()
        server.init_db()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def request(self, method, path, body=None, headers=None, remote_addr="203.0.113.10"):
        payload = b""
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "wsgi.input": io.BytesIO(payload),
            "CONTENT_LENGTH": str(len(payload)),
            "REMOTE_ADDR": remote_addr,
            "HTTP_HOST": "sho.rt",
            "wsgi.url_scheme": "http",
        }
        for key, value in (headers or {}).items():
            environ[key] = value
        capture = {}

        def start_response(status, response_headers):
            capture["status"] = status
            capture["headers"] = dict(response_headers)

        response = b"".join(server.app(environ, start_response))
        capture["body"] = response
        return capture

    def api_headers(self):
        return {"HTTP_X_API_KEY": "test-key"}

    def test_create_redirect_and_stats(self):
        created = self.request(
            "POST",
            "/api/links",
            {"destination_url": "https://example.com/landing", "slug": "launch-2026"},
            self.api_headers(),
        )
        self.assertTrue(created["status"].startswith("201"))
        self.assertEqual(json.loads(created["body"])["short_url"], "http://sho.rt/launch-2026")

        redirected = self.request("GET", "/launch-2026", headers={"HTTP_USER_AGENT": "Mozilla", "HTTP_REFERER": "https://ref.example"})
        self.assertTrue(redirected["status"].startswith("302"))
        self.assertEqual(redirected["headers"]["Location"], "https://example.com/landing")

        stats = self.request("GET", "/api/links/launch-2026/stats", headers=self.api_headers())
        body = json.loads(stats["body"])
        self.assertEqual(body["total_clicks"], 1)
        self.assertEqual(body["unique_clicks"], 1)
        self.assertEqual(body["top_referrers"][0]["referrer"], "https://ref.example")

    def test_rejects_invalid_slug_and_unsafe_url(self):
        bad_slug = self.request(
            "POST",
            "/api/links",
            {"destination_url": "https://example.com", "slug": "api"},
            self.api_headers(),
        )
        self.assertTrue(bad_slug["status"].startswith("400"))

        bad_url = self.request(
            "POST",
            "/api/links",
            {"destination_url": "javascript:alert(1)", "slug": "safe-slug"},
            self.api_headers(),
        )
        self.assertTrue(bad_url["status"].startswith("400"))

    def test_patch_delete_list_and_preview(self):
        self.request("POST", "/api/links", {"destination_url": "https://example.com", "slug": "editable"}, self.api_headers())
        patched = self.request("PATCH", "/api/links/editable", {"is_active": False}, self.api_headers())
        self.assertFalse(json.loads(patched["body"])["is_active"])

        gone = self.request("GET", "/editable")
        self.assertTrue(gone["status"].startswith("410"))

        listed = self.request("GET", "/api/links", headers=self.api_headers())
        self.assertEqual(json.loads(listed["body"])["links"][0]["slug"], "editable")

        preview = self.request("GET", "/preview/editable")
        self.assertEqual(json.loads(preview["body"])["slug"], "editable")

    def test_utm_and_csv_import_and_qr(self):
        created = self.request(
            "POST",
            "/api/links",
            {"destination_url": "https://example.com", "slug": "utm-link", "utm_source": "newsletter", "utm_campaign": "summer"},
            self.api_headers(),
        )
        self.assertIn("utm_source=newsletter", json.loads(created["body"])["destination_url"])

        csv_payload = b"destination_url,slug\nhttps://example.org,csv-link\n"
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/links/import",
            "wsgi.input": io.BytesIO(csv_payload),
            "CONTENT_LENGTH": str(len(csv_payload)),
            "REMOTE_ADDR": "203.0.113.10",
            "HTTP_X_API_KEY": "test-key",
            "HTTP_HOST": "sho.rt",
            "wsgi.url_scheme": "http",
        }
        capture = {}

        def start_response(status, response_headers):
            capture["status"] = status
            capture["headers"] = dict(response_headers)

        capture["body"] = b"".join(server.app(environ, start_response))
        self.assertTrue(capture["status"].startswith("201"))

        qr = self.request("GET", "/qr/csv-link")
        self.assertTrue(qr["status"].startswith("200"))
        self.assertEqual(qr["headers"]["Content-Type"], "image/svg+xml; charset=utf-8")


if __name__ == "__main__":
    unittest.main()
