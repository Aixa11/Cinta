import express from "express";
import multer from "multer";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { randomUUID, randomBytes, scryptSync, timingSafeEqual } from "node:crypto";
import https from "node:https";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const jobsDir = path.join(root, "data", "jobs");
const dist = path.join(root, "dist");
const py = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const workerScript = path.join(root, "pipeline", "worker.py");
const languages = JSON.parse(fs.readFileSync(path.join(root, "pipeline", "languages.json"), "utf8"));
const langIds = new Set(languages.map((item) => item.id));

fs.mkdirSync(jobsDir, { recursive: true });

const PORT = Number(process.env.PORT || 8787);
const shelfFile = path.join(root, "data", "shelf.json");
const accountsFile = path.join(root, "data", "accounts.json");
const sessionsFile = path.join(root, "data", "sessions.json");
const googleFile = path.join(root, "data", "google.json");
const legacyFlag = path.join(root, "data", "legacy-claimed");
const SESSION_COOKIE = "cinta_session";
const SESSION_MS = 30 * 24 * 60 * 60 * 1000;
const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "1mb" }));

function emptyShelf() {
  return { profile: { name: "" }, groups: [], formations: [], notes: {}, groupsOf: {}, read: {} };
}

function shelfPath(userId) {
  if (!/^[0-9a-f-]{36}$/i.test(userId || "")) return null;
  return path.join(root, "data", "people", userId, "shelf.json");
}

function normalizeShelf(data) {
  const profileName = cleanName(data?.profile?.name || "");
  return {
    profile: { name: profileName },
    groups: Array.isArray(data?.groups) ? data.groups : [],
    formations: Array.isArray(data?.formations) ? data.formations : [],
    notes: data?.notes && typeof data.notes === "object" ? data.notes : {},
    groupsOf: data?.groupsOf && typeof data.groupsOf === "object" ? data.groupsOf : {},
    read: data?.read && typeof data.read === "object" ? data.read : {},
  };
}

function readShelf(userId) {
  const file = shelfPath(userId);
  if (!file) return emptyShelf();
  try {
    return normalizeShelf(JSON.parse(fs.readFileSync(file, "utf8")));
  } catch {
    return emptyShelf();
  }
}

function writeShelf(userId, shelf) {
  const file = shelfPath(userId);
  if (!file) return;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(shelf, null, 2));
  fs.renameSync(tmp, file);
}

function cleanName(value) {
  return String(value || "").replace(/\s+/g, " ").trim().slice(0, 80);
}

function forgetJob(id, userId) {
  const shelf = readShelf(userId);
  delete shelf.notes[id];
  delete shelf.groupsOf[id];
  delete shelf.read[id];
  for (const formation of shelf.formations) {
    formation.items = (formation.items || []).filter((item) => item !== id);
  }
  writeShelf(userId, shelf);
}

function readJsonFile(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJsonFile(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, file);
}

function readAccounts() {
  const data = readJsonFile(accountsFile, { users: [] });
  data.users = Array.isArray(data.users) ? data.users : [];
  return data;
}

function publicUser(user) {
  if (!user) return null;
  return { id: user.id, email: user.email, name: user.name, google: Boolean(user.googleSub) };
}

function hashPassword(password, salt = randomBytes(16).toString("hex")) {
  return { salt, hash: scryptSync(password, salt, 32).toString("hex") };
}

function samePassword(password, salt, hash) {
  const next = scryptSync(String(password), salt, 32);
  const prev = Buffer.from(hash, "hex");
  return next.length === prev.length && timingSafeEqual(next, prev);
}

function validEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function readSessions() {
  const data = readJsonFile(sessionsFile, {});
  const now = Date.now();
  let dirty = false;
  for (const [token, session] of Object.entries(data)) {
    if (!session || session.expires < now) {
      delete data[token];
      dirty = true;
    }
  }
  if (dirty) writeJsonFile(sessionsFile, data);
  return data;
}

function cookieValue(req, name) {
  const raw = String(req.headers.cookie || "");
  for (const part of raw.split(/;\s*/)) {
    const cut = part.indexOf("=");
    if (cut === -1) continue;
    if (part.slice(0, cut) === name) return decodeURIComponent(part.slice(cut + 1));
  }
  return "";
}

