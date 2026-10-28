const http = require("http");
const fs = require("fs");
const path = require("path");
const { exec } = require("child_process");

const port = Number(process.env.PORT || 8080);
const root = __dirname;
const debug = process.argv.includes("--debug") || process.env.NEON_RIFT_DEBUG === "1";
const logFile = path.join(root, "neon_rift_debug.log");
const llmBackend = String(process.env.NEON_RIFT_LLM_BACKEND || "ollama").toLowerCase();
const llamaPort = Number(process.env.LLAMA_CPP_PORT || 8091);
const llamaModelName = String(process.env.LLAMA_CPP_MODEL_NAME || "local-gguf");
const llamaHome = String(process.env.LLAMA_HOME || "");
const llamaModelPath = String(process.env.LLAMA_CPP_MODEL_PATH || "");
const ollamaBaseUrlRaw = String(process.env.OLLAMA_BASE_URL || "http://127.0.0.1:11434");

function parseBaseUrl(rawUrl, fallbackHost, fallbackPort) {
  try {
    const url = new URL(rawUrl);
    const basePath = (url.pathname || "/").replace(/\/$/, "");
    return {
      hostname: url.hostname || fallbackHost,
      port: Number(url.port || (url.protocol === "https:" ? 443 : 80)) || fallbackPort,
      basePath: basePath === "/" ? "" : basePath
    };
  } catch (_) {
    return { hostname: fallbackHost, port: fallbackPort, basePath: "" };
  }
}

function joinPath(basePath, tailPath) {
  const left = (basePath || "").replace(/\/$/, "");
  const right = (tailPath || "/").startsWith("/") ? tailPath : `/${tailPath}`;
  return `${left}${right}` || "/";
}

const ollamaUpstream = parseBaseUrl(ollamaBaseUrlRaw, "127.0.0.1", 11434);
const startedAt = new Date();

if (debug) {
  fs.writeFileSync(logFile, `[${startedAt.toISOString()}] Neon Rift debug log started\n`);
}

function log(message, data) {
  if (!debug) return;
  const suffix = data === undefined ? "" : ` ${typeof data === "string" ? data : JSON.stringify(data)}`;
  const line = `[${new Date().toISOString()}] ${message}${suffix}`;
  console.log(line);
  fs.appendFile(logFile, line + "\n", () => {});
}

function send(res, status, headers, body) {
  res.writeHead(status, headers);
  res.end(body);
}

function contentType(file) {
  if (file.endsWith(".html")) return "text/html; charset=utf-8";
  if (file.endsWith(".js")) return "text/javascript; charset=utf-8";
  if (file.endsWith(".css")) return "text/css; charset=utf-8";
  return "application/octet-stream";
}

function proxyOllama(req, res) {
  if (handleOllamaOpenAiCompat(req, res)) return;
  proxyUpstream(req, res, "/ollama", ollamaUpstream.port, "ollama", ollamaUpstream.hostname, ollamaUpstream.basePath);
}

