from http.server import BaseHTTPRequestHandler, HTTPServer


class HealthzHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()


def main():
    server = HTTPServer(("0.0.0.0", 8080), HealthzHandler)
    print("listening on :8080")
    server.serve_forever()


if __name__ == "__main__":
    main()
