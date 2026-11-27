// Fixture: a Node.js HTTP server (no dependencies) with a tiny router.
const http = require("node:http");
const routes = [];
const app = { get: (path, fn) => routes.push(["GET", path, fn]), post: (path, fn) => routes.push(["POST", path, fn]) };

app.get('/stats', (url) => {
  const words = (url.searchParams.get("text") || "hello world").trim().split(/\s+/);
  return { words: words.length, longest: words.reduce((a, b) => (b.length > a.length ? b : a), "") };
});
app.get('/healthz', () => ({ ok: true }));

http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  const hit = routes.find(([m, p]) => m === req.method && p === url.pathname);
  res.setHeader("Content-Type", "application/json");
  if (!hit) { res.statusCode = 404; return res.end('{"error":"not found"}'); }
  res.end(JSON.stringify(hit[2](url)));
}).listen(Number(process.env.PORT || 3000), process.env.HOST || "127.0.0.1");