function ollamaRequest(method, requestPath, payload) {
  return new Promise((resolve, reject) => {
    const body = payload ? JSON.stringify(payload) : "";
    const req = http.request({
      hostname: ollamaUpstream.hostname,
      port: ollamaUpstream.port,
      path: requestPath,
      method,
      headers: {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(body)
      }
    }, upstream => {
      const responseChunks = [];
      upstream.on("data", chunk => responseChunks.push(chunk));
      upstream.on("end", () => {
        const responseBody = Buffer.concat(responseChunks).toString("utf8");
        let parsed = null;
        try {
          parsed = responseBody ? JSON.parse(responseBody) : {};
        } catch (_) {
          parsed = null;
        }
        resolve({
          status: upstream.statusCode || 502,
          data: parsed,
          raw: responseBody
        });
      });
    });

    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

function normalizeThinkValue(rawThink) {
  if (typeof rawThink === "boolean") return rawThink;
  if (typeof rawThink === "string") {
    const value = rawThink.trim().toLowerCase();
    if (value === "true") return true;
    if (value === "false") return false;
    if (value === "low" || value === "medium" || value === "high") return value;
  }
  return undefined;
}

async function handleOllamaOpenAiCompat(req, res) {
  const targetPath = req.url.replace(/^\/ollama/, "") || "/";
  if (!/^\/v1\b/.test(ollamaUpstream.basePath || "")) return false;

  if (req.method === "GET" && targetPath === "/api/tags") {
    try {
      const upstream = await ollamaRequest("GET", joinPath(ollamaUpstream.basePath, "/models"));
      let modelNames = [];
      if (upstream.status >= 200 && upstream.status < 300 && upstream.data && Array.isArray(upstream.data.data)) {
        modelNames = upstream.data.data
          .map(row => row && row.id)
          .filter(Boolean)
          .map(String);
      }
      if (!modelNames.length && llamaModelName) modelNames = [llamaModelName];
      sendJson(res, 200, { models: modelNames.map(name => ({ name })) });
    } catch (error) {
      log("ollama-openai tags error", { error: error.message });
      sendJson(res, 502, { error: `Backend /v1 non raggiungibile su ${ollamaUpstream.hostname}:${ollamaUpstream.port}` });
    }
    return true;
  }

  if (req.method === "POST" && targetPath === "/api/generate") {
    try {
      const payload = await readJson(req);
      const model = String(payload.model || llamaModelName || "");
      const prompt = String(payload.prompt || "");
      const options = payload.options || {};
      const normalizedThink = normalizeThinkValue(payload.think);
      const chatPayload = {
        model,
        stream: false,
        messages: [{ role: "user", content: prompt }],
        options: {}
      };

      if (normalizedThink !== undefined) {
        chatPayload.think = normalizedThink;
      }

      const temperature = Number(options.temperature);
      if (Number.isFinite(temperature)) chatPayload.options.temperature = temperature;

      const numPredict = Number(options.num_predict);
      if (Number.isFinite(numPredict) && numPredict > 0) chatPayload.options.num_predict = Math.floor(numPredict);

      if (Object.keys(chatPayload.options).length === 0) {
        delete chatPayload.options;
      }

      if (payload.format === "json") {
        chatPayload.format = "json";
      }

      // OLLAMA_BASE_URL may point to /v1, but `think` is defined for native /api/chat.
      // Use native endpoint explicitly to avoid reasoning-only responses on /v1 wrappers.
      const upstream = await ollamaRequest("POST", "/api/chat", chatPayload);
      if (upstream.status < 200 || upstream.status >= 300 || !upstream.data) {
        log("ollama-openai generate upstream error", { status: upstream.status, raw: upstream.raw.slice(0, 500) });
        sendJson(res, 502, {
          error: `Backend /v1 errore HTTP ${upstream.status}`,
          upstream: upstream.raw.slice(0, 800)
        });
        return true;
      }

      const message = (upstream.data && upstream.data.message) ? upstream.data.message : null;
      const text = String((message && message.content) || "");
      const thinking = String((message && message.thinking) || "");
      const doneReason = String((upstream.data && upstream.data.done_reason) || "stop");

      sendJson(res, 200, {
        model,
        created_at: new Date().toISOString(),
        response: text,
        thinking,
        done: true,
        done_reason: doneReason
      });
    } catch (error) {
      log("ollama-openai generate error", { error: error.message });
      sendJson(res, 502, { error: error.message || "Errore backend /v1" });
    }
    return true;
  }

  return false;
}

function findListeningPidByPort(targetPort) {
  return new Promise((resolve) => {
    if (process.platform === "win32") {
      exec(`netstat -ano -p tcp | findstr :${targetPort}`, (error, stdout) => {
        if (error || !stdout) return resolve(null);
        const lines = String(stdout).split(/\r?\n/).map(s => s.trim()).filter(Boolean);
        for (const line of lines) {
          if (!/LISTENING/i.test(line)) continue;
          const pid = Number(line.split(/\s+/).pop());
          if (Number.isFinite(pid) && pid > 0) return resolve(pid);
        }
        resolve(null);
      });
      return;
    }

    exec(`lsof -iTCP:${targetPort} -sTCP:LISTEN -t`, (error, stdout) => {
      if (error || !stdout) return resolve(null);
      const pid = Number(String(stdout).split(/\r?\n/).map(s => s.trim()).filter(Boolean)[0]);
      resolve(Number.isFinite(pid) && pid > 0 ? pid : null);
    });
  });
}

async function unloadLlamaProcess(res) {
  try {
    const pid = await findListeningPidByPort(llamaPort);
    if (!pid) {
      sendJson(res, 200, { ok: true, unloaded: false, reason: `nessun processo in ascolto su :${llamaPort}` });
      return;
    }

    if (process.platform === "win32") {
      exec(`taskkill /PID ${pid} /F`, (error, stdout, stderr) => {
        if (error) {
          sendJson(res, 500, { ok: false, error: stderr || error.message, pid, port: llamaPort });
          return;
        }
        sendJson(res, 200, { ok: true, unloaded: true, pid, port: llamaPort, output: String(stdout || "").trim() });
      });
      return;
    }

    process.kill(pid, "SIGTERM");
    sendJson(res, 200, { ok: true, unloaded: true, pid, port: llamaPort });
  } catch (error) {
    sendJson(res, 500, { ok: false, error: error.message || "unload failed", port: llamaPort });
  }
}

function modelDisplayNameFromPath(filePath) {
  if (!filePath) return "";
  const base = path.basename(filePath);
  return base.endsWith(".gguf") ? base : base + ".gguf";
}

function listLlamaModelsFromDisk() {
  const roots = [];
  if (llamaHome) {
    roots.push(path.join(llamaHome, "models"));
    roots.push(llamaHome);
  }
  if (llamaModelPath) {
    roots.push(path.dirname(llamaModelPath));
  }

  const uniqueRoots = roots
    .filter(Boolean)
    .map(p => path.resolve(p))
    .filter((p, i, arr) => arr.indexOf(p) === i && fs.existsSync(p));

  const names = new Set();
  for (const rootDir of uniqueRoots) {
    const stack = [rootDir];
    while (stack.length) {
      const current = stack.pop();
      let entries = [];
      try {
        entries = fs.readdirSync(current, { withFileTypes: true });
      } catch (_) {
        continue;
      }
      for (const entry of entries) {
        const full = path.join(current, entry.name);
        if (entry.isDirectory()) {
          stack.push(full);
          continue;
        }
        if (!entry.isFile()) continue;
        if (/\.gguf$/i.test(entry.name)) names.add(entry.name);
      }
    }
  }

  if (llamaModelPath) names.add(modelDisplayNameFromPath(llamaModelPath));
  if (llamaModelName) names.add(String(llamaModelName));
  return Array.from(names).filter(Boolean).sort((a, b) => a.localeCompare(b));
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("error", reject);
    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        resolve(raw ? JSON.parse(raw) : {});
      } catch (error) {
        reject(error);
      }
    });
  });
}

