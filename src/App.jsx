import { useEffect, useMemo, useRef, useState } from "react";

const EXTRA = {
  gl: "Gallego",
  eu: "Euskera",
  el: "Griego",
  ro: "Rumano",
  cs: "Checo",
  da: "Danés",
  fi: "Finés",
  id: "Indonesio",
  vi: "Vietnamita",
  th: "Tailandés",
  he: "Hebreo",
};

function clock(value) {
  const total = Math.max(0, Math.floor(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${minutes}:${ss}`;
}

function langName(code, languages) {
  if (!code) return "";
  return languages.find((item) => item.id === code)?.label || EXTRA[code] || code;
}

function statusLabel(job) {
  if (!job) return "";
  if (job.status === "done") return "Listo";
  if (job.status === "error") return "Error";
  if (job.status === "queued") return "En espera";
  return "Traduciendo";
}

function useTheme() {
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || "dark");
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    localStorage.setItem("cinta-theme", theme);
  }, [theme]);
  return [theme, setTheme];
}

async function readJson(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "No pude completar la solicitud.");
  return data;
}

export default function App() {
  const [languages, setLanguages] = useState([{ id: "es", label: "Español" }]);
  const [health, setHealth] = useState({ worker: "loading", message: "Conectando…" });
  const [jobs, setJobs] = useState([]);
  const [selected, setSelected] = useState(null);
  const [job, setJob] = useState(null);
  const [query, setQuery] = useState("");
  const [theme, setTheme] = useTheme();
  const [shelf, setShelf] = useState({ profile: { name: "" }, groups: [], formations: [], notes: {}, groupsOf: {}, read: {} });
  const [view, setView] = useState("home");
  const [pane, setPane] = useState("library");
  const [groupFilter, setGroupFilter] = useState("all");
  const [courseId, setCourseId] = useState("");
  const [profileDraft, setProfileDraft] = useState("");
  const [user, setUser] = useState(null);

  useEffect(() => {
    fetch("/api/languages").then((res) => res.json()).then(setLanguages).catch(() => {});
  }, []);

  useEffect(() => {
    let stop = false;
    async function tick() {
      try {
        const response = await fetch("/api/health");
        const data = await response.json();
        if (!stop) setHealth(data);
      } catch {
        if (!stop) setHealth({ worker: "error", message: "No encuentro el motor de traducción." });
      }
    }
    tick();
    const id = setInterval(tick, 4000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, []);

  async function refreshJobs(prefer) {
    const list = await readJson(await fetch("/api/jobs"));
    setJobs(list);
    if (prefer) setSelected(prefer);
    return list;
  }

  async function refreshShelf() {
    const data = await readJson(await fetch("/api/shelf"));
    setShelf(data);
    return data;
  }

  useEffect(() => {
    let stop = false;
    fetch("/api/auth/me")
      .then((response) => response.json())
      .then((data) => {
        if (!stop) setUser(data.user || null);
      })
      .catch(() => {});
    return () => {
      stop = true;
    };
  }, []);

  useEffect(() => {
    if (!user) {
      setJobs([]);
      setSelected(null);
      setJob(null);
      setShelf({ profile: { name: "" }, groups: [], formations: [], notes: {}, groupsOf: {}, read: {} });
      setProfileDraft("");
      return;
    }
    refreshJobs().catch(() => {});
    refreshShelf().then((data) => setProfileDraft(data?.profile?.name || user.name || "")).catch(() => {});
  }, [user?.id]);

  useEffect(() => {
    if (view !== "portal" || !selected) return undefined;
    let stop = false;
    fetch(`/api/jobs/${selected}/read`, { method: "POST" })
      .then((response) => readJson(response))
      .then((data) => {
        if (!stop) setShelf(data);
      })
      .catch(() => {});
    return () => {
      stop = true;
    };
  }, [view, selected]);

  useEffect(() => {
    if (!selected) {
      setJob(null);
      return undefined;
    }
    let stop = false;
    let timer;
    async function tick() {
      try {
        const data = await readJson(await fetch(`/api/jobs/${selected}`));
        if (stop) return;
        setJob(data);
        if (data.status === "done" || data.status === "error") {
          const list = await readJson(await fetch("/api/jobs"));
          if (!stop) setJobs(list);
          return;
        }
      } catch {
        return;
      }
      timer = setTimeout(tick, 1000);
    }
    tick();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [selected]);

  async function onCreated(id) {
    setView("portal");
    setSelected(id);
    setQuery("");
    await refreshJobs(id);
  }

  async function createGroup(name) {
    const data = await saveShelf("/api/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const groups = data.shelf?.groups || data.groups || [];
    return groups[groups.length - 1]?.id || "";
  }

  async function saveProfile() {
    const name = profileDraft.trim();
    if (!name) return;
    const data = await saveShelf("/api/profile", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    setProfileDraft(data.shelf?.profile?.name || data.profile?.name || name);
    if (data.user) setUser(data.user);
  }

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    setUser(null);
    setView("home");
  }

  async function onDelete(id) {
    await readJson(await fetch(`/api/jobs/${id}`, { method: "DELETE" }));
    const list = await refreshJobs();
    await refreshShelf().catch(() => {});
    if (selected === id) setSelected(list[0]?.id || null);
  }

  async function saveShelf(path, options) {
    const data = await readJson(await fetch(path, options));
    setShelf(data.shelf || data);
    if (data.job) setJob((current) => ({ ...(current || {}), ...data.job, cues: current?.cues || data.job.cues || [] }));
    return data;
  }

  const visible = jobs.filter((item) => {
    const text = `${item.title || ""} ${langName(item.targetLang, languages)}`.toLowerCase();
    if (!text.includes(query.trim().toLowerCase())) return false;
    const groupId = shelf.groupsOf?.[item.id] || "";
    if (groupFilter === "none") return !groupId;
    if (groupFilter !== "all") return groupId === groupFilter;
    return true;
  });
  const course = shelf.formations.find((item) => item.id === courseId) || shelf.formations[0] || null;
  const dot = health.worker === "ready" ? "dot" : health.worker === "error" ? "dot bad" : "dot wait";

  return (
    <div className="frame">
      <header className="top">
        <div className="brand">
          <div className="mark" aria-hidden="true"><i /></div>
          <div>
            <p className="word">Cinta</p>
            <p className="quiet">Traductor de video</p>
          </div>
        </div>
        <div className="top-tools">
          <div className="tabs" role="tablist" aria-label="Inicio o portal">
            <button type="button" role="tab" aria-selected={view === "home"} onClick={() => setView("home")}>Inicio</button>
            <button type="button" role="tab" aria-selected={view === "portal"} onClick={() => setView("portal")}>Mi portal</button>
          </div>
          {user ? <span className="who">{user.name}</span> : null}
          {user ? (
            <button type="button" className="texty" onClick={logout}>Salir</button>
          ) : (
            <button type="button" className="texty" onClick={() => setView("portal")}>Entrar</button>
          )}
          <div className="theme" role="group" aria-label="Apariencia">
            <button type="button" aria-pressed={theme === "light"} onClick={() => setTheme("light")}>Claro</button>
            <button type="button" aria-pressed={theme === "dark"} onClick={() => setTheme("dark")}>Oscuro</button>
          </div>
          <p className="ready"><i className={dot} /> {health.worker === "ready" ? "Motor listo" : health.message}</p>
        </div>
      </header>

      {health.worker !== "ready" && (
        <div className={health.worker === "error" ? "banner bad" : "banner"} role="status">{health.message}</div>
      )}

      {view === "home" ? (
        <div className="home">
          <p className="note">Este inicio es el mismo para todos. Para traducir, entrá a tu cuenta: cada persona ve solo sus videos, documentos y actividad.</p>
          <Composer languages={languages} health={health} formations={shelf.formations} signedIn={Boolean(user)} onNeedAccount={() => setView("portal")} onCreated={onCreated} />
        </div>
      ) : !user ? (
        <Login onDone={(next) => setUser(next)} />
      ) : (
      <div className="workspace">
        <aside className="library">
          <form className="field" onSubmit={(event) => { event.preventDefault(); saveProfile(); }}>
            Tu perfil
            <div className="mini">
              <input
                value={profileDraft}
                onChange={(event) => setProfileDraft(event.target.value)}
                placeholder="Tu nombre"
                aria-label="Nombre del perfil"
                maxLength={80}
              />
              <button type="submit">Guardar nombre</button>
            </div>
          </form>
          <p className="empty-copy">
            {shelf.profile?.name ? `Portal de ${shelf.profile.name}. ` : ""}
            Lo que lees y guardas queda en este perfil. No aparece en el inicio.
          </p>
          <div className="tabs" role="tablist" aria-label="Biblioteca o formación">
            <button type="button" role="tab" aria-selected={pane === "library"} onClick={() => setPane("library")}>Biblioteca</button>
            <button type="button" role="tab" aria-selected={pane === "course"} onClick={() => setPane("course")}>Tu formación</button>
          </div>
          {pane === "library" ? (
            <Library
              jobs={visible}
              total={jobs.length}
              selected={selected}
              languages={languages}
              query={query}
              setQuery={setQuery}
              groupFilter={groupFilter}
              setGroupFilter={setGroupFilter}
              groups={shelf.groups}
              groupsOf={shelf.groupsOf || {}}
              read={shelf.read || {}}
              onSelect={setSelected}
              onDelete={onDelete}
              onCreateGroup={createGroup}
              onDeleteGroup={async (id) => {
                await saveShelf(`/api/groups/${id}`, { method: "DELETE" });
                if (groupFilter === id) setGroupFilter("all");
              }}
            />
          ) : (
            <Formation
              jobs={jobs}
              formations={shelf.formations}
              course={course}
              languages={languages}
              onSelectCourse={setCourseId}
              onSelect={setSelected}
              onCreate={async (name) => {
                const data = await saveShelf("/api/formations", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ name }),
                });
                const created = data.formations?.[0];
                if (created) setCourseId(created.id);
              }}
              onDeleteCourse={async (id) => {
                await saveShelf(`/api/formations/${id}`, { method: "DELETE" });
                if (courseId === id) setCourseId("");
              }}
              onRemove={async (id, jobId) => {
                await saveShelf(`/api/formations/${id}/items/${jobId}`, { method: "DELETE" });
              }}
            />
          )}
        </aside>

        <main className="work">
          <Result
            job={job}
            languages={languages}
            shelf={shelf}
            onDelete={onDelete}
            onSave={async (patch) => {
              if (!job) return;
              await saveShelf(`/api/jobs/${job.id}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(patch),
              });
              if (patch.title) refreshJobs(job.id).catch(() => {});
            }}
            onCreateGroup={createGroup}
            onAddToCourse={async (id) => {
              if (!job) return;
              await saveShelf(`/api/formations/${id}/items`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ jobId: job.id }),
              });
            }}
          />
          <Composer languages={languages} health={health} formations={shelf.formations} signedIn onNeedAccount={() => {}} onCreated={onCreated} />
        </main>
      </div>
      )}
    </div>
  );
}

