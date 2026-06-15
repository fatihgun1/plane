"""Throwaway mock of the Plane API for local auth testing (not shipped)."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        cookie = self.headers.get("Cookie", "")
        if self.path == "/api/users/me/" and "session-id=valid-token" in cookie:
            body = json.dumps({
                "id": "user-123",
                "email": "fatih@example.com",
                "display_name": "Fatih",
                "avatar_url": "",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(401)
            self.end_headers()

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", 9123), Handler).serve_forever()