function sendJson(res, status, payload) {
  send(res, status, {
    "content-type": "application/json",
    "access-control-allow-origin": "*"
  }, JSON.stringify(payload));
}

function llamaRequest(method, requestPath, payload) {
  return new Promise((resolve, reject) => {
    const body = payload ? JSON.stringify(payload) : "";
    const req = http.request({
      hostname: "127.0.0.1",
      port: llamaPort,
      path: requestPath,
      method,
      headers: {
        "content-type": "application/json",
        "content-length": Buffer.byteLength(body)
      }
    }, upstream => {
      const responseChunks = [];
      upstream.on("data", chunk => responseChunks.push(chunk));
      upstream.on("end", () => {
        const responseBody = Buffer.concat(responseChunks).toString("utf8");
        let parsed = null;
        try {
          parsed = responseBody ? JSON.parse(responseBody) : {};
        } catch (_) {
          parsed = null;
        }
        resolve({
          status: upstream.statusCode || 502,
          data: parsed,
          raw: responseBody
        });
      });
    });

    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

async function handleOllamaCompat(req, res) {
  const targetPath = req.url.replace(/^\/ollama/, "") || "/";

  if (req.method === "GET" && targetPath === "/api/tags") {
    try {
      let modelNames = listLlamaModelsFromDisk();
      if (!modelNames.length) {
        const upstream = await llamaRequest("GET", "/v1/models");
        if (upstream.status >= 200 && upstream.status < 300 && upstream.data && Array.isArray(upstream.data.data)) {
          modelNames = upstream.data.data
            .map(row => row && row.id)
            .filter(Boolean)
            .map(String);
        }
      }
      if (!modelNames.length) modelNames = [llamaModelName];
      sendJson(res, 200, { models: modelNames.map(name => ({ name })) });
    } catch (error) {
      log("llamacpp tags error", { error: error.message });
      sendJson(res, 502, { error: `llama.cpp non raggiungibile su :${llamaPort}` });
    }
    return;
  }

  if (req.method === "POST" && targetPath === "/api/generate") {
    try {
      const payload = await readJson(req);
      const model = String(payload.model || llamaModelName);
      const prompt = String(payload.prompt || "");
      const options = payload.options || {};

      const chatPayload = {
        model,
        stream: false,
        messages: [{ role: "user", content: prompt }]
      };

      // Ollama compatibility: when caller requests no-thinking, disable
      // reasoning in chat templates that support this flag (Qwen family).
      if (payload.think === false) {
        chatPayload.think = false;
        chatPayload.chat_template_kwargs = { enable_thinking: false };
      }

      const temperature = Number(options.temperature);
      if (Number.isFinite(temperature)) chatPayload.temperature = temperature;

      const numPredict = Number(options.num_predict);
      if (Number.isFinite(numPredict) && numPredict > 0) chatPayload.max_tokens = Math.floor(numPredict);

      if (payload.format === "json") {
        chatPayload.response_format = { type: "json_object" };
      }

      const upstream = await llamaRequest("POST", "/v1/chat/completions", chatPayload);
      if (upstream.status < 200 || upstream.status >= 300 || !upstream.data) {
        log("llamacpp generate upstream error", { status: upstream.status, raw: upstream.raw.slice(0, 500) });
        sendJson(res, 502, {
          error: `llama.cpp errore HTTP ${upstream.status}`,
          upstream: upstream.raw.slice(0, 800)
        });
        return;
      }

      const choice = Array.isArray(upstream.data.choices) ? upstream.data.choices[0] : null;
      const message = (choice && choice.message) ? choice.message : null;
      let text = String((message && message.content) || "");
      if (!text && message && (typeof message.reasoning_content === "string" || typeof message.reasoning === "string")) {
        const rawReasoning = typeof message.reasoning_content === "string" ? message.reasoning_content : message.reasoning;
        const trimmedReasoning = String(rawReasoning || "").trim();
        const lines = trimmedReasoning.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
        const candidate = lines.length ? lines[lines.length - 1] : trimmedReasoning;
        text = String(candidate || "").slice(0, 180);
      }
      const doneReason = (choice && choice.finish_reason) ? String(choice.finish_reason) : "stop";

      sendJson(res, 200, {
        model,
        created_at: new Date().toISOString(),
        response: text,
        done: true,
        done_reason: doneReason
      });
    } catch (error) {
      log("llamacpp generate error", { error: error.message });
      sendJson(res, 502, { error: error.message || "Errore llama.cpp" });
    }
    return;
  }

  sendJson(res, 404, { error: `Endpoint non supportato in modalita llama.cpp: ${targetPath}` });
}

function proxyUpstream(req, res, prefix, upstreamPort, label, upstreamHost = "127.0.0.1", upstreamBasePath = "") {
  const tailPath = req.url.replace(new RegExp("^" + prefix), "") || "/";
  const targetPath = joinPath(upstreamBasePath, tailPath);
  const started = Date.now();
  const chunks = [];

  const proxy = http.request({
    hostname: upstreamHost,
    port: upstreamPort,
    path: targetPath,
    method: req.method,
    headers: {
      "content-type": req.headers["content-type"] || "application/json"
    }
  }, upstream => {
    const responseChunks = [];
    res.writeHead(upstream.statusCode || 502, {
      "content-type": upstream.headers["content-type"] || "application/json",
      "access-control-allow-origin": "*"
    });
    upstream.on("data", chunk => {
      responseChunks.push(chunk);
      res.write(chunk);
    });
    upstream.on("end", () => {
      res.end();
      const responseBody = Buffer.concat(responseChunks).toString("utf8");
      log(label + " response", {
        method: req.method,
        path: targetPath,
        host: upstreamHost,
        port: upstreamPort,
        status: upstream.statusCode,
        ms: Date.now() - started,
        bytes: responseBody.length,
        preview: responseBody.slice(0, 500)
      });
    });
  });

  proxy.on("error", error => {
    log(label + " proxy error", { method: req.method, path: targetPath, host: upstreamHost, port: upstreamPort, error: error.message });
    send(res, 502, { "content-type": "application/json" }, JSON.stringify({ error: error.message }));
  });

  req.on("data", chunk => chunks.push(chunk));
  req.on("end", () => {
    const body = Buffer.concat(chunks);
    let preview = body.toString("utf8").slice(0, 700);
    try {
      const parsed = JSON.parse(body.toString("utf8") || "{}");
      if (parsed.prompt) parsed.prompt = parsed.prompt.slice(0, 500);
      if (parsed.image && typeof parsed.image === "string") parsed.image = parsed.image.slice(0, 64) + "...(" + parsed.image.length + " bytes)";
      if (Array.isArray(parsed.images)) parsed.images = parsed.images.map(s => typeof s === "string" ? (s.slice(0, 48) + "...(" + s.length + " b)") : s);
      preview = JSON.stringify(parsed);
    } catch (_) {}
    log(label + " request", { method: req.method, path: targetPath, host: upstreamHost, port: upstreamPort, bytes: body.length, preview });
    proxy.end(body);
  });
}

function handleLlmBackends(req, res) {
  const llamaModels = listLlamaModelsFromDisk();
  sendJson(res, 200, {
    ok: true,
    defaultBackend: llmBackend,
    backends: [
      { id: "heuristic", label: "Euristica locale (Zero LLM / Edge)" },
      { id: "ollama", label: "Ollama" },
      { id: "llamacpp", label: "llama.cpp" }
    ],
    llama: {
      home: llamaHome || null,
      loadedModel: llamaModelName || null,
      modelPath: llamaModelPath || null,
      models: llamaModels
    },
    ollama: {
      baseUrl: ollamaBaseUrlRaw,
      upstream: ollamaUpstream
    }
  });
}

function handleEngineProxy(req, res) {
  const match = req.url.match(/^\/llm\/(ollama|llamacpp|heuristic)(\/.*)?$/);
  if (!match) return false;
  const engine = match[1];
  const tail = match[2] || "/";
  if (engine === "heuristic") {
    if (tail === "/api/tags" || tail === "/v1/models") {
      sendJson(res, 200, { models: [{ name: "Regole euristiche (Zero LLM)", modified_at: new Date().toISOString() }] });
      return true;
    }
    if (tail === "/api/generate" || tail === "/api/chat" || tail === "/v1/chat/completions") {
      sendJson(res, 200, { response: "Euristica attiva", message: { content: "Euristica attiva" }, done: true });
      return true;
    }
    sendJson(res, 200, { ok: true, mode: "heuristic" });
    return true;
  }
  req.url = `/ollama${tail}`;
  if (engine === "llamacpp") {
    handleOllamaCompat(req, res);
  } else {
    proxyOllama(req, res);
  }
  return true;
}

const server = http.createServer((req, res) => {
  log("request", { method: req.method, url: req.url });
  if (req.method === "OPTIONS") {
    send(res, 204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET,POST,OPTIONS",
      "access-control-allow-headers": "content-type"
    }, "");
    return;
  }

  if (req.url.startsWith("/ollama/")) {
    if (llmBackend === "heuristic") {
      const tail = req.url.replace(/^\/ollama/, "") || "/";
      if (tail === "/api/tags" || tail === "/v1/models") {
        sendJson(res, 200, { models: [{ name: "Regole euristiche (Zero LLM)", modified_at: new Date().toISOString() }] });
        return;
      }
      sendJson(res, 200, { response: "Euristica attiva", message: { content: "Euristica attiva" }, done: true });
      return;
    }
    if (llmBackend === "llamacpp") {
      handleOllamaCompat(req, res);
    } else {
      proxyOllama(req, res);
    }
    return;
  }

  if (req.url === "/llm/backends" && req.method === "GET") {
    handleLlmBackends(req, res);
    return;
  }

  if (req.url === "/llm/llamacpp/unload" && req.method === "POST") {
    unloadLlamaProcess(res);
    return;
  }

  if (req.url.startsWith("/llm/")) {
    if (handleEngineProxy(req, res)) return;
    sendJson(res, 404, { error: "Engine non supportato" });
    return;
  }

  if (req.url === "/debug/log" && req.method === "POST") {
    const bodyChunks = [];
    req.on("data", c => bodyChunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(bodyChunks).toString("utf8");
      let parsed = null;
      try { parsed = JSON.parse(raw || "{}"); } catch (_) { parsed = { raw: raw.slice(0, 500) }; }
      const event = (parsed && parsed.event) ? String(parsed.event) : "client";
      log("client " + event, parsed);
      send(res, 204, { "access-control-allow-origin": "*" }, "");
    });
    return;
  }

  // Elenco dei cataloghi / database JSON presenti nella cartella music/
  if (req.url === "/api/music/catalogs") {
    const musicDir = path.join(root, "music");
    fs.readdir(musicDir, (err, files) => {
      if (err) {
        sendJson(res, 200, { catalogs: [] });
        return;
      }
      const list = files
        .filter(f => f.endsWith(".json"))
        .map(f => ({ name: f, path: `music/${f}` }));
      sendJson(res, 200, { catalogs: list });
    });
    return;
  }

  // Ricerca globale nel database SQLite dei 62.000+ brani HVSC
  if (req.url.startsWith("/api/music/search")) {
    const parsedUrl = new URL(req.url, `http://${req.headers.host}`);
    const q = (parsedUrl.searchParams.get("q") || "").trim();
    if (!q || q.length < 2) {
      sendJson(res, 200, { results: [] });
      return;
    }
    // Esecuzione query rapida via processo python integrato
    const dbPath = path.join(root, "music", "hvsc_catalog.sqlite");
    if (!fs.existsSync(dbPath)) {
      sendJson(res, 200, { results: [] });
      return;
    }
    const pyScript = `import sqlite3, json, sys
conn = sqlite3.connect(sys.argv[1])
cur = conn.cursor()
q = '%' + sys.argv[2] + '%'
cur.execute("SELECT id, title, author, released, path, url FROM tracks WHERE title LIKE ? OR author LIKE ? ORDER BY CASE WHEN title LIKE ? THEN 1 ELSE 2 END, title LIMIT 30", (q, q, sys.argv[2] + '%'))
rows = cur.fetchall()
res = [{"id": r[0], "title": r[1], "author": r[2], "released": r[3], "path": r[4], "url": r[5], "platform": "C64 (SID)", "license": "HVSC Archive"} for r in rows]
print(json.dumps(res))`;

    const py = childProcess.spawn("python", ["-c", pyScript, dbPath, q]);
    let out = "";
    py.stdout.on("data", d => out += d);
    py.on("close", code => {
      try {
        const results = JSON.parse(out || "[]");
        sendJson(res, 200, { results });
      } catch (_) {
        sendJson(res, 200, { results: [] });
      }
    });
    return;
  }

  // Proxy per file SID con cache locale (risolve problemi di CORS verso hvsc.c64.org)
  if (req.url.startsWith("/api/sid-proxy")) {
    const parsedUrl = new URL(req.url, `http://${req.headers.host}`);
    const sidPath = parsedUrl.searchParams.get("path");
    if (!sidPath) {
      sendJson(res, 400, { error: "Parametro path mancante" });
      return;
    }
    // Percorso cache locale su disco
    const cacheDir = path.join(root, "music", "in_game", "sid_cache");
    const safeFilename = sidPath.replace(/[^a-zA-Z0-9._-]/g, "_");
    const cachedFile = path.join(cacheDir, safeFilename);

    if (fs.existsSync(cachedFile)) {
      fs.readFile(cachedFile, (err, data) => {
        if (!err) {
          send(res, 200, {
            "content-type": "application/octet-stream",
            "access-control-allow-origin": "*",
            "x-sid-source": "local-cache"
          }, data);
          return;
        }
      });
      return;
    }

    // Se non in cache, fetch da HVSC e salvataggio
    const remoteUrl = sidPath.startsWith("http") ? sidPath : `https://www.hvsc.c64.org/C64Music/${sidPath.replace(/^\/+/, "")}`;
    const client = remoteUrl.startsWith("https") ? https : http;
    client.get(remoteUrl, { headers: { "user-agent": "NeonRift/1.0" } }, remoteRes => {
      if (remoteRes.statusCode !== 200) {
        sendJson(res, remoteRes.statusCode, { error: `Errore download SID da HVSC: HTTP ${remoteRes.statusCode}` });
        return;
      }
      const chunks = [];
      remoteRes.on("data", c => chunks.push(c));
      remoteRes.on("end", () => {
        const sidBuffer = Buffer.concat(chunks);
        try {
          if (!fs.existsSync(cacheDir)) fs.mkdirSync(cacheDir, { recursive: true });
          fs.writeFileSync(cachedFile, sidBuffer);
        } catch (_) {}
        send(res, 200, {
          "content-type": "application/octet-stream",
          "access-control-allow-origin": "*",
          "x-sid-source": "remote-fetched"
        }, sidBuffer);
      });
    }).on("error", err => {
      sendJson(res, 502, { error: `Impossibile raggiungere HVSC: ${err.message}` });
    });
    return;
  }

  if (req.url === "/debug/status") {
    send(res, 200, { "content-type": "application/json" }, JSON.stringify({
      ok: true,
      debug,
      startedAt,
      port,
      logFile,
      llmBackend,
      llamaPort,
      llamaModelName,
      ollamaBaseUrl: ollamaBaseUrlRaw,
      ollamaUpstream
    }, null, 2));
    return;
  }

  const requested = req.url === "/" ? "/codex_asteroids.html" : decodeURIComponent(req.url.split("?")[0]);
  const file = path.resolve(root, "." + requested);
  if (!file.startsWith(root)) {
    send(res, 403, { "content-type": "text/plain; charset=utf-8" }, "Forbidden");
    return;
  }

  fs.readFile(file, (error, data) => {
    if (error) {
      send(res, 404, { "content-type": "text/plain; charset=utf-8" }, "Not found");
      return;
    }
    send(res, 200, { "content-type": contentType(file) }, data);
  });
});

server.listen(port, () => {
  console.log(`Neon Rift: http://localhost:${port}`);
  if (debug) {
    log("debug enabled", { port, root, logFile });
  }
});
