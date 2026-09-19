/* Переводчик RU -> прусский: загрузка Pyodide + pymorphy3, обвязка UI.
   Вся переводческая логика находится в translate_core.py и не меняется
   для работы в браузере — только источники данных заменены на fetch(). */

const statusText = document.getElementById("status-text");
const loadingPanel = document.getElementById("loading");
const appPanel = document.getElementById("app");
const errorPanel = document.getElementById("error");
const errorText = document.getElementById("error-text");
const readyNote = document.getElementById("ready-note");
const translateBtn = document.getElementById("translate-btn");
const input = document.getElementById("input");
const resultWrap = document.getElementById("result-wrap");
const resultText = document.getElementById("result-text");
const breakdownBody = document.getElementById("breakdown-body");
const dirRuPrBtn = document.getElementById("dir-ru-pr");
const dirPrRuBtn = document.getElementById("dir-pr-ru");
const inputLabel = document.getElementById("input-label");
const resultLabel = document.getElementById("result-label");

let direction = "ru-pr"; // "ru-pr" | "pr-ru"

const DIRECTION_UI = {
  "ru-pr": {
    inputLabel: "Фраза по-русски",
    placeholder: "Например: Брат идёт в лес",
    resultLabel: "Прусский (черновой перевод)",
  },
  "pr-ru": {
    inputLabel: "Фраза по-прусски",
    placeholder: "Например: kaīls, brāti",
    resultLabel: "Русский (черновой перевод)",
  },
};

function setDirection(dir) {
  direction = dir;
  dirRuPrBtn.classList.toggle("active", dir === "ru-pr");
  dirPrRuBtn.classList.toggle("active", dir === "pr-ru");
  const ui = DIRECTION_UI[dir];
  inputLabel.textContent = ui.inputLabel;
  input.placeholder = ui.placeholder;
  resultLabel.textContent = ui.resultLabel;
  resultWrap.classList.add("hidden");
}

dirRuPrBtn.addEventListener("click", () => setDirection("ru-pr"));
dirPrRuBtn.addEventListener("click", () => setDirection("pr-ru"));

function setStatus(msg) {
  statusText.textContent = msg;
}

function showFatalError(msg) {
  loadingPanel.classList.add("hidden");
  errorPanel.classList.remove("hidden");
  errorText.textContent = msg;
}

async function boot() {
  try {
    setStatus("Загружаю Pyodide (python в браузере)…");
    const pyodide = await loadPyodide();

    setStatus("Устанавливаю pymorphy3 и русский словарь форм (~10 МБ, один раз)…");
    await pyodide.loadPackage("micropip");
    const micropip = pyodide.pyimport("micropip");
    await micropip.install(["pymorphy3", "pymorphy3-dicts-ru"]);

    setStatus("Загружаю данные словаря Prūsiska bilā…");
    const [dictText, overridesText, coreCode] = await Promise.all([
      fetch("data/dictionary.json").then(r => {
        if (!r.ok) throw new Error("dictionary.json: HTTP " + r.status);
        return r.text();
      }),
      fetch("data/overrides.json").then(r => {
        if (!r.ok) throw new Error("overrides.json: HTTP " + r.status);
        return r.text();
      }),
      fetch("translate_core.py").then(r => {
        if (!r.ok) throw new Error("translate_core.py: HTTP " + r.status);
        return r.text();
      }),
    ]);

    setStatus("Строю индексы (словоформы, предлоги, парадигмы)…");
    await pyodide.runPythonAsync(coreCode);
    const loadData = pyodide.globals.get("load_data");
    const stats = loadData(dictText, overridesText).toJs({ dict_converter: Object.fromEntries });

    readyNote.textContent = `Слов: ${stats.words} · с парадигмами: ${stats.overrides} · предлогов: ${stats.prepositions}`;

    const translateSentenceJson = pyodide.globals.get("translate_sentence_json");
    const translatePrSentenceJson = pyodide.globals.get("translate_pr_sentence_json");
    window.__translate = {
      "ru-pr": (text) => JSON.parse(translateSentenceJson(text)),
      "pr-ru": (text) => JSON.parse(translatePrSentenceJson(text)),
    };

    loadingPanel.classList.add("hidden");
    appPanel.classList.remove("hidden");
    input.focus();
  } catch (err) {
    console.error(err);
    showFatalError(
      "Не удалось загрузить переводчик: " + (err && err.message ? err.message : err) +
      ". Проверьте подключение к интернету (нужен доступ к cdn.jsdelivr.net и pypi.org для первой загрузки) и обновите страницу."
    );
  }
}

function renderResult(data) {
  resultText.textContent = data.translation || "—";
  breakdownBody.innerHTML = "";
  for (const w of data.words) {
    const tr = document.createElement("tr");
    const src = document.createElement("td");
    src.textContent = w.src;
    const out = document.createElement("td");
    out.textContent = w.out;
    const note = document.createElement("td");
    note.textContent = w.note || "";
    tr.append(src, out, note);
    breakdownBody.appendChild(tr);
  }
  resultWrap.classList.remove("hidden");
}

function doTranslate() {
  const text = input.value.trim();
  if (!text || !window.__translate) return;
  const data = window.__translate[direction](text);
  renderResult(data);
}

translateBtn.addEventListener("click", doTranslate);
input.addEventListener("keydown", (e) => {
  if ((e.key === "Enter") && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    doTranslate();
  }
});

boot();