function Library({
  jobs, total, selected, languages, query, setQuery, groupFilter, setGroupFilter,
  groups, groupsOf = {}, read = {}, onSelect, onDelete, onCreateGroup, onDeleteGroup,
}) {
  const [name, setName] = useState("");
  return (
    <>
      <header>
        <span className="kicker">Biblioteca</span>
        <span className="quiet">{total}</span>
      </header>
      <input
        className="search"
        type="search"
        placeholder="Buscar"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        aria-label="Buscar en la biblioteca"
      />
      <label className="field">
        Grupo
        <select value={groupFilter} onChange={(event) => setGroupFilter(event.target.value)}>
          <option value="all">Todos</option>
          <option value="none">Sin grupo</option>
          {groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
        </select>
      </label>
      <form className="mini" onSubmit={(event) => {
        event.preventDefault();
        if (!name.trim()) return;
        onCreateGroup(name.trim());
        setName("");
      }}>
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Nuevo grupo" aria-label="Nombre del grupo" />
        <button type="submit">Crear</button>
      </form>
      {groupFilter !== "all" && groupFilter !== "none" && (
        <button type="button" className="link" onClick={() => onDeleteGroup(groupFilter)}>Borrar este grupo</button>
      )}
      <div className="library-list">
        {jobs.map((item) => (
          <div key={item.id} className={item.id === selected ? "jobline on" : "jobline"}>
            <button type="button" className="job" onClick={() => onSelect(item.id)}>
              <strong>{item.title || "Traducción"}</strong>
              <span>
                {item.kind === "document" ? "Documento" : "Video"} · {langName(item.targetLang, languages)} · <em>{statusLabel(item)}</em>
                {groups.find((group) => group.id === groupsOf[item.id]) ? ` · ${groups.find((group) => group.id === groupsOf[item.id]).name}` : ""}
                {read[item.id] ? " · Leído" : ""}
              </span>
            </button>
            <button type="button" className="job-x" aria-label={`Borrar ${item.title || "traducción"}`} onClick={() => onDelete(item.id)}>×</button>
          </div>
        ))}
        {total === 0 && <p className="empty-copy">Todavía no hay traducciones en tu portal. Las que hagas desde el inicio quedan acá.</p>}
        {total > 0 && jobs.length === 0 && <p className="empty-copy">Nada coincide con ese filtro.</p>}
      </div>
    </>
  );
}

function Formation({ jobs, formations, course, languages, onSelectCourse, onSelect, onCreate, onDeleteCourse, onRemove }) {
  const [name, setName] = useState("");
  const lessons = (course?.items || [])
    .map((id) => jobs.find((item) => item.id === id))
    .filter(Boolean);
  return (
    <>
      <header>
        <span className="kicker">Tu formación</span>
        <span className="quiet">{formations.length}</span>
      </header>
      <p className="empty-copy">Las clases que guardes acá siguen disponibles cuando cierres o recargues la plataforma.</p>
      <form className="mini" onSubmit={(event) => {
        event.preventDefault();
        if (!name.trim()) return;
        onCreate(name.trim());
        setName("");
      }}>
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Nueva formación" aria-label="Nombre de la formación" />
        <button type="submit">Crear</button>
      </form>
      <div className="library-list">
        {formations.map((item) => (
          <button key={item.id} type="button" className={course?.id === item.id ? "job on" : "job"} onClick={() => onSelectCourse(item.id)}>
            <strong>{item.name}</strong>
            <span>{(item.items?.length || 0) === 1 ? "1 clase" : `${item.items?.length || 0} clases`}</span>
          </button>
        ))}
        {formations.length === 0 && <p className="empty-copy">Crea una formación y suma las traducciones de cada clase.</p>}
      </div>
      {course && (
        <>
          <div className="fileline">
            <strong>{course.name}</strong>
            <button type="button" className="link" onClick={() => onDeleteCourse(course.id)}>Borrar formación</button>
          </div>
          <div className="library-list">
            {lessons.map((item) => (
              <div key={item.id} className="jobline">
                <button type="button" className="job" onClick={() => onSelect(item.id)}>
                  <strong>{item.title || "Clase"}</strong>
                  <span>{item.kind === "document" ? "Documento" : "Video"} · {langName(item.targetLang, languages)}</span>
                </button>
                <button type="button" className="job-x" aria-label="Quitar de la formación" onClick={() => onRemove(course.id, item.id)}>×</button>
              </div>
            ))}
            {lessons.length === 0 && <p className="empty-copy">Esta formación todavía no tiene clases.</p>}
          </div>
        </>
      )}
    </>
  );
}

function Login({ onDone }) {
  const [mode, setMode] = useState("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [googleId, setGoogleId] = useState("");
  const [legacyPending, setLegacyPending] = useState(false);
  const [clientDraft, setClientDraft] = useState("");
  const googleHost = useRef(null);

  useEffect(() => {
    fetch("/api/auth/config")
      .then((response) => response.json())
      .then((data) => {
        setGoogleId(data.googleClientId || "");
        setLegacyPending(Boolean(data.legacyPending));
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!googleId) return undefined;
    let script;
    window.cintaGoogle = async (response) => {
      try {
        const data = await readJson(await fetch("/api/auth/google", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ credential: response.credential }),
        }));
        onDone(data.user);
      } catch (err) {
        setError(err.message);
      }
    };
    script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.onload = () => {
      if (!window.google?.accounts?.id || !googleHost.current) return;
      window.google.accounts.id.initialize({
        client_id: googleId,
        callback: (response) => window.cintaGoogle(response),
      });
      googleHost.current.replaceChildren();
      window.google.accounts.id.renderButton(googleHost.current, {
        theme: "outline",
        size: "large",
        text: "continue_with",
        locale: "es",
        width: 320,
      });
    };
    document.body.appendChild(script);
    return () => {
      script.remove();
    };
  }, [googleId, onDone]);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const path = mode === "login" ? "/api/auth/login" : "/api/auth/register";
      const data = await readJson(await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, password }),
      }));
      onDone(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveGoogleClient(event) {
    event.preventDefault();
    setError("");
    try {
      const data = await readJson(await fetch("/api/auth/google-client", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clientId: clientDraft.trim() }),
      }));
      setGoogleId(data.googleClientId || "");
      setClientDraft("");
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div className="home">
      <form className="composer" onSubmit={submit}>
        <div className="intro">
          <div className="tabs" role="tablist" aria-label="Entrar o crear cuenta">
            <button type="button" role="tab" aria-selected={mode === "login"} onClick={() => { setMode("login"); setError(""); }}>Entrar</button>
            <button type="button" role="tab" aria-selected={mode === "create"} onClick={() => { setMode("create"); setError(""); }}>Crear cuenta</button>
          </div>
          <h2>{mode === "login" ? "Tu portal" : "Crear tu cuenta"}</h2>
          <p>Cada cuenta ve solo sus videos, documentos, grupos y notas. Otra persona que abra Cinta no ve lo tuyo.</p>
          {legacyPending && <p>Las traducciones que ya estaban en esta computadora pasan a la primera cuenta que se cree.</p>}
        </div>
        {mode === "create" && (
          <label className="field">
            Nombre
            <input value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" required />
          </label>
        )}
        <label className="field">
          Correo
          <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" required />
        </label>
        <label className="field">
          Contraseña
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={8} required />
        </label>
        <p className="hint" role="alert">{error}</p>
        <button className="primary" disabled={busy}>{busy ? "Esperá…" : mode === "login" ? "Entrar" : "Crear cuenta"}</button>
      </form>
      <div className="google-box">
        <p className="note">O entrá con Google.</p>
        {googleId ? <div ref={googleHost} /> : (
          <form className="composer" onSubmit={saveGoogleClient}>
            <label className="field">
              Client ID de Google
              <input value={clientDraft} onChange={(event) => setClientDraft(event.target.value)} placeholder="….apps.googleusercontent.com" aria-label="Client ID de Google" />
            </label>
            <button type="submit" className="texty">Activar Google</button>
          </form>
        )}
      </div>
    </div>
  );
}