function userFromRequest(req) {
  const token = cookieValue(req, SESSION_COOKIE);
  if (!token) return null;
  const session = readSessions()[token];
  if (!session) return null;
  return readAccounts().users.find((user) => user.id === session.userId) || null;
}

function sendSession(res, userId) {
  const token = randomBytes(32).toString("hex");
  const sessions = readSessions();
  sessions[token] = { userId, expires: Date.now() + SESSION_MS };
  writeJsonFile(sessionsFile, sessions);
  res.setHeader("Set-Cookie", `${SESSION_COOKIE}=${token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${Math.floor(SESSION_MS / 1000)}`);
}

function clearSession(req, res) {
  const token = cookieValue(req, SESSION_COOKIE);
  if (token) {
    const sessions = readSessions();
    delete sessions[token];
    writeJsonFile(sessionsFile, sessions);
  }
  res.setHeader("Set-Cookie", `${SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0`);
}

function googleClientId() {
  if (process.env.CINTA_GOOGLE_CLIENT_ID) return String(process.env.CINTA_GOOGLE_CLIENT_ID).trim();
  const data = readJsonFile(googleFile, {});
  return String(data.clientId || "").trim();
}

function claimLegacy(userId) {
  if (fs.existsSync(legacyFlag)) return;
  for (const job of summaries()) {
    if (!job.ownerId) writeStatus(job.id, { ownerId: userId });
  }
  const dest = shelfPath(userId);
  if (dest && !fs.existsSync(dest) && fs.existsSync(shelfFile)) {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(shelfFile, dest);
  }
  const shelf = readShelf(userId);
  if (!shelf.profile.name) {
    const person = readAccounts().users.find((user) => user.id === userId);
    if (person?.name) {
      shelf.profile = { name: person.name };
      writeShelf(userId, shelf);
    }
  }
  fs.writeFileSync(legacyFlag, userId);
}

function createAccount({ email, name, password, googleSub }) {
  const accounts = readAccounts();
  const existing = accounts.users.find((user) => user.email === email);
  if (existing && !googleSub) {
    const error = new Error("Ese correo ya tiene una cuenta.");
    error.status = 409;
    throw error;
  }
  if (existing && googleSub) {
    existing.googleSub = googleSub;
    if (name && !existing.name) existing.name = name;
    writeJsonFile(accountsFile, accounts);
    return existing;
  }
  const user = {
    id: randomUUID(),
    email,
    name: cleanName(name) || email.split("@")[0],
    createdAt: new Date().toISOString(),
    googleSub: googleSub || "",
  };
  if (password) Object.assign(user, hashPassword(password));
  accounts.users.push(user);
  writeJsonFile(accountsFile, accounts);
  claimLegacy(user.id);
  if (!fs.existsSync(shelfPath(user.id))) {
    const shelf = emptyShelf();
    shelf.profile = { name: user.name };
    writeShelf(user.id, shelf);
  }
  return user;
}

function httpsGetJson(url) {
  return new Promise((resolve, reject) => {
    https.get(url, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        const body = Buffer.concat(chunks).toString("utf8");
        if (response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error("Google no aceptó esa sesión."));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    }).on("error", reject);
  });
}

function ownedStatus(req, id) {
  const status = readStatus(id);
  if (!status || !req.user || status.ownerId !== req.user.id) return null;
  return status;
}

let worker = null;
let workerState = { worker: "loading", message: "Arrancando el motor de traducción…" };
let queue = [];
let active = null;
let restarts = 0;

function jobDir(id) {
  if (!/^[0-9a-f-]{36}$/i.test(id || "")) return null;
  const dir = path.resolve(jobsDir, id);
  if (!dir.startsWith(path.resolve(jobsDir))) return null;
  return dir;
}

function readStatus(id) {
  const dir = jobDir(id);
  if (!dir) return null;
  const file = path.join(dir, "status.json");
  if (!fs.existsSync(file)) return null;
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function writeStatus(id, patch) {
  const dir = jobDir(id);
  const file = path.join(dir, "status.json");
  const current = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
  const next = { ...current, ...patch, updatedAt: new Date().toISOString() };
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(next, null, 2));
  fs.renameSync(tmp, file);
  return next;
}

