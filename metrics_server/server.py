from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            with open("/app/metrics.txt") as f:
                body = f.read().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
