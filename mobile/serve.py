"""Development file server for the mobile PWA. Serves on port 8421."""

import http.server
import os
import functools

PORT = 8421
DIR = os.path.dirname(os.path.abspath(__file__))

Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=DIR)

if __name__ == "__main__":
    with http.server.HTTPServer(("", PORT), Handler) as httpd:
        print(f"Serving mobile PWA at http://localhost:{PORT}")
        httpd.serve_forever()