function summaries(userId) {
  const jobs = [];
  for (const entry of fs.readdirSync(jobsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    try {
      const status = readStatus(entry.name);
      if (status && (!userId || status.ownerId === userId)) jobs.push(status);
    } catch {
      /* un trabajo a medias no bloquea la lista */
    }
  }
  jobs.sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")));
  return jobs;
}

function interruptStale() {
  for (const job of summaries()) {
    if (job.status === "processing" || job.status === "queued") {
      writeStatus(job.id, {
        status: "error",
        stage: "error",
        error: "El proceso se interrumpió. Vuelve a enviar el video.",
        message: "Interrumpido",
      });
    }
  }
}

function handleWorkerLine(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  if (msg.event === "health") {
    workerState = { worker: msg.worker, message: msg.message || "" };
    if (msg.worker === "ready") {
      restarts = 0;
      pump();
    }
    return;
  }
  if ((msg.event === "done" || msg.event === "error") && active === msg.id) {
    active = null;
    pump();
  }
}

function spawnWorker() {
  if (!fs.existsSync(py)) {
    workerState = {
      worker: "error",
      message: "Falta el entorno de Python del traductor.",
    };
    return;
  }
  workerState = { worker: "loading", message: "Arrancando el motor de traducción…" };
  worker = spawn(py, ["-u", workerScript], {
    cwd: root,
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
      PYTHONIOENCODING: "utf-8",
      PYTHONUTF8: "1",
      HF_HUB_DISABLE_SYMLINKS_WARNING: "1",
    },
    windowsHide: true,
  });
  let buffer = "";
  worker.stdout.on("data", (chunk) => {
    buffer += chunk.toString("utf8");
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (line) handleWorkerLine(line);
    }
  });
  worker.stderr.on("data", (chunk) => {
    const text = chunk.toString("utf8");
    process.stderr.write(text);
    if (active) {
      try {
        fs.appendFileSync(path.join(jobsDir, active, "pipeline.log"), text);
      } catch {
        /* el log es auxiliar */
      }
    }
  });
  worker.on("exit", () => {
    const failed = active;
    worker = null;
    active = null;
    if (failed) {
      const status = readStatus(failed);
      if (status && (status.status === "processing" || status.status === "queued")) {
        writeStatus(failed, {
          status: "error",
          stage: "error",
          error: "El motor de traducción se detuvo a mitad de camino. Intenta de nuevo.",
          message: "Error",
        });
      }
    }
    if (workerState.worker !== "ready" && restarts < 2) {
      restarts += 1;
      setTimeout(spawnWorker, 1200);
      return;
    }
    workerState = {
      worker: "error",
      message:
        workerState.worker === "error"
          ? workerState.message
          : "El motor se cerró. Cierra esta ventana y vuelve a abrir Cinta.",
    };
  });
}

function pump() {
  if (active || !worker || workerState.worker !== "ready") return;
  const next = queue.shift();
  if (!next) return;
  active = next;
  try {
    worker.stdin.write(`${JSON.stringify({ id: next })}\n`);
  } catch {
    active = null;
    writeStatus(next, {
      status: "error",
      stage: "error",
      error: "No pude enviar el video al motor de traducción.",
      message: "Error",
    });
  }
}

function enqueue(id) {
  queue.push(id);
  pump();
}

function safeUrl(value) {
  try {
    const url = new URL(String(value || "").trim());
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.toString();
  } catch {
    return null;
  }
}

const upload = multer({
  storage: multer.diskStorage({
    destination: (req, _file, cb) => cb(null, req.jobDir),
    filename: (_req, file, cb) => {
      const ext = path.extname(file.originalname || "").toLowerCase();
      const allowed = [
        ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mp3", ".wav", ".m4a", ".mpeg", ".mpg", ".ogg",
        ".pdf", ".docx", ".pptx", ".txt", ".md", ".html", ".htm", ".odt", ".rtf",
      ];
      cb(null, `upload${allowed.includes(ext) ? ext : ".bin"}`);
    },
  }),
  limits: { fileSize: 700 * 1024 * 1024 },
});

app.get("/api/health", (_req, res) => {
  res.set("Cache-Control", "no-store");
  res.json({ ok: workerState.worker === "ready", ...workerState });
});