function Composer({ languages, health, formations, signedIn, onNeedAccount, onCreated }) {
  const [product, setProduct] = useState("video");
  const [mode, setMode] = useState("file");
  const [file, setFile] = useState(null);
  const [url, setUrl] = useState("");
  const [targetLang, setTargetLang] = useState("es");
  const [sourceLang, setSourceLang] = useState("auto");
  const [variety, setVariety] = useState("ar");
  const [voice, setVoice] = useState("f");
  const [wantSubs, setWantSubs] = useState(true);
  const [wantDub, setWantDub] = useState(true);
  const [bed, setBed] = useState(false);
  const [formationId, setFormationId] = useState("");
  const [over, setOver] = useState(false);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const inputRef = useRef(null);

  const ordered = useMemo(() => {
    const spanish = languages.filter((item) => item.id === "es");
    const rest = languages.filter((item) => item.id !== "es");
    return [...spanish, ...rest];
  }, [languages]);

  function takeFile(next) {
    if (!next) return;
    setFile(next);
    setError("");
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (!signedIn) {
      onNeedAccount();
      return;
    }
    setError("");
    if (product === "video" && !wantSubs && !wantDub) {
      setError("Elige subtítulos, voz, o los dos.");
      return;
    }
    if (mode === "file" && !file) {
      setError(product === "document" ? "Elige un documento de tu equipo." : "Elige un video de tu equipo.");
      return;
    }
    if (mode === "link" && !url.trim()) {
      setError("Pega un enlace.");
      return;
    }
    const body = new FormData();
    body.append("kind", product);
    if (mode === "file") body.append("file", file);
    else body.append("url", url.trim());
    body.append("targetLang", targetLang);
    body.append("sourceLang", sourceLang);
    body.append("variety", variety);
    body.append("voice", voice);
    body.append("wantSubs", String(wantSubs));
    body.append("wantDub", String(wantDub));
    body.append("bed", String(bed));
    if (formationId) body.append("formationId", formationId);
    setSending(true);
    try {
      const created = await readJson(await fetch("/api/jobs", { method: "POST", body }));
      setFile(null);
      setUrl("");
      if (inputRef.current) inputRef.current.value = "";
      await onCreated(created.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setSending(false);
    }
  }

  return (
    <form className="composer" onSubmit={onSubmit}>
      <div className="intro">
        <div className="tabs" role="tablist" aria-label="Qué quieres traducir">
          <button type="button" role="tab" aria-selected={product === "video"} onClick={() => { setProduct("video"); setError(""); setFile(null); }}>Video</button>
          <button type="button" role="tab" aria-selected={product === "document"} onClick={() => { setProduct("document"); setError(""); setFile(null); }}>Documento</button>
        </div>
        <h2>{product === "document" ? "Documento completo" : "Nuevo video"}</h2>
        <p>
          {product === "document"
            ? "Un PDF, Word, PowerPoint o texto, o el enlace de una página. La traducción conserva el formato, las fuentes, los espacios y las imágenes."
            : "Archivo de tu equipo o un enlace. Puedes pedir subtítulos, voz, o las dos cosas."}
        </p>
      </div>
      <div className="tabs" role="tablist">
        <button type="button" role="tab" aria-selected={mode === "file"} onClick={() => { setMode("file"); setError(""); }}>Archivo</button>
        <button type="button" role="tab" aria-selected={mode === "link"} onClick={() => { setMode("link"); setError(""); }}>Enlace</button>
      </div>

      {mode === "file" ? (
        <>
          <label
            className={over ? "drop over" : "drop"}
            onDragOver={(event) => { event.preventDefault(); setOver(true); }}
            onDragLeave={() => setOver(false)}
            onDrop={(event) => {
              event.preventDefault();
              setOver(false);
              takeFile(event.dataTransfer.files?.[0]);
            }}
          >
            <strong>{file ? file.name : (product === "document" ? "Suelta un documento o elige uno" : "Suelta un video o elige uno")}</strong>
            <span>{product === "document" ? "pdf, docx, pptx, txt, md, html, odt o rtf" : "mp4, mov, mkv, webm, avi, mp3 o wav"}</span>
            <input
              key={product}
              ref={inputRef}
              type="file"
              accept={product === "document" ? ".pdf,.docx,.pptx,.txt,.md,.html,.htm,.odt,.rtf,application/pdf,text/plain" : "video/*,audio/*,.mkv,.mov,.mp4,.webm,.avi,.mp3,.wav,.m4a"}
              onChange={(event) => takeFile(event.target.files?.[0])}
            />
          </label>
          {file && (
            <div className="fileline">
              <span>{Math.max(1, Math.ceil(file.size / (1024 * 1024)))} MB</span>
              <button type="button" className="link" onClick={() => { setFile(null); if (inputRef.current) inputRef.current.value = ""; }}>Quitar</button>
            </div>
          )}
        </>
      ) : (
        <label className="field">
          Enlace
          <input
            type="url"
            placeholder={product === "document" ? "https://ejemplo.com/informe.pdf" : "https://www.youtube.com/watch?v=…"}
            value={url}
            onChange={(event) => setUrl(event.target.value)}
          />
        </label>
      )}

      <div className="row">
        <label className="field">
          Traducir al
          <select value={targetLang} onChange={(event) => setTargetLang(event.target.value)}>
            {ordered.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label className="field">
          Idioma original
          <select value={sourceLang} onChange={(event) => setSourceLang(event.target.value)}>
            <option value="auto">Detectar</option>
            {ordered.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
      </div>

      {targetLang === "es" && (
        <label className="field">
          Variedad del español
          <select value={variety} onChange={(event) => setVariety(event.target.value)}>
            <option value="ar">Rioplatense</option>
            <option value="es">España</option>
            <option value="mx">México</option>
          </select>
        </label>
      )}

      {product === "video" && <>
      <div className="field">
        Voz
        <div className="segment" role="group" aria-label="Voz">
          <button type="button" aria-pressed={voice === "f"} onClick={() => setVoice("f")}>Femenina</button>
          <button type="button" aria-pressed={voice === "m"} onClick={() => setVoice("m")}>Masculina</button>
        </div>
      </div>

      <div className="modes">
        <button type="button" className={wantSubs ? "mode on" : "mode"} aria-pressed={wantSubs} onClick={() => setWantSubs((value) => !value)}>
          <header>
            <h2>Subtítulos</h2>
            <i className="check" aria-hidden="true">{wantSubs ? "✓" : ""}</i>
          </header>
          <p>Texto sincronizado. En el reproductor puedes mostrarlos o esconderlos. La descarga los lleva incrustados.</p>
        </button>
        <button type="button" className={wantDub ? "mode on" : "mode"} aria-pressed={wantDub} onClick={() => setWantDub((value) => !value)}>
          <header>
            <h2>Voz traducida</h2>
            <i className="check" aria-hidden="true">{wantDub ? "✓" : ""}</i>
          </header>
          <p>Escuchas el video directo en el idioma elegido.</p>
        </button>
        {wantDub && (
          <label className="bed">
            <input type="checkbox" checked={bed} onChange={(event) => setBed(event.target.checked)} />
            Dejar el audio original muy bajo. Si se escucha sucio, déjalo apagado.
          </label>
        )}
      </div>
      </>}

      {formations.length > 0 && (
        <label className="field">
          Guardar en tu formación
          <select value={formationId} onChange={(event) => setFormationId(event.target.value)}>
            <option value="">No guardar en una formación</option>
            {formations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
      )}

      <p className="hint" role="alert">{error}</p>
      <button className="primary" disabled={sending || (signedIn && health.worker === "error")}>
        {sending ? "Enviando…" : !signedIn ? "Entrá para traducir" : product === "document" ? "Traducir documento" : "Traducir video"}
      </button>
      <p className="fine">
        {product === "document"
          ? "Hasta 40 MB o 1,2 millones de caracteres. El archivo se lee en esta computadora y el texto sale a internet para traducirse."
          : "Hasta 45 minutos y 700 MB. El video se procesa en esta computadora. La traducción del texto y las voces salen por internet."}
      </p>
    </form>
  );
}

function FileCard({ job, shelf, onSave, onAddToCourse, onCreateGroup }) {
  const [title, setTitle] = useState(job.title || "");
  const [note, setNote] = useState(shelf.notes?.[job.id] || "");
  const [groupId, setGroupId] = useState(shelf.groupsOf?.[job.id] || "");
  const [freshGroup, setFreshGroup] = useState("");
  const [course, setCourse] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState("");
  const [cardError, setCardError] = useState("");

  useEffect(() => {
    setTitle(job.title || "");
    setNote(shelf.notes?.[job.id] || "");
    setGroupId(shelf.groupsOf?.[job.id] || "");
    setFreshGroup("");
    setSaved("");
    setCardError("");
  }, [job.id]);

  async function saveCard() {
    const fresh = freshGroup.trim();
    if (!groupId && !fresh) return;
    setSaving(true);
    setSaved("");
    setCardError("");
    try {
      let id = groupId;
      let groupName = shelf.groups.find((group) => group.id === groupId)?.name || "";
      if (fresh) {
        id = await onCreateGroup(fresh);
        groupName = fresh;
        if (id) setGroupId(id);
        setFreshGroup("");
      }
      if (!id) return;
      const nextTitle = title.trim();
      await onSave({
        note,
        groupId: id,
        ...(nextTitle ? { title: nextTitle } : {}),
      });
      setSaved(groupName ? `Guardado en ${groupName}.` : "Guardado en tu portal.");
    } catch (err) {
      setCardError(err.message || "No pude guardarlo.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="ficha">
      <label className="field">
        Nombre
        <input value={title} onChange={(event) => { setTitle(event.target.value); setSaved(""); }} />
      </label>
      <label className="field">
        Grupo
        <select value={groupId} onChange={(event) => { setGroupId(event.target.value); setSaved(""); }}>
          <option value="">Elegir grupo</option>
          {shelf.groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
        </select>
      </label>
      <label className="field">
        Grupo nuevo
        <input
          value={freshGroup}
          onChange={(event) => { setFreshGroup(event.target.value); setSaved(""); }}
          placeholder="Si todavía no existe, escribe el nombre"
          aria-label="Nombre del grupo nuevo"
        />
      </label>
      <div className="field">
        Tu formación
        <div className="mini">
          <select value={course} onChange={(event) => setCourse(event.target.value)} aria-label="Formación">
            <option value="">Elegir formación</option>
            {shelf.formations.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
          <button type="button" disabled={!course} onClick={() => onAddToCourse(course)}>Agregar</button>
        </div>
      </div>
      <label className="field">
        Anotación
        <textarea
          rows={3}
          value={note}
          onChange={(event) => { setNote(event.target.value); setSaved(""); }}
          placeholder="Queda en tu portal, dentro del grupo."
        />
      </label>
      <div className="save-line">
        <button type="button" className="solid" disabled={saving || (!groupId && !freshGroup.trim())} onClick={saveCard}>
          {saving ? "Guardando…" : "Guardar"}
        </button>
        {saved && <span className="note">{saved} Solo se ve en tu portal.</span>}
        {cardError && <span className="error">{cardError}</span>}
      </div>
    </div>
  );
}

function DocumentView({ job, languages, shelf, onDelete, onSave, onAddToCourse, onCreateGroup }) {
  const previewRef = useRef(null);
  const [text, setText] = useState("");
  const [copied, setCopied] = useState(false);
  const target = langName(job.targetLang, languages);
  const detected = langName(job.sourceLang, languages);
  const fileKind = ["pdf", "docx", "pptx", "odt", "html", "rtf", "md"].find((kind) => job.outputs?.[kind]);
  const stamp = job.updatedAt || job.id;

  useEffect(() => {
    if (!job.outputs?.text) {
      setText("");
      return undefined;
    }
    let stop = false;
    fetch(`/api/jobs/${job.id}/media/text`)
      .then((response) => response.text())
      .then((value) => {
        if (!stop) setText(value);
      })
      .catch(() => {
        if (!stop) setText("");
      });
    return () => {
      stop = true;
    };
  }, [job.id, job.outputs?.text, stamp]);

  useEffect(() => {
    const host = previewRef.current;
    if (!host || !job.outputs?.docx) return undefined;
    let stop = false;
    host.replaceChildren();
    (async () => {
      try {
        const { renderAsync } = await import("docx-preview");
        const response = await fetch(`/api/jobs/${job.id}/media/docx?v=${encodeURIComponent(stamp)}`);
        if (!response.ok) return;
        const blob = await response.blob();
        if (stop) return;
        host.replaceChildren();
        await renderAsync(blob, host, null, {
          className: "cinta-docx",
          inWrapper: true,
          ignoreWidth: false,
          ignoreHeight: false,
        });
      } catch {
        if (!stop) host.replaceChildren();
      }
    })();
    return () => {
      stop = true;
    };
  }, [job.id, job.outputs?.docx, stamp]);

  async function copyText() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <section className="stage" aria-live="polite">
      <div className="viewbar">
        <h1>{job.title || "Documento"}</h1>
      </div>
      <div className="metrics">
        <div className="metric"><span>Tipo</span><strong>Documento</strong></div>
        <div className="metric"><span>Original</span><strong>{detected || "Detectado al traducir"}</strong></div>
        <div className="metric"><span>Destino</span><strong>{target || "—"}</strong></div>
        <div className="metric"><span>Extensión</span><strong>{job.chars ? `${job.chars.toLocaleString("es-AR")} caracteres` : "—"}</strong></div>
      </div>
      {job.status !== "done" && job.status !== "error" && (
        <div className="progress-block">
          <div className="progress-line">
            <span>{job.message}</span>
            <span>{Math.round(job.progress || 0)}%</span>
          </div>
          <progress value={job.progress || 0} max="100" />
        </div>
      )}
      {job.status === "done" && <p className="note" style={{ marginTop: 12 }}>Mismo documento, traducido. Se conservan el formato, las fuentes, los espacios y las imágenes.</p>}
      {job.error && <p className="error" style={{ marginTop: 12 }}>{job.error}</p>}
      {job.warning && <p className="note warn" style={{ marginTop: 12 }}>{job.warning}</p>}
      <FileCard job={job} shelf={shelf} onSave={onSave} onAddToCourse={onAddToCourse} onCreateGroup={onCreateGroup} />
      <div className="actions">
        {job.outputs?.text && <button type="button" className="texty" onClick={copyText}>{copied ? "Copiado" : "Copiar"}</button>}
        {fileKind && <a className="solid" href={`/api/jobs/${job.id}/download/${fileKind}`}>Descargar el documento</a>}
        {job.outputs?.text && <a href={`/api/jobs/${job.id}/download/text`}>Descargar texto</a>}
        <button type="button" className="ghost danger" onClick={() => onDelete(job.id)}>Quitar</button>
      </div>
      {job.outputs?.pdf && (
        <iframe className="docframe" title="Documento traducido" src={`/api/jobs/${job.id}/media/pdf?v=${encodeURIComponent(stamp)}`} />
      )}
      {job.outputs?.html && (
        <iframe className="docframe" title="Página traducida" sandbox="" src={`/api/jobs/${job.id}/media/html?v=${encodeURIComponent(stamp)}`} />
      )}
      {job.outputs?.docx && <div className="docx-host" ref={previewRef} />}
      {text && (
        <details className="plain">
          <summary>Texto para copiar</summary>
          <article className="article">{text}</article>
        </details>
      )}
    </section>
  );
}

function Result({ job, languages, shelf, onDelete, onSave, onAddToCourse, onCreateGroup }) {
  const videoRef = useRef(null);
  const linesRef = useRef(null);
  const lineRefs = useRef([]);
  const pending = useRef(0);
  const [tab, setTab] = useState("subs");
  const [time, setTime] = useState(0);
  const [follow, setFollow] = useState(false);
  const [filter, setFilter] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!job) return;
    setFilter("");
    setTime(0);
    if (job.wantDub && job.outputs?.dub) setTab("dub");
    else if (job.wantSubs) setTab("subs");
    else if (job.wantDub) setTab("dub");
  }, [job?.id]);

  const cues = Array.isArray(job?.cues) ? job.cues : [];
  const active = cues.findIndex((cue, index) => {
    const end = cue.end ?? cues[index + 1]?.start ?? Number.POSITIVE_INFINITY;
    return time >= cue.start && time < end;
  });

  useEffect(() => {
    if (!follow || active < 0) return;
    const node = lineRefs.current[active];
    const box = linesRef.current;
    if (!node || !box) return;
    const boxRect = box.getBoundingClientRect();
    const nodeRect = node.getBoundingClientRect();
    if (nodeRect.top < boxRect.top) box.scrollTop -= boxRect.top - nodeRect.top;
    else if (nodeRect.bottom > boxRect.bottom) box.scrollTop += nodeRect.bottom - boxRect.bottom;
  }, [active, follow]);

  if (!job) {
    return (
      <section className="stage">
        <div className="intro">
          <h2>Tu portal</h2>
          <p>Elegí una traducción para verla, anotarla y guardarla en un grupo. Eso queda en tu perfil, no en el inicio.</p>
        </div>
      </section>
    );
  }

  if (job.kind === "document") {
    return <DocumentView job={job} languages={languages} shelf={shelf} onDelete={onDelete} onSave={onSave} onAddToCourse={onAddToCourse} onCreateGroup={onCreateGroup} />;
  }

  const subsReady = Boolean(job.outputs?.vtt && job.outputs?.source);
  const burnReady = Boolean(job.outputs?.subs);
  const dubReady = Boolean(job.outputs?.dub);
  const showDub = tab === "dub" && dubReady;
  const src = showDub ? `/api/jobs/${job.id}/media/dub` : subsReady ? `/api/jobs/${job.id}/media/source` : "";
  const detected = langName(job.sourceLang, languages);
  const target = langName(job.targetLang, languages);
  const needle = filter.trim().toLowerCase();
  const shown = cues
    .map((cue, index) => ({ cue, index }))
    .filter(({ cue }) => !needle || `${cue.text} ${cue.original || ""}`.toLowerCase().includes(needle));

  function switchTab(next) {
    pending.current = videoRef.current?.currentTime || 0;
    setTab(next);
  }

  async function copyText() {
    const text = cues.map((cue) => cue.text).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  const steps = [
    { label: "Video", done: job.outputs?.source, now: job.stage === "acquire" },
    { label: "Texto", done: job.outputs?.vtt, now: job.stage === "transcribe" || job.stage === "translate" },
    job.wantDub ? { label: "Voz", done: dubReady, now: job.stage === "voice" } : null,
    job.wantSubs ? { label: "Subtítulos", done: burnReady, now: job.stage === "subtitles" } : null,
  ].filter(Boolean);

  return (
    <section className="stage" aria-live="polite">
      <div className="viewbar">
        <h1>{job.title || "Video"}</h1>
      </div>
      {(job.wantSubs || job.wantDub) && (
        <div className="versions" role="group" aria-label="Forma de ver el video">
          {job.wantSubs && (
            <button type="button" className={tab === "subs" ? "version on" : "version"} aria-pressed={tab === "subs"} disabled={!subsReady} onClick={() => switchTab("subs")}>
              <strong>Subtítulos</strong>
              <span>{burnReady ? "Listos para ver y descargar" : subsReady ? "Listos para leer" : "Preparando"}</span>
            </button>
          )}
          {job.wantDub && (
            <button type="button" className={tab === "dub" ? "version on" : "version"} aria-pressed={tab === "dub"} disabled={!dubReady} onClick={() => switchTab("dub")}>
              <strong>Voz traducida</strong>
              <span>{dubReady ? "Lista para escuchar" : "Preparando la voz"}</span>
            </button>
          )}
        </div>
      )}

      {src ? (
        <div className="screen">
          <video
            key={`${tab}-${src}`}
            ref={videoRef}
            src={src}
            controls
            playsInline
            onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
            onLoadedMetadata={(event) => {
              if (pending.current) event.currentTarget.currentTime = pending.current;
            }}
          >
            {tab === "subs" && job.outputs?.vtt && (
              <track
                key={job.id}
                default={true}
                kind="subtitles"
                srcLang={job.targetLang || "es"}
                label={target}
                src={`/api/jobs/${job.id}/media/vtt`}
              />
            )}
          </video>
        </div>
      ) : (
        <p className="note">El reproductor aparece en cuanto el video está listo para verse.</p>
      )}

      <div className="metrics">
        <div className="metric"><span>Original</span><strong>{detected || "Detectando"}</strong></div>
        <div className="metric"><span>Destino</span><strong>{target || "—"}</strong></div>
        <div className="metric"><span>Duración</span><strong>{job.duration ? clock(job.duration) : "—"}</strong></div>
        <div className="metric"><span>Frases</span><strong>{job.segments || cues.length || "—"}</strong></div>
      </div>

      {job.status !== "done" && job.status !== "error" && (
        <div className="progress-block">
          <div className="progress-line">
            <span>{job.message}</span>
            <span>{Math.round(job.progress || 0)}%</span>
          </div>
          <progress value={job.progress || 0} max="100" />
        </div>
      )}
      {job.status === "done" && <p className="note" style={{ marginTop: 12 }}>Listo para ver y descargar.</p>}
      {job.error && <p className="error" style={{ marginTop: 12 }}>{job.error}</p>}
      {job.warning && <p className="note warn" style={{ marginTop: 12 }}>{job.warning}</p>}
      <FileCard job={job} shelf={shelf} onSave={onSave} onAddToCourse={onAddToCourse} onCreateGroup={onCreateGroup} />

      <div className="steps" style={{ marginTop: 12 }}>
        {steps.map((step) => (
          <span key={step.label} className={step.done ? "step done" : step.now ? "step now" : "step"}>
            {step.label} · {step.done ? "listo" : step.now ? "ahora" : "pendiente"}
          </span>
        ))}
      </div>

      <div className="actions">
        {burnReady && <a className="solid" href={`/api/jobs/${job.id}/download/subs`}>Descargar con subtítulos</a>}
        {dubReady && <a className="solid" href={`/api/jobs/${job.id}/download/dub`}>Descargar con voz</a>}
        {job.outputs?.srt && <a href={`/api/jobs/${job.id}/download/srt`}>Subtítulos .srt</a>}
        <button type="button" className="ghost danger" onClick={() => onDelete(job.id)}>Quitar</button>
      </div>

      {job.outputs?.vtt && tab === "subs" && !burnReady && job.status !== "error" && (
        <p className="fine">Ya puedes leer los subtítulos. El archivo con el texto incrustado sigue preparándose.</p>
      )}

      {cues.length > 0 && (
        <div className="transcript">
          <div className="transcript-head">
            <span className="kicker">Texto</span>
            <div className="transcript-tools">
              <input
                className="search"
                type="search"
                placeholder="Buscar una frase"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                aria-label="Buscar en el texto"
              />
              <button type="button" className="texty" aria-pressed={follow} onClick={() => setFollow((value) => !value)}>
                {follow ? "Siguiendo" : "Seguir"}
              </button>
              <button type="button" className="texty" onClick={copyText}>{copied ? "Copiado" : "Copiar"}</button>
            </div>
          </div>
          <div className="lines" ref={linesRef}>
            {shown.map(({ cue, index }) => (
              <button
                key={`${cue.start}-${index}`}
                ref={(node) => { lineRefs.current[index] = node; }}
                className={index === active ? "line active" : "line"}
                type="button"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  if (!videoRef.current) return;
                  videoRef.current.currentTime = cue.start;
                  videoRef.current.play().catch(() => {});
                }}
              >
                <time>{clock(cue.start)}</time>
                <span>
                  {cue.text}
                  {cue.original && cue.original !== cue.text ? <small>{cue.original}</small> : null}
                </span>
              </button>
            ))}
            {shown.length === 0 && <p className="empty-copy">Ninguna frase coincide.</p>}
          </div>
        </div>
      )}
    </section>
  );
}