app.get("/api/languages", (_req, res) => {
  res.json(languages);
});

app.get("/api/auth/config", (_req, res) => {
  res.set("Cache-Control", "no-store");
  res.json({
    googleClientId: googleClientId() || "",
    legacyPending: !fs.existsSync(legacyFlag),
  });
});

app.get("/api/auth/me", (req, res) => {
  res.set("Cache-Control", "no-store");
  res.json({ user: publicUser(userFromRequest(req)) });
});

app.post("/api/auth/register", (req, res) => {
  const email = String(req.body?.email || "").trim().toLowerCase();
  const name = cleanName(req.body?.name);
  const password = String(req.body?.password || "");
  if (!validEmail(email)) return res.status(400).json({ error: "Escribe un correo válido." });
  if (!name) return res.status(400).json({ error: "Escribe tu nombre." });
  if (password.length < 8) return res.status(400).json({ error: "La contraseña necesita al menos 8 caracteres." });
  try {
    const user = createAccount({ email, name, password });
    sendSession(res, user.id);
    res.status(201).json({ user: publicUser(user) });
  } catch (error) {
    res.status(error.status || 400).json({ error: error.message || "No pude crear la cuenta." });
  }
});

app.post("/api/auth/login", (req, res) => {
  const email = String(req.body?.email || "").trim().toLowerCase();
  const password = String(req.body?.password || "");
  const user = readAccounts().users.find((item) => item.email === email);
  if (!user?.hash || !user.salt || !samePassword(password, user.salt, user.hash)) {
    return res.status(401).json({ error: "Ese correo o la contraseña no coinciden." });
  }
  sendSession(res, user.id);
  res.json({ user: publicUser(user) });
});

app.post("/api/auth/logout", (req, res) => {
  clearSession(req, res);
  res.json({ ok: true });
});

app.post("/api/auth/google-client", (req, res) => {
  const clientId = String(req.body?.clientId || "").trim();
  if (!clientId.endsWith(".apps.googleusercontent.com")) {
    return res.status(400).json({ error: "Ese Client ID no parece de Google." });
  }
  writeJsonFile(googleFile, { clientId });
  res.json({ googleClientId: clientId });
});

app.post("/api/auth/google", async (req, res) => {
  const clientId = googleClientId();
  const credential = String(req.body?.credential || "");
  if (!clientId) return res.status(400).json({ error: "Google todavía no está activado en esta computadora." });
  if (!credential) return res.status(400).json({ error: "Google no envió la sesión." });
  try {
    const payload = await httpsGetJson(`https://oauth2.googleapis.com/tokeninfo?id_token=${encodeURIComponent(credential)}`);
    if (payload.aud !== clientId) return res.status(401).json({ error: "Esa sesión de Google no corresponde a esta plataforma." });
    if (String(payload.email_verified) !== "true") return res.status(401).json({ error: "Ese correo de Google no está verificado." });
    const email = String(payload.email || "").trim().toLowerCase();
    if (!validEmail(email)) return res.status(401).json({ error: "Google no entregó un correo." });
    const user = createAccount({ email, name: payload.name || email, googleSub: payload.sub });
    sendSession(res, user.id);
    res.json({ user: publicUser(user) });
  } catch {
    res.status(401).json({ error: "No pude confirmar la sesión de Google." });
  }
});

app.use("/api", (req, res, next) => {
  if (req.path === "/health" || req.path === "/languages" || req.path.startsWith("/auth")) return next();
  const user = userFromRequest(req);
  if (!user) return res.status(401).json({ error: "Entrá a tu cuenta para continuar." });
  req.user = user;
  next();
});

app.get("/api/shelf", (req, res) => {
  res.set("Cache-Control", "no-store");
  res.json(readShelf(req.user.id));
});

app.patch("/api/profile", (req, res) => {
  const name = cleanName(req.body?.name);
  if (!name) return res.status(400).json({ error: "Escribe cómo quieres ver tu portal." });
  const accounts = readAccounts();
  const person = accounts.users.find((user) => user.id === req.user.id);
  if (person) {
    person.name = name;
    writeJsonFile(accountsFile, accounts);
  }
  const shelf = readShelf(req.user.id);
  shelf.profile = { name };
  writeShelf(req.user.id, shelf);
  res.json({ shelf, user: publicUser(person) });
});

app.post("/api/jobs/:id/read", (req, res) => {
  if (!ownedStatus(req, req.params.id)) return res.status(404).json({ error: "No está esa traducción." });
  const shelf = readShelf(req.user.id);
  shelf.read[req.params.id] = new Date().toISOString();
  writeShelf(req.user.id, shelf);
  res.json(shelf);
});

app.post("/api/groups", (req, res) => {
  const name = cleanName(req.body?.name);
  if (!name) return res.status(400).json({ error: "Escribe un nombre para el grupo." });
  const shelf = readShelf(req.user.id);
  shelf.groups.push({ id: randomUUID(), name });
  writeShelf(req.user.id, shelf);
  res.status(201).json(shelf);
});

app.delete("/api/groups/:id", (req, res) => {
  const shelf = readShelf(req.user.id);
  shelf.groups = shelf.groups.filter((group) => group.id !== req.params.id);
  for (const [jobId, groupId] of Object.entries(shelf.groupsOf)) {
    if (groupId === req.params.id) delete shelf.groupsOf[jobId];
  }
  writeShelf(req.user.id, shelf);
  res.json(shelf);
});

app.post("/api/formations", (req, res) => {
  const name = cleanName(req.body?.name);
  if (!name) return res.status(400).json({ error: "Escribe el nombre de la formación." });
  const shelf = readShelf(req.user.id);
  shelf.formations.unshift({
    id: randomUUID(),
    name,
    createdAt: new Date().toISOString(),
    items: [],
  });
  writeShelf(req.user.id, shelf);
  res.status(201).json(shelf);
});

app.delete("/api/formations/:id", (req, res) => {
  const shelf = readShelf(req.user.id);
  shelf.formations = shelf.formations.filter((item) => item.id !== req.params.id);
  writeShelf(req.user.id, shelf);
  res.json(shelf);
});

app.post("/api/formations/:id/items", (req, res) => {
  const jobId = String(req.body?.jobId || "");
  if (!ownedStatus(req, jobId)) return res.status(400).json({ error: "Esa traducción no está." });
  const shelf = readShelf(req.user.id);
  const formation = shelf.formations.find((item) => item.id === req.params.id);
  if (!formation) return res.status(404).json({ error: "No está esa formación." });
  formation.items = formation.items || [];
  if (!formation.items.includes(jobId)) formation.items.push(jobId);
  writeShelf(req.user.id, shelf);
  res.json(shelf);
});

app.delete("/api/formations/:id/items/:jobId", (req, res) => {
  const shelf = readShelf(req.user.id);
  const formation = shelf.formations.find((item) => item.id === req.params.id);
  if (!formation) return res.status(404).json({ error: "No está esa formación." });
  formation.items = (formation.items || []).filter((item) => item !== req.params.jobId);
  writeShelf(req.user.id, shelf);
  res.json(shelf);
});

app.patch("/api/jobs/:id", (req, res) => {
  const status = ownedStatus(req, req.params.id);
  if (!status) return res.status(404).json({ error: "No está esa traducción." });
  const shelf = readShelf(req.user.id);
  if (typeof req.body?.note === "string") {
    const note = String(req.body.note).slice(0, 4000);
    if (note.trim()) shelf.notes[req.params.id] = note;
    else delete shelf.notes[req.params.id];
  }
  if ("groupId" in (req.body || {})) {
    const groupId = String(req.body.groupId || "");
    if (!groupId) delete shelf.groupsOf[req.params.id];
    else if (shelf.groups.some((group) => group.id === groupId)) shelf.groupsOf[req.params.id] = groupId;
  }
  writeShelf(req.user.id, shelf);
  if (typeof req.body?.title === "string") {
    const title = cleanName(req.body.title);
    if (title) writeStatus(req.params.id, { title });
  }
  res.json({ shelf: readShelf(req.user.id), job: readStatus(req.params.id) });
});

app.get("/api/jobs", (req, res) => {
  res.set("Cache-Control", "no-store");
  res.json(summaries(req.user.id));
});

app.get("/api/jobs/:id", (req, res) => {
  const status = ownedStatus(req, req.params.id);
  if (!status) return res.status(404).json({ error: "No está esa traducción." });
  const cuesFile = path.join(jobDir(req.params.id), "cues.json");
  try {
    status.cues = fs.existsSync(cuesFile) ? JSON.parse(fs.readFileSync(cuesFile, "utf8")) : [];
  } catch {
    status.cues = [];
  }
  res.set("Cache-Control", "no-store");
  res.json(status);
});

const mediaFiles = {
  source: ["input.mp4", "video/mp4"],
  subs: ["output-subs.mp4", "video/mp4"],
  dub: ["output-dub.mp4", "video/mp4"],
  vtt: ["subs.vtt", "text/vtt; charset=utf-8"],
  srt: ["subs.srt", "application/x-subrip; charset=utf-8"],
  text: ["translation.txt", "text/plain; charset=utf-8"],
  docx: ["translation.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
  pdf: ["translation.pdf", "application/pdf"],
  pptx: ["translation.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"],
  odt: ["translation.odt", "application/vnd.oasis.opendocument.text"],
  html: ["translation.html", "text/html; charset=utf-8"],
  rtf: ["translation.rtf", "application/rtf"],
  md: ["translation.md", "text/markdown; charset=utf-8"],
};

app.get("/api/jobs/:id/media/:kind", (req, res) => {
  const spec = mediaFiles[req.params.kind];
  const dir = jobDir(req.params.id);
  if (!spec || !dir || !ownedStatus(req, req.params.id)) return res.status(404).end();
  const file = path.join(dir, spec[0]);
  if (!fs.existsSync(file)) return res.status(404).end();
  res.sendFile(file, { headers: { "Content-Type": spec[1], "Cache-Control": "private, max-age=3600" } });
});

app.get("/api/jobs/:id/download/:kind", (req, res) => {
  const spec = mediaFiles[req.params.kind];
  const status = ownedStatus(req, req.params.id);
  const dir = jobDir(req.params.id);
  if (!spec || !status || !dir) return res.status(404).json({ error: "No está ese archivo." });
  const file = path.join(dir, spec[0]);
  if (!fs.existsSync(file)) return res.status(404).json({ error: "Todavía no está listo." });
  const base = String(status.title || "video")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80) || "video";
  const suffix = {
    subs: "subtitulos",
    dub: "voz",
    srt: "subtitulos",
    source: "original",
    vtt: "subtitulos",
    text: "traduccion",
    docx: "traduccion",
    pdf: "traduccion",
    pptx: "traduccion",
    odt: "traduccion",
    html: "traduccion",
    rtf: "traduccion",
    md: "traduccion",
  }[req.params.kind];
  const ext = path.extname(spec[0]);
  res.download(file, `${base}-${status.targetLang || "es"}-${suffix}${ext}`);
});

app.delete("/api/jobs/:id", (req, res) => {
  if (active === req.params.id || queue.includes(req.params.id)) {
    return res.status(409).json({ error: "Ese video todavía se está procesando." });
  }
  const dir = jobDir(req.params.id);
  if (!dir || !fs.existsSync(dir) || !ownedStatus(req, req.params.id)) return res.status(404).json({ error: "No está esa traducción." });
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    return res.status(409).json({ error: "No pude borrarlo. Cierra el video e inténtalo de nuevo." });
  }
  forgetJob(req.params.id, req.user.id);
  res.json({ ok: true });
});

app.post("/api/jobs", (req, res, next) => {
  const id = randomUUID();
  req.jobId = id;
  req.jobDir = path.join(jobsDir, id);
  fs.mkdirSync(req.jobDir, { recursive: true });
  next();
}, upload.single("file"), (req, res) => {
  const kind = req.body.kind === "document" ? "document" : "video";
  const targetLang = String(req.body.targetLang || "es");
  const sourceLang = String(req.body.sourceLang || "auto");
  const voice = req.body.voice === "m" ? "m" : "f";
  const variety = ["ar", "es", "mx"].includes(req.body.variety) ? req.body.variety : "ar";
  const wantSubs = kind === "video" && req.body.wantSubs !== "false";
  const wantDub = kind === "video" && req.body.wantDub !== "false";
  const bed = req.body.bed === "true";
  const formationId = cleanName(req.body.formationId) ? String(req.body.formationId) : "";
  const url = safeUrl(req.body.url);
  const file = req.file;
  const docExts = new Set([".pdf", ".docx", ".pptx", ".txt", ".md", ".html", ".htm", ".odt", ".rtf"]);
  const ext = file ? path.extname(file.originalname || file.filename || "").toLowerCase() : "";

  if (!langIds.has(targetLang) || (sourceLang !== "auto" && !langIds.has(sourceLang))) {
    fs.rmSync(req.jobDir, { recursive: true, force: true });
    return res.status(400).json({ error: "Ese idioma no está disponible." });
  }
  if (kind === "video" && !wantSubs && !wantDub) {
    fs.rmSync(req.jobDir, { recursive: true, force: true });
    return res.status(400).json({ error: "Elige subtítulos, voz, o los dos." });
  }
  if (kind === "document" && file && !docExts.has(ext)) {
    fs.rmSync(req.jobDir, { recursive: true, force: true });
    return res.status(400).json({ error: "Para un documento usa PDF, Word, texto, HTML o un enlace." });
  }
  if (kind === "video" && file && docExts.has(ext)) {
    fs.rmSync(req.jobDir, { recursive: true, force: true });
    return res.status(400).json({ error: "Ese archivo es un documento. Cámbialo a la sección Documentos." });
  }
  if (!file && !url) {
    fs.rmSync(req.jobDir, { recursive: true, force: true });
    return res.status(400).json({ error: kind === "document" ? "Sube un documento o pega un enlace." : "Sube un archivo o pega un enlace." });
  }
  if (req.body.url && !file && !url) {
    fs.rmSync(req.jobDir, { recursive: true, force: true });
    return res.status(400).json({ error: "Ese enlace no es válido." });
  }

  const title = file
    ? path.basename(file.originalname, path.extname(file.originalname))
    : (kind === "document" ? "Documento del enlace" : "Video del enlace");
  const createdAt = new Date().toISOString();
  const request = {
    kind,
    url: file ? null : url,
    targetLang,
    sourceLang,
    voice,
    variety,
    bed,
    wantSubs,
    wantDub,
    title,
  };
  fs.writeFileSync(path.join(req.jobDir, "request.json"), JSON.stringify(request, null, 2));
  const status = {
    id: req.jobId,
    kind,
    title,
    createdAt,
    status: "queued",
    stage: "acquire",
    progress: 0,
    message: workerState.worker === "ready" ? "En espera…" : "En espera de que el motor termine de arrancar…",
    error: null,
    warning: null,
    targetLang,
    sourceLang: null,
    requestedSource: sourceLang,
    voice,
    variety,
    bed,
    wantSubs,
    wantDub,
    duration: null,
    segments: 0,
    outputs: { source: false, vtt: false, srt: false, subs: false, dub: false },
    ownerId: req.user.id,
  };
  fs.writeFileSync(path.join(req.jobDir, "status.json"), JSON.stringify(status, null, 2));
  if (formationId) {
    const shelf = readShelf(req.user.id);
    const formation = shelf.formations.find((item) => item.id === formationId);
    if (formation) {
      formation.items = formation.items || [];
      formation.items.push(req.jobId);
      writeShelf(req.user.id, shelf);
    }
  }
  enqueue(req.jobId);
  res.status(201).json(status);
});

app.use((err, req, res, _next) => {
  if (req.jobDir) fs.rmSync(req.jobDir, { recursive: true, force: true });
  const message = err.code === "LIMIT_FILE_SIZE"
    ? "El archivo supera los 700 MB."
    : (err.message || "No pude recibir el video.");
  res.status(400).json({ error: message });
});

app.use(express.static(dist, { index: false, maxAge: "1h" }));
app.use((req, res) => {
  if (req.path.startsWith("/api")) return res.status(404).json({ error: "No encontrado." });
  const index = path.join(dist, "index.html");
  if (!fs.existsSync(index)) {
    return res
      .status(503)
      .type("html")
      .send("<!doctype html><meta charset=utf-8><title>Cinta</title><p>Falta construir la interfaz.</p>");
  }
  res.sendFile(index);
});

interruptStale();
spawnWorker();
app.listen(PORT, "127.0.0.1", () => {
  console.log(`Cinta lista en http://127.0.0.1:${PORT}`);
});
