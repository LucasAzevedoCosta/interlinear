/* Interlinear — lógica da interface.

   JavaScript puro, sem build. Um app local que o usuário vai querer modificar
   não deveria exigir instalar um empacotador antes de trocar uma cor.

   Três regras que guiam o código daqui:
     - o que o usuário digita nunca é perdido: salvamento com atraso curto e
       também ao sair do campo;
     - o preview usa o gerador real do EPUB, nunca uma imitação em HTML;
     - toda falha de rede vira uma mensagem legível, não um erro silencioso.
*/

'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  projects: [],
  project: null,
  doc: null,
  chapterId: null,
  settings: null,
  catalog: [],
  editingProviderId: null,
  providerHasKey: false,
  // Modelos do serviço escolhido no formulário de provedor.
  models: [],
  modelsLoading: false,
  modelsError: '',
  // A rota de configurações foi aberta por nós (dá para voltar com history).
  pushedSettings: false,
  pickerIndex: -1,
  claudeTimer: null,
  // Último `claude auth status` lido, com a hora ({at, value}).
  claude: null,
  jobId: null,
  jobTimer: null,
  // "convert" ou "translate": a etapa aberta (ver setStage).
  stage: 'convert',
  previewTranslated: true,
  dirty: new Map(),
  saveTimer: null,
  // Trechos que a conversão provavelmente errou (ver loadSuspects).
  suspects: [],
  suspectSeq: 0,
  lastSuspect: null,
  // Revisão por IA (ver loadReview): sugestões do review.json e a tarefa.
  review: { suggestions: [] },
  reviewSeq: 0,
  reviewTimer: null,
  // Limite do "aceitar em grupo"; null até o usuário escolher (padrão 90%).
  reviewThreshold: null,
  // Importações em andamento, pelo id da tarefa (ou um id local enquanto o
  // arquivo ainda sobe).
  imports: new Map(),
  importTimer: null,
  importOptions: null,
  pendingFile: null,
  pendingPurpose: 'convert',
};

/* ------------------------------------------------------------------ rede */

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const isJson = (response.headers.get('content-type') || '').includes('json');
  const payload = isJson ? await response.json() : null;

  if (!response.ok) {
    const detail = payload?.detail || `Erro ${response.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return payload;
}

// `action` ({label, onClick}) põe um botão no aviso, como "Desfazer". O aviso
// fica mais tempo na tela para dar chance de clicar.
function toast(message, tone = 'info', action = null) {
  const element = $('toast');
  element.textContent = message;
  if (action) {
    const button = document.createElement('button');
    button.className = 'toast__action';
    button.textContent = action.label;
    button.onclick = () => {
      element.hidden = true;
      action.onClick();
    };
    element.append(' ', button);
  }
  element.dataset.tone = tone;
  element.hidden = false;
  clearTimeout(toast._timer);
  const delay = tone === 'error' ? 7000 : action ? 8000 : 3200;
  toast._timer = setTimeout(() => { element.hidden = true; }, delay);
}

/* ----------------------------------------------------------------- rotas */

/* As configurações são uma página, não uma gaveta: têm endereço próprio, o
   botão Voltar do navegador funciona, e dá para deixar aberta numa aba. */

const SETTINGS_ROUTE = '#/configuracoes';

function openSettings() {
  if (location.hash === SETTINGS_ROUTE) return applyRoute();
  state.pushedSettings = true;
  location.hash = SETTINGS_ROUTE;
}

function closeSettings() {
  if (state.pushedSettings) return history.back();
  history.replaceState(null, '', location.pathname + location.search);
  applyRoute();
}

function applyRoute() {
  const open = location.hash === SETTINGS_ROUTE;
  $('settingsPage').hidden = !open;
  if (open) {
    $('settingsPage').scrollTop = 0;
    loadSettings();
    checkClaude();
  } else {
    state.pushedSettings = false;
    stopClaudePolling();
  }
}

/* --------------------------------------------------------------- projetos */

async function loadProjects() {
  try {
    const data = await api('/api/projects');
    state.projects = data.projects;
    renderProjects();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function renderProjects() {
  const container = $('projectList');
  container.innerHTML = '';

  if (!state.projects.length) {
    container.innerHTML =
      '<p class="empty">Nenhum livro ainda. Importe um PDF ou EPUB para começar.</p>';
    return;
  }

  for (const project of state.projects) {
    const row = document.createElement('div');
    row.className = 'project';

    const purpose = projectPurpose(project);
    const name = document.createElement('button');
    name.className = 'project__name';
    name.textContent = project.title;
    name.onclick = () => openProject(project.id);

    const meta = document.createElement('span');
    meta.className = 'project__meta';
    const words = `${project.word_count.toLocaleString('pt-BR')} palavras`;
    if (purpose === 'translate') {
      const pct = project.total_translatable
        ? Math.round((project.translated_count / project.total_translatable) * 100)
        : 0;
      meta.textContent = `${words} · ${pct}% traduzido`;
    } else {
      const chapters = project.import_summary?.chapters;
      const pages = project.import_summary?.pages;
      meta.textContent = [
        pages ? `${pages.toLocaleString('pt-BR')} páginas` : null,
        chapters ? `${chapters} ${chapters === 1 ? 'capítulo' : 'capítulos'}` : null,
        words,
      ].filter(Boolean).join(' · ');
    }
    row.append(purposeTag(purpose));

    const remove = document.createElement('button');
    remove.className = 'btn btn--danger';
    remove.textContent = 'Apagar';
    remove.onclick = async () => {
      if (!confirm(`Apagar "${project.title}"? Isso não pode ser desfeito.`)) return;
      await api(`/api/projects/${project.id}`, { method: 'DELETE' });
      toast('Livro apagado');
      loadProjects();
    };

    row.append(name, meta, remove);
    container.append(row);
  }
}

/* ------------------------------------------------------------- importação */

// Escolher o arquivo abre as opções; "Importar" envia e cria uma tarefa no
// servidor, acompanhada na lista da tela inicial até o livro ficar pronto.

const IMPORT_STAGES = {
  upload: 'Enviando',
  read: 'Lendo as páginas',
  ocr: 'Reconhecendo o texto (OCR)',
  build: 'Montando os capítulos',
  save: 'Gravando',
};

// Duas caixas, dois objetivos. As duas criam o mesmo tipo de livro, com as duas
// etapas; o objetivo decide por qual ele começa e o que o painel pergunta.
const PURPOSES = {
  convert: {
    heading: 'Converter PDF em EPUB',
    note: 'O livro abre na etapa de conversão, para revisar e exportar o EPUB. Dá para traduzir depois.',
    tag: 'Conversão',
    done: 'Revisar a conversão',
  },
  translate: {
    heading: 'Importar para traduzir',
    note: 'O livro abre na etapa de tradução. A etapa de conversão continua disponível para corrigir o texto.',
    tag: 'Tradução',
    done: 'Ir para a tradução',
  },
};

// Livros importados antes das duas caixas não têm objetivo: vale o que já foi feito.
function projectPurpose(project) {
  return project.purpose || (project.translated_count ? 'translate' : 'convert');
}

async function importFile(file, purpose) {
  if (!file) return;
  for (const input of [$('fileConvert'), $('fileTranslate')]) input.value = '';  // o mesmo arquivo de novo dispara onchange
  const kind = file.name.toLowerCase().match(/\.(pdf|epub)$/)?.[1];
  if (purpose === 'convert' && kind !== 'pdf') {
    toast(
      kind === 'epub'
        ? 'A conversão recebe só PDF. Para traduzir um EPUB, use a caixa Traduzir.'
        : 'Envie um arquivo .pdf',
      'error'
    );
    return;
  }
  if (!kind) { toast('Envie um arquivo .pdf ou .epub', 'error'); return; }

  state.pendingFile = file;
  state.pendingPurpose = purpose;
  $('importHeading').textContent = PURPOSES[purpose].heading;
  $('importPurpose').textContent = PURPOSES[purpose].note;
  $('importPurpose').dataset.purpose = purpose;
  const size = file.size < 1024 * 1024
    ? `${Math.max(1, Math.round(file.size / 1024))} KB`
    : `${(file.size / 1024 / 1024).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} MB`;
  $('importFileName').textContent = `${file.name} · ${size}`;
  $('importTitle').value = '';
  $('importTitle').placeholder = kind === 'pdf' ? 'detectar pela folha de rosto' : 'o que está no EPUB';
  $('importTitleHint').textContent = 'Deixe em branco para usar o título do arquivo. Dá para mudar depois.';
  for (const element of document.querySelectorAll('.pdf-only')) element.hidden = kind !== 'pdf';
  for (const element of document.querySelectorAll('.translate-only')) element.hidden = purpose !== 'translate';
  $('startImport').textContent = purpose === 'translate' ? 'Importar para traduzir' : 'Converter';
  $('importSheet').hidden = false;
  $('startImport').focus();
  await loadImportOptions();
  if (purpose === 'translate') fillTranslationFields();
}

// Idiomas e provedor: o padrão vem das preferências gerais.
function fillTranslationFields() {
  const languages = state.importOptions?.languages || [];
  const settings = state.settings || {};

  fillLanguageSelect($('importSource'), languages, languageCode(languages, settings.source_language) || 'en');
  fillLanguageSelect($('importTarget'), languages, languageCode(languages, settings.target_language) || 'pt-BR');

  const providers = settings.providers || [];
  const select = $('importProvider');
  select.innerHTML = '';
  for (const provider of providers) {
    select.append(new Option(providerLabel(provider), provider.id, false, provider.id === settings.active_provider_id));
  }
  select.disabled = !providers.length;
  if (!providers.length) select.append(new Option('Nenhum configurado', ''));
  $('importProviderHint').textContent = providers.length
    ? 'O livro traduz com este provedor. Se ele for apagado, vale o ativo nas configurações.'
    : 'Adicione um provedor em Configurações antes de traduzir. O livro pode ser importado mesmo assim.';
  syncOcrWithSource();
}

// Os idiomas chegam como nomes ("inglês") das preferências e do livro, e como
// códigos ("en") nos campos: a tabela do servidor liga um ao outro.
function languageCode(languages, name) {
  return languages.find((l) => l.name === (name || '').trim().toLowerCase())?.code || null;
}

function fillLanguageSelect(select, languages, chosen) {
  select.innerHTML = '';
  for (const { code, name } of languages) {
    select.append(new Option(name[0].toUpperCase() + name.slice(1), code, false, code === chosen));
  }
}

function providerLabel(provider) {
  return provider.model ? `${provider.label} · ${provider.model}` : provider.label;
}

// O idioma do livro também é o que o OCR precisa reconhecer.
const OCR_CODES = {
  en: 'eng', 'pt-BR': 'por', 'pt-PT': 'por', es: 'spa', fr: 'fra', de: 'deu',
  it: 'ita', nl: 'nld', ru: 'rus', ja: 'jpn', zh: 'chi_sim', la: 'lat',
};

function syncOcrWithSource() {
  const code = OCR_CODES[$('importSource').value];
  const box = $('importLangs').querySelector(`input[value="${code}"]`);
  if (box) box.checked = true;
}

async function loadImportOptions() {
  try {
    state.importOptions ??= await api('/api/imports/options');
  } catch (error) {
    toast(error.message, 'error');
    return;
  }
  const { ocr } = state.importOptions;
  const force = $('importOcr').querySelector('option[value="force"]');
  force.disabled = !ocr.available;
  if (!ocr.available && $('importOcr').value === 'force') $('importOcr').value = 'auto';
  $('importOcrHint').textContent = ocr.available
    ? '"Todas as páginas" serve para PDFs cujo texto sai embaralhado ou com ' +
      'letras trocadas. O OCR leva cerca de 1 segundo por página.'
    : 'O Tesseract não está instalado: páginas escaneadas ficam sem texto. ' +
      'Reconstrua a imagem: docker compose up -d --build.';

  const langs = $('importLangs');
  langs.innerHTML = '';
  // O Tesseract dá preferência ao primeiro idioma: os padrões vêm antes, na
  // ordem deles, e a ordem da tela é a ordem enviada.
  const defaults = ocr.default.split('+');
  const chosen = new Set(defaults);
  const ordered = [...defaults, ...ocr.languages.filter((code) => !chosen.has(code))];
  for (const code of ordered) {
    const label = document.createElement('label');
    label.className = 'check';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.value = code;
    box.checked = chosen.has(code);
    label.append(box, ` ${LANGUAGE_NAMES[code] || code}`);
    langs.append(label);
  }
  syncOcrFields();
}

// Os nomes dos idiomas mais comuns do Tesseract; os outros aparecem pelo código.
const LANGUAGE_NAMES = {
  por: 'Português', eng: 'Inglês', spa: 'Espanhol', fra: 'Francês', deu: 'Alemão',
  ita: 'Italiano', lat: 'Latim', nld: 'Holandês', rus: 'Russo', jpn: 'Japonês',
  chi_sim: 'Chinês simplificado', chi_tra: 'Chinês tradicional', ell: 'Grego', grc: 'Grego antigo',
};

function syncOcrFields() {
  const ocr = state.importOptions?.ocr;
  $('importLangsField').hidden = !ocr?.available || !ocr.languages.length || $('importOcr').value === 'off';
}

async function startImport() {
  const file = state.pendingFile;
  const purpose = state.pendingPurpose;
  if (!file) return;
  state.pendingFile = null;
  $('importSheet').hidden = true;

  const form = new FormData();
  form.append('file', file);
  form.append('purpose', purpose);
  form.append('title', $('importTitle').value.trim());
  if (purpose === 'translate') {
    form.append('source_language', $('importSource').value);
    form.append('target_language', $('importTarget').value);
    form.append('provider_id', $('importProvider').value);
  }
  if (file.name.toLowerCase().endsWith('.pdf')) {
    form.append('split_on', $('importSplit').value);
    form.append('ocr_mode', $('importOcr').value);
    const langs = [...$('importLangs').querySelectorAll('input:checked')].map((box) => box.value);
    if (langs.length) form.append('ocr_languages', langs.join('+'));
  }

  // Enquanto o arquivo sobe, a importação ainda não tem id no servidor.
  const local = {
    id: `envio-${Date.now()}`, filename: file.name, purpose,
    status: 'running', stage: 'upload', done: 0, total: 100,
  };
  state.imports.set(local.id, local);
  renderImports();

  try {
    const { import: job } = await upload('/api/imports', form, (fraction) => {
      local.done = Math.round(fraction * 100);
      renderImports();
    }, (xhr) => { local.abort = () => xhr.abort(); });
    state.imports.delete(local.id);
    state.imports.set(job.id, job);
    renderImports();
    watchImports();
  } catch (error) {
    state.imports.delete(local.id);
    renderImports();
    if (error.name !== 'AbortError') toast(error.message, 'error');
  }
}

// fetch não informa o progresso do envio; XMLHttpRequest sim. Livros de
// 200 MB levam alguns segundos para subir até num servidor local em Docker.
function upload(path, form, onProgress, onStart) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.responseType = 'json';
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status < 300) return resolve(xhr.response);
      const detail = xhr.response?.detail || `Erro ${xhr.status}`;
      reject(new Error(typeof detail === 'string' ? detail : JSON.stringify(detail)));
    };
    xhr.onerror = () => reject(new Error('Falha de rede ao enviar o arquivo'));
    xhr.onabort = () => reject(Object.assign(new Error('Envio cancelado'), { name: 'AbortError' }));
    onStart(xhr);
    xhr.send(form);
  });
}

function purposeTag(purpose) {
  const tag = document.createElement('span');
  tag.className = 'tag';
  tag.dataset.purpose = purpose;
  tag.textContent = PURPOSES[purpose]?.tag || purpose;
  return tag;
}

function renderImports() {
  const list = $('importList');
  list.innerHTML = '';
  for (const job of state.imports.values()) {
    const row = document.createElement('div');
    row.className = 'import';

    const name = document.createElement('span');
    name.className = 'import__name';
    name.append(purposeTag(job.purpose), job.filename);

    const status = document.createElement('span');
    status.className = 'import__status';
    status.textContent = importLabel(job);

    const track = document.createElement('span');
    track.className = 'progress__track import__track';
    const fill = document.createElement('span');
    fill.className = 'progress__fill';
    fill.style.width = `${job.total ? Math.round((job.done / job.total) * 100) : 0}%`;
    track.append(fill);

    const cancel = document.createElement('button');
    cancel.className = 'btn btn--quiet';
    cancel.textContent = 'Cancelar';
    cancel.disabled = Boolean(job.cancelling);
    cancel.onclick = () => cancelImport(job);

    row.append(name, status, track, cancel);
    list.append(row);
  }
}

function importLabel(job) {
  if (job.cancelling) return 'Cancelando…';
  const stage = IMPORT_STAGES[job.stage] || job.stage;
  if (job.stage === 'upload') return `${stage} · ${job.done}%`;
  if (job.stage === 'read' && job.total > 1) {
    return `${stage} · ${job.done} de ${job.total}`;
  }
  if (job.stage === 'ocr') {
    const eta = job.eta_seconds >= 2 ? ` · faltam ${formatDuration(job.eta_seconds)}` : '';
    return `${stage} · página ${job.done} de ${job.total}${eta}`;
  }
  return stage;
}

async function cancelImport(job) {
  if (job.abort) { job.abort(); return; }
  job.cancelling = true;
  renderImports();
  try {
    await api(`/api/imports/${job.id}/cancel`, { method: 'POST' });
  } catch (error) {
    toast(error.message, 'error');
  }
}

// Uma consulta por vez, para todas as importações: chamar de novo enquanto
// uma está agendada ou em andamento não faz nada.
function watchImports() {
  if (!state.importTimer) state.importTimer = setTimeout(pollImports, 300);
}

async function pollImports() {
  state.importTimer = true;  // em andamento
  const running = [...state.imports.values()].filter((job) => job.stage !== 'upload');

  for (const job of running) {
    let current;
    try {
      ({ import: current } = await api(`/api/imports/${job.id}`));
    } catch (error) {
      state.imports.delete(job.id);
      toast(error.message, 'error');
      continue;
    }
    if (current.status === 'running') {
      state.imports.set(job.id, { ...current, cancelling: job.cancelling });
      continue;
    }
    state.imports.delete(job.id);
    finishImport(current);
  }
  renderImports();
  const pending = [...state.imports.values()].some((job) => job.stage !== 'upload');
  state.importTimer = pending ? setTimeout(pollImports, 700) : null;
}

async function finishImport(job) {
  if (job.status === 'stopped') { toast(`Importação de "${job.filename}" cancelada.`); return; }
  if (job.status === 'error') { toast(job.error, 'error'); return; }

  loadProjects();
  // Quem já abriu outro livro enquanto esperava não é arrancado dele.
  if (state.project) {
    toast(`"${job.filename}" importado.`, 'info', {
      label: 'Abrir',
      onClick: () => openImported(job.project_id),
    });
    return;
  }
  openImported(job.project_id);
}

async function openImported(projectId) {
  await openProject(projectId);
  if (state.project?.import_summary) showSummary(state.project);
}

async function resumeImports() {
  try {
    const { imports } = await api('/api/imports');
    for (const job of imports) state.imports.set(job.id, job);
    renderImports();
    if (imports.length) watchImports();
  } catch { /* sem importações em andamento */ }
}

function showSummary(project) {
  const summary = project.import_summary || {};
  $('summaryFile').textContent = project.source_filename;
  $('summaryDone').textContent = PURPOSES[projectPurpose(project)].done;

  const number = (value) => Number(value || 0).toLocaleString('pt-BR');
  const stats = [
    ['Páginas', summary.pages != null ? number(summary.pages) : null],
    ['Páginas lidas pelo OCR', summary.ocr_pages ? number(summary.ocr_pages) : null],
    ['Idiomas do OCR', summary.ocr_pages
      ? summary.ocr_languages.split('+').map((code) => LANGUAGE_NAMES[code] || code).join(', ')
      : null],
    ['Capítulos', number(summary.chapters)],
    ['Palavras', number(summary.words)],
    ['Títulos', number(summary.headings)],
    ['Parágrafos', number(summary.paragraphs)],
    ['Notas de rodapé', summary.footnotes ? number(summary.footnotes) : null],
    ['Tabelas', summary.tables ? number(summary.tables) : null],
    ['Imagens', summary.images ? number(summary.images) : null],
    ['Tempo', summary.seconds != null
      ? (summary.seconds < 1 ? 'menos de 1s' : formatDuration(summary.seconds))
      : null],
  ];
  const list = $('summaryStats');
  list.innerHTML = '';
  for (const [label, value] of stats) {
    if (value == null) continue;
    const item = document.createElement('div');
    const term = document.createElement('dt');
    term.textContent = label;
    const detail = document.createElement('dd');
    detail.textContent = value;
    item.append(term, detail);
    list.append(item);
  }

  const warnings = $('summaryWarnings');
  warnings.innerHTML = '';
  if (summary.warnings?.length) {
    const head = document.createElement('p');
    head.className = 'summary__head';
    head.textContent = 'Vale conferir';
    const items = document.createElement('ul');
    items.className = 'summary__warnings';
    for (const text of summary.warnings) {
      const item = document.createElement('li');
      item.textContent = text;
      items.append(item);
    }
    warnings.append(head, items);
  }
  $('summarySheet').hidden = false;
}

/* ------------------------------------------------------------- a bancada */

async function openProject(projectId) {
  try {
    const data = await api(`/api/projects/${projectId}`);
    state.project = data.project;
    state.doc = data.document;
    state.chapterId = data.document.chapters[0]?.id || null;
    state.stage = initialStage(data.project);
    state.suspects = [];
    state.lastSuspect = null;
    state.review = { suggestions: [] };

    $('start').hidden = true;
    for (const id of ['rail', 'editor', 'preview']) $(id).hidden = false;
    for (const id of ['homeBtn', 'exportBtn', 'bookBar']) $(id).hidden = false;

    $('bookTitle').value = data.document.title;
    applyStage();
    renderRail();
    renderEditor();
    refreshPreview();
    updateStat();
    loadSuspects();
    loadReview();
    watchExistingJob(projectId);
  } catch (error) {
    toast(error.message, 'error');
  }
}

function goHome() {
  flushSaves();
  stopPolling();
  state.project = null;
  state.doc = null;
  state.suspects = [];
  state.review = { suggestions: [] };
  stopReviewPolling();
  $('reviewRun').hidden = true;
  $('reviewBtn').hidden = true;
  $('start').hidden = false;
  for (const id of ['rail', 'editor', 'preview']) $(id).hidden = true;
  for (const id of ['homeBtn', 'translateBtn', 'exportBtn', 'bookBar', 'progress']) $(id).hidden = true;
  loadProjects();
}

/* ---------------------------------------------------------------- etapas */

// Conversão: o texto e a estrutura que vieram do arquivo, para corrigir antes
// de exportar ou traduzir. Tradução: o campo de tradução sob cada original.
// A etapa fica lembrada por livro, neste navegador.

function stageKey(projectId) {
  return `interlinear.stage.${projectId}`;
}

function initialStage(project) {
  try {
    const saved = localStorage.getItem(stageKey(project.id));
    if (saved === 'convert' || saved === 'translate') return saved;
  } catch { /* armazenamento bloqueado: decide pelo livro */ }
  return projectPurpose(project);
}

function setStage(stage) {
  if (!state.project || stage === state.stage) return;
  flushSaves();
  state.stage = stage;
  try { localStorage.setItem(stageKey(state.project.id), stage); } catch { /* fica só nesta sessão */ }
  applyStage();
  renderRail();
  renderEditor();
  refreshPreview();
  updateStat();
  loadSuspects();
  loadReview();
}

function applyStage() {
  const translating = state.stage === 'translate';
  $('stageConvert').setAttribute('aria-pressed', !translating);
  $('stageTranslate').setAttribute('aria-pressed', translating);
  $('translateBtn').hidden = !translating;
  // A revisão por IA é da conversão; enquanto roda, o botão dá lugar ao progresso.
  $('reviewBtn').hidden = translating || Boolean(state.reviewTimer);
  $('reviewRun').hidden = translating || !state.reviewTimer;
  $('previewToggle').hidden = !translating;
  $('previewLabel').hidden = translating;
  $('editor').dataset.stage = state.stage;
}

function chapterProgress(chapter) {
  const translatable = chapter.blocks.filter(
    (b) => b.text.trim() && b.kind !== 'image' && b.kind !== 'page_break'
  );
  const done = translatable.filter((b) => b.translated && b.translated.trim());
  const stale = done.filter((b) => b.meta?.stale);
  const refused = translatable.filter((b) => b.meta?.refused);
  return {
    done: done.length, total: translatable.length, stale: stale.length, refused: refused.length,
  };
}

function chapterPages(chapter) {
  const pages = chapter.blocks.map((b) => b.page).filter(Boolean);
  if (!pages.length) return '';
  const first = Math.min(...pages);
  const last = Math.max(...pages);
  return first === last ? `p. ${first}` : `p. ${first}–${last}`;
}

function renderRail() {
  const list = $('chapterList');
  list.innerHTML = '';
  $('chapterCount').textContent = state.doc.chapters.length;
  const converting = state.stage === 'convert';

  state.doc.chapters.forEach((chapter, index) => {
    const { done, total } = chapterProgress(chapter);
    const pct = total ? Math.round((done / total) * 100) : 0;

    const button = document.createElement('button');
    button.className = 'chapter';
    button.setAttribute('aria-current', chapter.id === state.chapterId);
    button.onclick = () => selectChapter(chapter.id);

    const title = document.createElement('span');
    title.className = 'chapter__title';
    title.textContent = chapter.title || `Capítulo ${index + 1}`;

    const meta = document.createElement('span');
    meta.className = 'chapter__meta';
    if (converting) {
      const blocks = chapter.blocks.length;
      const doubts = suspectBlocks(chapter.id).length;
      meta.textContent = [
        `${blocks} ${blocks === 1 ? 'bloco' : 'blocos'}`,
        chapterPages(chapter),
        doubts ? `${doubts} ${doubts === 1 ? 'duvidoso' : 'duvidosos'}` : '',
      ].filter(Boolean).join(' · ');
      button.append(title, meta);
    } else {
      meta.textContent = total ? `${done}/${total} parágrafos` : 'sem texto';
      const gauge = document.createElement('span');
      gauge.className = 'chapter__gauge';
      const fill = document.createElement('i');
      fill.style.width = `${pct}%`;
      gauge.append(fill);
      button.append(title, meta, gauge);
    }
    list.append(button);
  });
}

function selectChapter(chapterId) {
  flushSaves();
  state.chapterId = chapterId;
  renderRail();
  renderEditor();
  refreshPreview();
  $('editor').scrollTop = 0;
}

function currentChapter() {
  return state.doc?.chapters.find((c) => c.id === state.chapterId) || null;
}

const MARKS = {
  heading1: 'H1', heading2: 'H2', heading3: 'H3',
  paragraph: '¶', quote: '❝', list_item: '•', footnote: '†', table: '▦',
  image: '▣', page_break: '⎯',
};

const KIND_LABELS = {
  heading1: 'Título 1', heading2: 'Título 2', heading3: 'Título 3',
  paragraph: 'Parágrafo', quote: 'Citação', list_item: 'Item de lista',
  footnote: 'Nota de rodapé', table: 'Tabela',
};

function blockState(block) {
  if (block.translated && block.meta?.stale) return 'stale';
  if (block.meta?.refused && !block.translated?.trim()) return 'refused';
  if (block.meta?.edited) return 'human';
  if (block.translated && block.translated.trim()) return 'machine';
  return 'pending';
}

function renderEditor() {
  const chapter = currentChapter();
  const inner = $('editorInner');
  inner.innerHTML = '';
  if (!chapter) return;

  const title = document.createElement('input');
  title.className = 'editor__chapter-title';
  title.value = chapter.title;
  title.setAttribute('aria-label', 'Título do capítulo');
  title.oninput = () => {
    chapter.title = title.value;
    scheduleChapterTitleSave();
  };
  inner.append(title);

  const note = document.createElement('p');
  note.className = 'editor__note';
  if (state.stage === 'convert') {
    note.textContent =
      'Confira o que veio do arquivo: clique num trecho para corrigir o texto, ' +
      'troque o tipo do bloco ou comece um capítulo onde o livro começa. ';
    if (state.project.import_summary?.chapters != null) {
      const link = document.createElement('button');
      link.className = 'editor__link';
      link.textContent = 'Resumo da importação';
      link.onclick = () => showSummary(state.project);
      note.append(link);
    }
  } else {
    const { done, total, stale, refused } = chapterProgress(chapter);
    note.textContent = total
      ? `${done} de ${total} parágrafos traduzidos.` +
        (stale ? ` ${stale} com o original alterado depois da tradução.` : '') +
        (refused ? ` ${refused} que o modelo se recusou a traduzir.` : '') +
        ' O texto original fica acima de cada campo.'
      : 'Capítulo sem texto traduzível.';
    note.append(' ', ...bookTranslationLine());
  }
  inner.append(note);

  if (state.stage === 'convert') {
    const doubts = document.createElement('p');
    doubts.id = 'suspectNav';
    doubts.className = 'editor__suspects';
    const suggestions = document.createElement('p');
    suggestions.id = 'reviewNav';
    suggestions.className = 'editor__review';
    inner.append(doubts, suggestions);
    renderSuspectNav();
    renderReviewNav();
  }

  chapter.blocks.forEach((block, index) => {
    inner.append(renderBlock(block, chapter.blocks[index + 1]));
  });
}

const TEXT_KINDS = new Set(Object.keys(KIND_LABELS));

// Mesma regra do inline_html do backend: escapa tudo e reabilita só as tags de
// ênfase, para o original mostrar itálico e negrito em vez de "<em>".
const ALLOWED_INLINE = /&lt;(\/?)(em|strong|i|b|sup|sub|small)&gt;|&lt;br\s*\/?&gt;/gi;

function inlineHtml(text) {
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
  return escaped.replace(ALLOWED_INLINE, (match, slash, tag) =>
    tag ? `<${slash}${tag.toLowerCase()}>` : '<br>'
  );
}

function autoGrow(field) {
  field.style.height = 'auto';
  field.style.height = `${field.scrollHeight}px`;
}

function renderMark(block) {
  const mark = document.createElement('span');
  mark.className = 'block__mark';
  mark.textContent = MARKS[block.kind] || '¶';
  mark.title = block.page ? `Página ${block.page} do original` : '';
  if (block.kind === 'footnote') {
    // A nota mostra o próprio marcador ("1", "*"), que liga ela à chamada no texto.
    mark.textContent = block.meta?.marker || MARKS.footnote;
    mark.title = `Nota de rodapé${mark.title ? ` · ${mark.title}` : ''}`;
  }
  return mark;
}

function renderBlock(block, next) {
  const wrapper = document.createElement('div');
  wrapper.className = 'block';
  wrapper.dataset.id = block.id;
  wrapper.dataset.kind = block.kind;
  wrapper.append(renderMark(block));
  const converting = state.stage === 'convert';

  if (block.kind === 'image') {
    const img = document.createElement('p');
    img.className = 'block__source';
    img.textContent = `[imagem: ${block.src || 'sem arquivo'}]`;
    wrapper.append(img);
    if (converting) {
      wrapper.append(renderStructureTools(block, next, wrapper));
      renderSuspectNote(block, wrapper);
      renderSuggestion(block, wrapper);
    }
    return wrapper;
  }

  if (converting) {
    wrapper.append(renderSourceEditor(block), renderStructureTools(block, next, wrapper));
    renderSuspectNote(block, wrapper);
    renderSuggestion(block, wrapper);
  } else {
    wrapper.dataset.state = blockState(block);
    renderTranslation(block, wrapper);
  }
  return wrapper;
}

// Conversão: o original aparece formatado; um clique troca por um campo com o
// texto cru, onde o itálico aparece como <em>…</em>.
function renderSourceEditor(block) {
  const box = document.createElement('div');

  const view = document.createElement('p');
  view.className = 'block__text';
  view.tabIndex = 0;
  view.setAttribute('role', 'button');
  view.title = 'Clique para corrigir o texto';

  const field = document.createElement('textarea');
  field.className = 'block__text block__text-field';
  field.rows = 1;
  field.hidden = true;
  field.placeholder = 'texto que faltou na conversão';
  field.setAttribute('aria-label', 'Texto original deste bloco');

  const show = () => {
    view.innerHTML = inlineHtml(block.text);
    view.dataset.empty = !block.text.trim();
  };
  const open = () => {
    view.hidden = true;
    field.hidden = false;
    field.value = block.text;
    autoGrow(field);
    field.focus();
  };

  view.onclick = open;
  view.onkeydown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      open();
    }
  };
  field.oninput = () => {
    autoGrow(field);
    block.text = field.value;
    // O servidor marca o mesmo ao gravar; aqui é só para a tela concordar.
    if (block.translated) block.meta = { ...(block.meta || {}), stale: true };
    queueUpdate(block.id, { text: block.text });
  };
  field.onblur = () => {
    show();
    field.hidden = true;
    view.hidden = false;
    flushSaves();
  };

  show();
  box.append(view, field);
  return box;
}

// Tradução: o original fixo em cima, o campo de tradução embaixo.
function renderTranslation(block, wrapper) {
  const source = document.createElement('p');
  source.className = 'block__source';
  source.innerHTML = inlineHtml(block.text);
  if (!block.text.trim()) source.textContent = '(sem texto: escreva o original na etapa de conversão)';
  wrapper.append(source);

  const stale = document.createElement('p');
  stale.className = 'block__stale';
  stale.hidden = blockState(block) !== 'stale';
  stale.textContent = 'O original mudou depois desta tradução. ';
  const keep = document.createElement('button');
  keep.className = 'editor__link';
  keep.textContent = 'Manter a tradução';
  keep.onclick = () => {
    block.meta = { ...(block.meta || {}), stale: false };
    queueUpdate(block.id, { meta: { stale: false } });
    stale.hidden = true;
    wrapper.dataset.state = blockState(block);
  };
  stale.append(keep);
  wrapper.append(stale);

  // A recusa não entra como tradução. A resposta fica à vista porque a
  // detecção pode errar: aí o botão aceita a resposta como tradução.
  const refused = document.createElement('details');
  refused.className = 'block__refused';
  refused.hidden = !block.meta?.refused;
  const summary = document.createElement('summary');
  summary.textContent =
    'O modelo se recusou a traduzir este trecho. Tente de novo, traduza à mão ou use outro provedor.';
  const reply = document.createElement('p');
  reply.textContent = block.meta?.refused || '';
  const accept = document.createElement('button');
  accept.className = 'editor__link';
  accept.textContent = 'É tradução: usar esta resposta';
  refused.append(summary, reply, accept);
  wrapper.append(refused);

  const dropRefusal = () => {
    if (!block.meta?.refused) return;
    const { refused: _, ...meta } = block.meta;
    block.meta = meta;
    refused.hidden = true;
  };

  const field = document.createElement('textarea');
  field.className = 'block__field';
  field.rows = 1;
  field.value = block.translated || '';
  field.placeholder = 'ainda não traduzido';
  field.setAttribute('aria-label', 'Tradução deste parágrafo');

  const grow = () => autoGrow(field);

  field.oninput = () => {
    grow();
    block.translated = field.value;
    block.meta = { ...(block.meta || {}), edited: true, stale: false };
    stale.hidden = true;
    dropRefusal();  // gravar a tradução desfaz a recusa no servidor também
    wrapper.dataset.state = blockState(block);
    markDirty(block);
  };
  field.onblur = () => flushSaves();
  accept.onclick = () => {
    field.value = block.meta.refused;
    field.dispatchEvent(new Event('input'));
    flushSaves();
  };

  wrapper.append(field);
  requestAnimationFrame(grow);

  const tools = document.createElement('div');
  tools.className = 'block__tools';

  const copy = document.createElement('button');
  copy.className = 'block__tool';
  copy.textContent = 'Usar original';
  copy.title = 'Copia o texto original para o campo de tradução';
  copy.onclick = () => {
    field.value = block.text;
    field.dispatchEvent(new Event('input'));
  };

  const clear = document.createElement('button');
  clear.className = 'block__tool';
  clear.textContent = 'Limpar';
  clear.onclick = () => {
    field.value = '';
    block.translated = null;
    block.meta = { ...(block.meta || {}), edited: false, stale: false };
    stale.hidden = true;
    dropRefusal();
    wrapper.dataset.state = 'pending';
    markDirty(block, true);
    grow();
  };

  tools.append(copy, clear);
  wrapper.append(tools);
}

// Os consertos de estrutura, só na etapa de conversão: tipo do bloco, começo
// de capítulo, juntar, inserir e apagar. Valem para imagens também (apagar uma
// figura solta, inserir texto depois dela).
function renderStructureTools(block, next, wrapper) {
  const tools = document.createElement('div');
  tools.className = 'block__tools';

  if (TEXT_KINDS.has(block.kind)) {
    const kind = document.createElement('select');
    kind.className = 'block__tool block__kind';
    kind.setAttribute('aria-label', 'Tipo do bloco');
    for (const [value, label] of Object.entries(KIND_LABELS)) {
      kind.append(new Option(label, value, false, value === block.kind));
    }
    kind.onchange = () => {
      block.kind = kind.value;
      wrapper.dataset.kind = block.kind;
      wrapper.querySelector('.block__mark').replaceWith(renderMark(block));
      queueUpdate(block.id, { kind: block.kind });
      flushSaves();
    };
    tools.append(kind);
  }

  const split = document.createElement('button');
  split.className = 'block__tool';
  split.textContent = 'Começar capítulo aqui';
  split.onclick = () => splitHere(block.id);
  tools.append(split);

  if (TEXT_KINDS.has(block.kind) && next?.kind === block.kind) {
    const merge = document.createElement('button');
    merge.className = 'block__tool';
    merge.textContent = 'Juntar com o próximo';
    merge.title = 'Para parágrafos que a conversão partiu em dois';
    merge.onclick = () => mergeWithNext(block);
    tools.append(merge);
  }

  const insert = document.createElement('button');
  insert.className = 'block__tool';
  insert.textContent = 'Inserir abaixo';
  insert.title = 'Novo parágrafo para um trecho que faltou na conversão';
  insert.onclick = () => insertBelow(block);
  tools.append(insert);

  // Apagar pede um segundo clique em 3 s, e o aviso ainda oferece desfazer.
  const remove = document.createElement('button');
  remove.className = 'block__tool block__tool--danger';
  remove.textContent = 'Apagar';
  remove.onclick = () => {
    if (remove.dataset.armed) {
      deleteBlock(block);
      return;
    }
    remove.dataset.armed = '1';
    remove.textContent = 'Clique de novo para apagar';
    setTimeout(() => {
      delete remove.dataset.armed;
      remove.textContent = 'Apagar';
    }, 3000);
  };
  tools.append(remove);

  return tools;
}

/* ------------------------------------------------------------- suspeitos */

// Trechos que a conversão provavelmente errou: parágrafo partido, cabeçalho
// que escapou, título longo demais (backend/app/core/review.py). Só na etapa
// de conversão. O servidor recalcula depois de cada gravação, então corrigir
// um trecho o tira da lista.

async function loadSuspects() {
  if (!state.project || state.stage !== 'convert') return;
  const projectId = state.project.id;
  const seq = ++state.suspectSeq;
  let data;
  try {
    data = await api(`/api/projects/${projectId}/suspects`);
  } catch {
    return; // a marcação ajuda a revisar, mas não pode atrapalhar a edição
  }
  // Uma resposta atrasada (de outro livro ou de antes da última gravação) não vale.
  if (seq !== state.suspectSeq || state.project?.id !== projectId) return;
  state.suspects = data.suspects;
  paintSuspects();
  renderRail();
}

// Ids dos blocos duvidosos, na ordem de leitura e sem repetir.
function suspectBlocks(chapterId = null) {
  const ids = state.suspects
    .filter((s) => s.block_id && (!chapterId || s.chapter_id === chapterId))
    .map((s) => s.block_id);
  return [...new Set(ids)];
}

function renderSuspectNote(block, wrapper) {
  wrapper.querySelector('.block__suspect')?.remove();
  const found = state.stage === 'convert'
    ? state.suspects.filter((s) => s.block_id === block.id)
    : [];
  wrapper.dataset.suspect = String(found.length > 0);
  if (!found.length) return;
  const note = document.createElement('p');
  note.className = 'block__suspect';
  // O detalhe é a evidência: "repetido nas páginas 3, 5 e 7", "9 palavras".
  note.textContent = found.map((s) => (s.detail ? `${s.message} (${s.detail})` : s.message)).join(' ');
  wrapper.querySelector('.block__mark').after(note);
}

// Redesenha só as marcas, sem refazer o editor: quem está digitando não perde
// o cursor quando a lista chega.
function paintSuspects() {
  for (const block of currentChapter()?.blocks || []) {
    const wrapper = document.querySelector(`.block[data-id="${block.id}"]`);
    if (wrapper) renderSuspectNote(block, wrapper);
  }
  renderSuspectNav();
}

function renderSuspectNav() {
  const nav = $('suspectNav');
  if (!nav) return;
  nav.innerHTML = '';
  const total = suspectBlocks().length;
  nav.hidden = !total;
  if (!total) return;
  nav.append(
    `${total} ${total === 1 ? 'trecho duvidoso' : 'trechos duvidosos'} no livro. `
  );
  const next = document.createElement('button');
  next.className = 'editor__link';
  next.textContent = 'Ir para o próximo';
  next.onclick = nextSuspect;
  nav.append(next);
}

function nextSuspect() {
  const ids = suspectBlocks();
  if (!ids.length) return;
  const order = new Map(
    state.doc.chapters.flatMap((c) => c.blocks.map((b) => b.id)).map((id, i) => [id, i])
  );
  // Continua de onde parou; se aquele bloco sumiu (juntado, apagado), recomeça.
  const current = order.get(state.lastSuspect) ?? -1;
  const target = ids.find((id) => (order.get(id) ?? -1) > current) || ids[0];
  state.lastSuspect = target;

  goToBlock(target);
}

// Abre o capítulo do bloco, rola até ele e dá um realce breve.
function goToBlock(blockId) {
  const chapter = state.doc.chapters.find((c) => c.blocks.some((b) => b.id === blockId));
  if (!chapter) return;
  if (chapter.id !== state.chapterId) selectChapter(chapter.id);
  const wrapper = document.querySelector(`.block[data-id="${blockId}"]`);
  if (!wrapper) return;
  wrapper.scrollIntoView({ block: 'center', behavior: 'smooth' });
  wrapper.classList.remove('block--flash');
  void wrapper.offsetWidth; // reinicia a animação num segundo clique
  wrapper.classList.add('block--flash');
  clearTimeout(wrapper.flashTimer);
  wrapper.flashTimer = setTimeout(() => wrapper.classList.remove('block--flash'), 1600);
}

/* -------------------------------------------------------- revisão por IA */

// O modelo confere os trechos duvidosos e sugere o que fazer; nada muda no
// livro sem o usuário aceitar (backend/app/routes/review.py). Aceitar é uma
// edição comum, conferida de novo contra o livro de agora.

const OP_LABELS = {
  ok: 'Está certo',
  merge_prev: 'Juntar com o anterior',
  merge_next: 'Juntar com o próximo',
  set_kind: 'Trocar o tipo',
  delete: 'Apagar',
  fix_text: 'Corrigir o texto',
  start_chapter: 'Começar capítulo aqui',
  merge_chapter: 'Juntar ao capítulo anterior',
  insert_after: 'Inserir texto que faltou',
};

// Na ordem em que aparecem no painel: o que mais aparece e menos arrisca primeiro.
const OP_ORDER = [
  'delete', 'merge_next', 'merge_prev', 'fix_text', 'insert_after', 'set_kind',
  'start_chapter', 'merge_chapter', 'ok',
];

const THRESHOLD_KEY = 'interlinear.reviewThreshold';

function reviewThreshold() {
  if (state.reviewThreshold != null) return state.reviewThreshold;
  try {
    const saved = localStorage.getItem(THRESHOLD_KEY);
    if (saved !== null && !Number.isNaN(Number(saved))) return Number(saved);
  } catch { /* armazenamento bloqueado: usa o padrão */ }
  return 0.9;
}

function pendingSuggestions() {
  return state.review.suggestions.filter((s) => s.status === 'pending');
}

async function loadReview() {
  if (!state.project || state.stage !== 'convert') return;
  const projectId = state.project.id;
  const seq = ++state.reviewSeq;
  let data;
  try {
    data = await api(`/api/projects/${projectId}/review`);
  } catch {
    return; // as sugestões ajudam, mas não podem atrapalhar a edição
  }
  if (seq !== state.reviewSeq || state.project?.id !== projectId) return;
  state.review = data;
  if (data.job?.status === 'running' && !state.reviewTimer) startReviewPolling(data.job);
  paintSuggestions();
}

// body: {} revisa os trechos duvidosos; {mode: 'visual', pages?} compara com
// a imagem das páginas do PDF.
async function startReview(body = {}) {
  if (!state.project) return;
  closeReviewMenu();
  await flushSaves();
  try {
    const data = await api(`/api/projects/${state.project.id}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!data.job) { toast(data.message || 'Nada a revisar'); return; }
    toast(`Revisando com ${data.model || data.provider}`);
    startReviewPolling(data.job);
  } catch (error) {
    toast(error.message, 'error');
  }
}

function showReviewRun(job) {
  const done = job.done + job.failed;
  $('reviewRunLabel').textContent = `Revisando ${done}/${job.total}`;
}

/* Menu: trechos duvidosos (texto) ou páginas do PDF (imagem). */

// A página do primeiro bloco visível no editor: a que o usuário está olhando.
function visiblePage() {
  const top = $('editor').getBoundingClientRect().top;
  for (const wrapper of document.querySelectorAll('#editorInner .block')) {
    if (wrapper.getBoundingClientRect().bottom < top + 40) continue;
    const page = blockById(wrapper.dataset.id)?.page;
    if (page) return page;
  }
  return null;
}

function openReviewMenu() {
  const fromPdf = state.project?.source_kind === 'pdf';
  const page = fromPdf ? visiblePage() : null;
  $('reviewPages').disabled = !fromPdf;
  $('reviewThisPage').disabled = !page;
  $('reviewThisPage').dataset.page = page || '';
  $('reviewThisPageTitle').textContent = page ? `Página ${page}` : 'Esta página';
  if (!fromPdf) {
    $('reviewPages').title = 'Só para livros importados de PDF';
    $('reviewThisPage').title = 'Só para livros importados de PDF';
  }
  closeExportMenu();
  $('reviewMenu').hidden = false;
  $('reviewBtn').setAttribute('aria-expanded', 'true');
  $('reviewText').focus();
}

function closeReviewMenu() {
  $('reviewMenu').hidden = true;
  $('reviewBtn').setAttribute('aria-expanded', 'false');
}

function startReviewPolling(job) {
  stopReviewPolling();
  showReviewRun(job);
  state.reviewTimer = setInterval(pollReview, 1200);
  applyStage();
}

function stopReviewPolling() {
  clearInterval(state.reviewTimer);
  state.reviewTimer = null;
}

async function pollReview() {
  if (!state.project) return stopReviewPolling();
  const projectId = state.project.id;
  let data;
  try {
    data = await api(`/api/projects/${projectId}/review`);
  } catch (error) {
    stopReviewPolling();
    applyStage();
    toast(error.message, 'error');
    return;
  }
  if (state.project?.id !== projectId) return;
  const job = data.job;
  if (job?.status === 'running') { showReviewRun(job); return; }

  stopReviewPolling();
  applyStage();
  state.review = data;
  paintSuggestions();
  loadSuspects();

  if (job?.status === 'error') {
    toast(`A revisão parou: ${job.error}`, 'error');
  } else if (job?.status === 'stopped') {
    toast('Revisão interrompida.');
  } else {
    const count = pendingSuggestions().length;
    if (!count) {
      toast('A IA não sugeriu nenhuma mudança.');
    } else {
      toast(
        `A IA sugeriu ${count} ${count === 1 ? 'mudança' : 'mudanças'}.`,
        'info',
        { label: 'Ver', onClick: openReviewSheet },
      );
    }
  }
}

async function stopReview() {
  if (!state.project) return;
  try {
    await api(`/api/projects/${state.project.id}/review/stop`, { method: 'POST' });
    toast('Parando depois dos lotes em andamento…');
  } catch (error) {
    toast(error.message, 'error');
  }
}

// Redesenha só os cartões, como as marcas de suspeito: sem tirar o cursor.
function paintSuggestions() {
  for (const block of currentChapter()?.blocks || []) {
    const wrapper = document.querySelector(`.block[data-id="${block.id}"]`);
    if (wrapper) renderSuggestion(block, wrapper);
  }
  renderReviewNav();
  if (!$('reviewSheet').hidden) renderReviewSheet();
}

function renderReviewNav() {
  const nav = $('reviewNav');
  if (!nav) return;
  nav.innerHTML = '';
  const count = pendingSuggestions().length;
  nav.hidden = !count;
  if (!count) return;
  nav.append(`A IA sugeriu ${count} ${count === 1 ? 'mudança' : 'mudanças'}. `);
  const open = document.createElement('button');
  open.className = 'editor__link';
  open.textContent = 'Ver todas';
  open.onclick = openReviewSheet;
  nav.append(open);
}

function percent(confidence) {
  return `${Math.round((confidence || 0) * 100)}%`;
}

// O trecho que mudou, com um pouco de contexto: "…na <del>l</del><ins>I</ins>sso…".
function changeExcerpt(before, after, context = 24) {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) start++;
  let end = 0;
  while (
    end < before.length - start && end < after.length - start &&
    before[before.length - 1 - end] === after[after.length - 1 - end]
  ) end++;
  const head = before.slice(Math.max(0, start - context), start);
  const tail = before.slice(before.length - end, before.length - end + context);
  const line = document.createElement('p');
  line.className = 'ai__diff';
  if (start > context) line.append('…');
  line.append(head);
  const removed = before.slice(start, before.length - end);
  const added = after.slice(start, after.length - end);
  if (removed) { const del = document.createElement('del'); del.textContent = removed; line.append(del); }
  if (added) { const ins = document.createElement('ins'); ins.textContent = added; line.append(ins); }
  line.append(tail);
  if (before.length - end + context < before.length) line.append('…');
  return line;
}

function suggestionTitle(suggestion, block) {
  if (suggestion.op === 'set_kind') {
    const from = KIND_LABELS[block?.kind] || block?.kind || '';
    const to = KIND_LABELS[suggestion.payload?.kind] || suggestion.payload?.kind;
    return `Trocar o tipo: ${from} → ${to}`;
  }
  if (suggestion.op === 'ok') return 'A IA acha que este trecho está certo';
  if (suggestion.op === 'insert_after') return 'Inserir, depois deste trecho, o texto que faltou';
  return OP_LABELS[suggestion.op] || suggestion.op;
}

function renderSuggestion(block, wrapper) {
  wrapper.querySelector('.ai')?.remove();
  if (state.stage !== 'convert') return;
  const suggestion = pendingSuggestions().find((s) => s.block_id === block.id);
  wrapper.dataset.suggestion = String(Boolean(suggestion));
  if (!suggestion) return;

  const card = document.createElement('div');
  card.className = 'ai';
  card.dataset.op = suggestion.op;

  const head = document.createElement('p');
  head.className = 'ai__head';
  const badge = document.createElement('span');
  badge.className = 'ai__badge';
  // A revisão visual leu a imagem da página: o usuário sabe de onde veio.
  badge.textContent = suggestion.source === 'visual' ? 'IA · página' : 'IA';
  if (suggestion.source === 'visual') badge.title = `Comparado com a imagem da página ${block.page ?? ''} do PDF`;
  const title = document.createElement('b');
  title.textContent = suggestionTitle(suggestion, block);
  const confidence = document.createElement('span');
  confidence.className = 'ai__confidence';
  confidence.textContent = percent(suggestion.confidence);
  confidence.title = 'Confiança do modelo nesta sugestão';
  head.append(badge, title, confidence);
  card.append(head);

  if (suggestion.reason) {
    const reason = document.createElement('p');
    reason.className = 'ai__reason';
    reason.textContent = suggestion.reason;
    card.append(reason);
  }
  if (suggestion.op === 'fix_text' && suggestion.payload?.text) {
    card.append(changeExcerpt(block.text, suggestion.payload.text));
  }
  if (suggestion.op === 'insert_after' && suggestion.payload?.text) {
    const line = document.createElement('p');
    line.className = 'ai__diff';
    const ins = document.createElement('ins');
    ins.textContent = suggestion.payload.text;
    line.append(ins);
    card.append(line);
  }
  if (suggestion.problem) {
    const problem = document.createElement('p');
    problem.className = 'ai__problem';
    problem.textContent = `Não dá mais para aplicar: ${suggestion.problem}.`;
    card.append(problem);
  }

  const actions = document.createElement('div');
  actions.className = 'ai__actions';
  const accept = document.createElement('button');
  accept.className = 'ai__accept';
  accept.textContent = suggestion.op === 'ok' ? 'Confirmar' : 'Aplicar';
  accept.title = suggestion.op === 'ok'
    ? 'Tira a marca de trecho duvidoso deste bloco'
    : 'Faz a mudança no livro, como se você tivesse feito à mão';
  accept.disabled = Boolean(suggestion.problem);
  accept.onclick = () => decideSuggestions([suggestion.id], true);
  const reject = document.createElement('button');
  reject.className = 'ai__reject';
  reject.textContent = suggestion.problem ? 'Descartar' : 'Rejeitar';
  reject.onclick = () => decideSuggestions([suggestion.id], false);
  actions.append(accept, reject);
  card.append(actions);

  const anchor = wrapper.querySelector('.block__suspect') || wrapper.querySelector('.block__mark');
  anchor.after(card);
}

// Aceita ou rejeita. Aceitar pode mexer em capítulos inteiros: o servidor
// devolve o livro, e a bancada é redesenhada.
async function decideSuggestions(ids, accept) {
  if (!ids.length || !state.project) return;
  await flushSaves();
  await sendDecision(`/review/${accept ? 'accept' : 'reject'}`, { ids });
}

async function acceptGroup(op) {
  await flushSaves();
  const body = { min_confidence: reviewThreshold() };
  if (op) body.op = op;
  await sendDecision('/review/accept-all', body);
}

async function sendDecision(path, body) {
  try {
    const outcome = await api(`/api/projects/${state.project.id}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (outcome.document) {
      state.doc = outcome.document;
      if (!currentChapter()) state.chapterId = state.doc.chapters[0]?.id || null;
    }
    if (outcome.project) state.project = outcome.project;
    if (outcome.document) {
      renderRail();
      renderEditor();
      refreshPreview();
    }
    updateStat();
    await loadReview();
    loadSuspects();
    toast(decisionNote(outcome));
  } catch (error) {
    toast(error.message, 'error');
  }
}

function decisionNote({ accepted = [], rejected = [], stale = [] }) {
  const parts = [];
  if (accepted.length) parts.push(`${accepted.length} ${accepted.length === 1 ? 'aplicada' : 'aplicadas'}`);
  if (rejected.length) parts.push(`${rejected.length} ${rejected.length === 1 ? 'rejeitada' : 'rejeitadas'}`);
  if (stale.length) {
    parts.push(`${stale.length} não ${stale.length === 1 ? 'pôde' : 'puderam'} ser ${stale.length === 1 ? 'aplicada' : 'aplicadas'}: o livro mudou desde a revisão`);
  }
  return parts.length ? `${parts.join(' · ')}.` : 'Nenhuma sugestão pendente nesse grupo.';
}

/* Painel: as pendentes por operação, com "aceitar todas" do grupo. */

function openReviewSheet() {
  $('reviewThreshold').value = String(reviewThreshold());
  renderReviewSheet();
  $('reviewSheet').hidden = false;
}

function closeReviewSheet() {
  $('reviewSheet').hidden = true;
}

function blockById(blockId) {
  for (const chapter of state.doc?.chapters || []) {
    const block = chapter.blocks.find((b) => b.id === blockId);
    if (block) return block;
  }
  return null;
}

function renderReviewSheet() {
  const review = state.review;
  const threshold = reviewThreshold();
  const pending = pendingSuggestions();

  const when = review.reviewed_at ? new Date(review.reviewed_at).toLocaleString('pt-BR') : '';
  $('reviewMeta').textContent = review.reviewed_at
    ? `Revisado com ${review.model || review.provider} em ${when}. Nada muda no livro sem você aceitar.`
    : 'Nenhuma revisão ainda. Use "Revisar com IA" na barra de cima.';

  const groups = $('reviewGroups');
  groups.innerHTML = '';
  if (!pending.length && review.reviewed_at) {
    const empty = document.createElement('p');
    empty.className = 'review-sheet__empty';
    empty.textContent = 'Nenhuma sugestão pendente.';
    groups.append(empty);
  }

  for (const op of OP_ORDER) {
    const items = pending.filter((s) => s.op === op);
    if (!items.length) continue;
    const eligible = items.filter((s) => !s.problem && s.confidence >= threshold);

    const group = document.createElement('section');
    group.className = 'review-group';
    const head = document.createElement('div');
    head.className = 'review-group__head';
    const name = document.createElement('b');
    name.textContent = `${OP_LABELS[op]} · ${items.length}`;
    const accept = document.createElement('button');
    accept.className = 'btn';
    accept.textContent = eligible.length ? `Aceitar ${eligible.length}` : 'Nenhuma acima do limite';
    accept.disabled = !eligible.length;
    accept.onclick = () => acceptGroup(op);
    head.append(name, accept);
    group.append(head);

    const list = document.createElement('ul');
    list.className = 'review-group__list';
    for (const suggestion of items) {
      const block = blockById(suggestion.block_id);
      const item = document.createElement('li');
      const go = document.createElement('button');
      go.className = 'review-item';
      go.title = 'Ir para o trecho';
      go.onclick = () => { closeReviewSheet(); goToBlock(suggestion.block_id); };
      const text = document.createElement('span');
      text.className = 'review-item__text';
      text.textContent = (block?.text || '(bloco não existe mais)').replace(/<[^>]+>/g, '').slice(0, 90);
      const detail = document.createElement('span');
      detail.className = 'review-item__detail';
      const where = suggestion.source === 'visual' && block?.page ? `imagem da p. ${block.page}` : '';
      detail.textContent = [percent(suggestion.confidence), where, suggestion.problem || suggestion.reason]
        .filter(Boolean).join(' · ');
      if (suggestion.problem || suggestion.confidence < threshold) item.dataset.below = 'true';
      go.append(text, detail);
      item.append(go);
      list.append(item);
    }
    group.append(list);
    groups.append(group);
  }

  const eligible = pending.filter((s) => !s.problem && s.confidence >= threshold).length;
  $('reviewAcceptAll').textContent = eligible ? `Aceitar todas (${eligible})` : 'Aceitar todas';
  $('reviewAcceptAll').disabled = !eligible;
  $('reviewRejectAll').disabled = !pending.length;

  const decided = review.suggestions.filter((s) => s.status !== 'pending');
  const count = (status) => decided.filter((s) => s.status === status).length;
  const done = [
    count('accepted') && `${count('accepted')} aceitas`,
    count('rejected') && `${count('rejected')} rejeitadas`,
    count('stale') && `${count('stale')} que o livro já não permitia`,
  ].filter(Boolean);
  $('reviewDone').textContent = done.length ? `Já decididas: ${done.join(', ')}.` : '';
}

/* ------------------------------------------------------------ salvamento */

function markDirty(block, clearing = false) {
  queueUpdate(block.id, {
    translated: clearing ? null : block.translated,
    meta: { edited: !clearing },
  });
}

// Junta os campos alterados de um bloco até o próximo salvamento.
function queueUpdate(blockId, fields) {
  const pending = state.dirty.get(blockId) || { id: blockId };
  state.dirty.set(blockId, { ...pending, ...fields });
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(flushSaves, 900);
}

async function flushSaves() {
  clearTimeout(state.saveTimer);
  if (!state.dirty.size || !state.project) return;

  const updates = [...state.dirty.values()];
  state.dirty.clear();

  try {
    const data = await api(`/api/projects/${state.project.id}/blocks`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates }),
    });
    if (data.project) { state.project = data.project; updateStat(); }
    renderRail();
    refreshPreview();
    loadSuspects();
  } catch (error) {
    toast(`Não foi possível salvar: ${error.message}`, 'error');
  }
}

let titleTimer = null;
function scheduleChapterTitleSave() {
  clearTimeout(titleTimer);
  titleTimer = setTimeout(async () => {
    renderRail();
    // O título do capítulo vive no documento; salvamos junto com um no-op de bloco.
    try {
      await api(`/api/projects/${state.project.id}/chapters`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chapters: state.doc.chapters.map((c) => ({ id: c.id, title: c.title })),
        }),
      });
      refreshPreview();
    } catch (error) {
      toast(error.message, 'error');
    }
  }, 700);
}

async function splitHere(blockId) {
  try {
    const data = await api(
      `/api/projects/${state.project.id}/chapters/${state.chapterId}/split`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ block_id: blockId }),
      }
    );
    state.doc = data.document;
    renderRail();
    renderEditor();
    loadSuspects();
    toast('Capítulo dividido');
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ------------------------------------------------- estrutura dos blocos */

// Cada operação grava antes o que está pendente (para a edição não se perder
// no redesenho) e recebe de volta o capítulo inteiro, já alterado.
async function changeStructure(path, options, after) {
  await flushSaves();
  try {
    const data = await api(`/api/projects/${state.project.id}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const index = state.doc.chapters.findIndex((c) => c.id === data.chapter.id);
    if (index >= 0) state.doc.chapters[index] = data.chapter;
    if (data.project) { state.project = data.project; updateStat(); }
    renderRail();
    renderEditor();
    refreshPreview();
    loadSuspects();
    after?.(data);
  } catch (error) {
    toast(error.message, 'error');
  }
}

function mergeWithNext(block) {
  changeStructure(`/blocks/${block.id}/merge-next`, { method: 'POST' }, (data) => {
    const note = data.block.translated ? '' : ' A tradução precisa ser refeita.';
    toast(`Parágrafos juntados.${note}`);
  });
}

function insertBelow(block) {
  const chapter = currentChapter();
  const index = chapter.blocks.findIndex((b) => b.id === block.id) + 1;
  changeStructure(
    `/chapters/${chapter.id}/blocks`,
    { method: 'POST', body: JSON.stringify({ index }) },
    (data) => {
      document.querySelector(`.block[data-id="${data.block.id}"] .block__text`)?.click();
    }
  );
}

function deleteBlock(block) {
  const chapterId = state.chapterId;
  changeStructure(`/blocks/${block.id}`, { method: 'DELETE' }, (data) => {
    toast('Bloco apagado.', 'info', {
      label: 'Desfazer',
      onClick: () => changeStructure(`/chapters/${chapterId}/blocks`, {
        method: 'POST',
        body: JSON.stringify(data.removed),
      }),
    });
  });
}

function updateStat() {
  const project = state.project;
  if (!project) return;
  const words = `${project.word_count.toLocaleString('pt-BR')} palavras`;
  if (state.stage === 'convert') {
    const chapters = state.doc.chapters.length;
    $('bookStat').textContent = `${words} · ${chapters} ${chapters === 1 ? 'capítulo' : 'capítulos'}`;
    return;
  }
  const pct = project.total_translatable
    ? Math.round((project.translated_count / project.total_translatable) * 100)
    : 0;
  const count = (key) => state.doc.chapters.reduce((sum, c) => sum + chapterProgress(c)[key], 0);
  const stale = count('stale');
  const refused = count('refused');
  $('bookStat').textContent =
    `${words} · ${pct}% traduzido` +
    (stale ? ` · ${stale} ${stale === 1 ? 'desatualizado' : 'desatualizados'}` : '') +
    (refused ? ` · ${refused} ${refused === 1 ? 'recusado' : 'recusados'}` : '');
}

/* ---------------------------------------------------------------- preview */

function refreshPreview() {
  if (!state.project || !state.chapterId) return;
  const url =
    `/api/projects/${state.project.id}/preview/${state.chapterId}` +
    `?translated=${state.stage === 'translate' && state.previewTranslated}&t=${Date.now()}`;
  $('previewFrame').src = url;
}

/* --------------------------------------------------------------- tradução */

async function startTranslation() {
  if (!state.project) return;
  // Um livro vindo da conversão ainda não disse de que idioma para qual vai.
  if (!state.project.translation?.source_language) {
    openBookTranslation(true);
    return;
  }
  await flushSaves();

  const whole = confirm(
    'Traduzir o livro inteiro?\n\n' +
    'OK traduz tudo o que ainda falta.\n' +
    'Cancelar traduz apenas o capítulo aberto.'
  );

  const body = {
    glossary: parseGlossary($('glossary').value),
    chapter_ids: whole ? [] : [state.chapterId],
  };

  try {
    const data = await api(`/api/projects/${state.project.id}/translate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!data.job) { toast(data.message || 'Nada a traduzir'); return; }

    toast(`Traduzindo com ${data.model || data.provider}`);
    state.jobId = data.job.id;
    showProgress(data.job);
    startPolling();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function parseGlossary(text) {
  const entries = {};
  for (const line of (text || '').split('\n')) {
    const [source, ...rest] = line.split('=');
    if (!rest.length) continue;
    const key = source.trim();
    const value = rest.join('=').trim();
    if (key && value) entries[key] = value;
  }
  return entries;
}

function showProgress(job) {
  $('progress').hidden = false;
  const total = job.total || 1;
  const completed = job.done + job.failed + (job.refused || 0);
  $('progressFill').style.width = `${Math.round((completed / total) * 100)}%`;

  let label = `${job.done}/${job.total}`;
  if (job.failed) label += ` · ${job.failed} falhas`;
  if (job.refused) label += ` · ${job.refused} recusados`;
  if (job.eta_seconds != null) label += ` · faltam ${formatDuration(job.eta_seconds)}`;
  // A pausa pelo limite do plano aparece no lugar da estimativa.
  if (job.note?.startsWith('Limite do plano')) label = `${job.done}/${job.total} · ${job.note}`;
  $('progressLabel').textContent = label;
}

function formatDuration(seconds) {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

function startPolling() {
  stopPolling();
  state.jobTimer = setInterval(pollJob, 1500);
}

function stopPolling() {
  if (state.jobTimer) clearInterval(state.jobTimer);
  state.jobTimer = null;
}

async function pollJob() {
  if (!state.jobId) return stopPolling();

  try {
    const { job } = await api(`/api/jobs/${state.jobId}`);
    showProgress(job);

    if (job.status === 'running') return;

    stopPolling();
    state.jobId = null;
    setTimeout(() => { $('progress').hidden = true; }, 2500);

    if (job.status === 'error') {
      toast(`A tradução parou: ${job.error}`, 'error');
    } else if (job.status === 'stopped') {
      toast('Tradução interrompida. O que já foi feito está salvo.');
    } else {
      const note =
        (job.failed ? ` ${job.failed} parágrafos falharam.` : '') +
        (job.refused ? ` O modelo se recusou a traduzir ${job.refused}: estão marcados no texto.` : '');
      toast(`Tradução concluída.${note}`);
    }

    // Recarrega para trazer o texto traduzido. Antes, grava o que ainda está
    // esperando o salvamento automático, senão a recarga mostra o texto velho.
    await flushSaves();
    const data = await api(`/api/projects/${state.project.id}`);
    state.project = data.project;
    state.doc = data.document;
    renderRail();
    renderEditor();
    refreshPreview();
    updateStat();
  } catch (error) {
    stopPolling();
    toast(error.message, 'error');
  }
}

async function watchExistingJob(projectId) {
  try {
    const { job } = await api(`/api/projects/${projectId}/job`);
    if (job && job.status === 'running') {
      state.jobId = job.id;
      showProgress(job);
      startPolling();
    }
  } catch { /* sem tarefa ativa, segue normalmente */ }
}

/* ------------------------------------------------- tradução deste livro */

// Idiomas e provedor de cada livro. Valem antes das preferências gerais; sem
// eles, o primeiro "Traduzir" abre este painel.

function bookProvider() {
  const providers = state.settings?.providers || [];
  const own = state.project.translation?.provider_id;
  return {
    own: providers.find((p) => p.id === own) || null,
    active: providers.find((p) => p.id === state.settings?.active_provider_id) || providers[0] || null,
  };
}

function bookTranslationLine() {
  const own = state.project.translation || {};
  const link = document.createElement('button');
  link.className = 'editor__link';
  link.onclick = () => openBookTranslation(false);
  if (!own.source_language) {
    link.textContent = 'Escolher idiomas e provedor';
    return ['Este livro ainda não tem idiomas escolhidos. ', link];
  }
  const { own: chosen, active } = bookProvider();
  const provider = chosen
    ? `com ${providerLabel(chosen)}`
    : `com o provedor ativo${active ? ` (${providerLabel(active)})` : ''}`;
  link.textContent = 'Trocar';
  return [`Do ${own.source_language} para o ${own.target_language}, ${provider}. `, link];
}

async function openBookTranslation(thenTranslate) {
  try {
    state.importOptions ??= await api('/api/imports/options');
    if (!state.settings) await loadSettings();
  } catch (error) {
    toast(error.message, 'error');
    return;
  }
  const languages = state.importOptions.languages;
  const own = state.project.translation || {};
  const settings = state.settings || {};
  // O que o livro escolheu; senão as preferências gerais; senão o que o
  // arquivo dizia.
  const known = (code) => (languages.some((l) => l.code === code) ? code : null);
  fillLanguageSelect($('bookSource'), languages,
    languageCode(languages, own.source_language) || languageCode(languages, settings.source_language)
    || known(state.doc.source_language) || 'en');
  fillLanguageSelect($('bookTarget'), languages,
    languageCode(languages, own.target_language) || languageCode(languages, settings.target_language)
    || known(state.doc.language) || 'pt-BR');

  const select = $('bookProvider');
  select.innerHTML = '';
  const { active } = bookProvider();
  select.append(new Option(
    `O ativo nas configurações${active ? ` (${providerLabel(active)})` : ': nenhum ainda'}`, ''
  ));
  for (const provider of settings.providers || []) {
    select.append(new Option(providerLabel(provider), provider.id));
  }
  select.value = bookProvider().own?.id || '';

  $('bookTranslationNote').textContent = thenTranslate
    ? 'Antes da primeira tradução, confirme de que idioma para qual este livro vai.'
    : 'Vale para as próximas traduções deste livro. O que já foi traduzido fica como está.';
  $('saveAndTranslate').hidden = !thenTranslate;
  $('saveBookTranslation').className = thenTranslate ? 'btn' : 'btn btn--primary';
  $('bookTranslationSheet').hidden = false;
  (thenTranslate ? $('saveAndTranslate') : $('bookSource')).focus();
}

function closeBookTranslation() {
  $('bookTranslationSheet').hidden = true;
}

async function saveBookTranslation(thenTranslate) {
  const body = {
    source_language: $('bookSource').value,
    target_language: $('bookTarget').value,
    provider_id: $('bookProvider').value,
  };
  try {
    const data = await api(`/api/projects/${state.project.id}/translation`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.project = data.project;
    state.doc.source_language = body.source_language;
    state.doc.language = body.target_language;
    closeBookTranslation();
    renderEditor();
    if (thenTranslate) startTranslation();
    else toast('Tradução deste livro salva');
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ----------------------------------------------------------- configurações */

async function loadSettings() {
  try {
    const [settingsData, catalogData] = await Promise.all([
      api('/api/settings'),
      api('/api/providers/catalog'),
    ]);
    state.settings = settingsData.settings;
    state.catalog = catalogData.catalog;
    renderSettings();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function renderSettings() {
  const settings = state.settings;

  const kindSelect = $('pKind');
  if (!kindSelect.options.length) {
    for (const entry of state.catalog) {
      const option = document.createElement('option');
      option.value = entry.kind;
      option.textContent = entry.label;
      kindSelect.append(option);
    }
    kindSelect.onchange = () => {
      // O modelo de um serviço não vale no outro.
      $('pModel').value = '';
      applyCatalogDefaults();
    };
    applyCatalogDefaults();
  }

  $('srcLang').value = settings.source_language;
  $('tgtLang').value = settings.target_language;
  $('styleNotes').value = settings.style_notes;
  $('batchChars').value = settings.batch_chars;
  $('concurrency').value = settings.concurrency;
  $('glossary').value = glossaryToText(settings.glossary || {});

  const list = $('providerList');
  list.innerHTML = '';

  if (!settings.providers.length) {
    list.innerHTML =
      '<p class="empty">Nenhum provedor configurado. Adicione um abaixo para poder traduzir.</p>';
    return;
  }

  for (const provider of settings.providers) {
    const card = document.createElement('div');
    card.className = 'provider-card';
    card.dataset.active = provider.id === settings.active_provider_id;

    const body = document.createElement('div');
    body.className = 'provider-card__body';
    body.innerHTML =
      `<div class="provider-card__name">${escapeHtml(provider.label)}</div>` +
      `<div class="provider-card__meta">${escapeHtml(provider.model || 'sem modelo')}` +
      `${provider.key_hint ? ` · chave ${escapeHtml(provider.key_hint)}` : ''}</div>`;

    // Quem traduz pela assinatura não tem chave para mostrar: mostra se o
    // Claude Code está conectado, que é o que decide se a tradução vai rodar.
    if (provider.kind === 'claude_cli') {
      const badge = document.createElement('span');
      badge.className = 'dot-badge';
      badge.dataset.tone = 'wait';
      badge.dataset.claudeBadge = '';
      badge.textContent = 'verificando…';
      body.querySelector('.provider-card__meta').append(' · ', badge);
    }

    const use = document.createElement('button');
    use.className = 'btn btn--quiet';
    use.textContent = provider.id === settings.active_provider_id ? 'Em uso' : 'Usar';
    use.disabled = provider.id === settings.active_provider_id;
    use.onclick = async () => {
      await api('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active_provider_id: provider.id }),
      });
      loadSettings();
    };

    const edit = document.createElement('button');
    edit.className = 'btn btn--quiet';
    edit.textContent = 'Editar';
    edit.onclick = () => fillProviderForm(provider);

    const remove = document.createElement('button');
    remove.className = 'btn btn--danger';
    remove.textContent = 'Remover';
    remove.onclick = async () => {
      await api(`/api/providers/${provider.id}`, { method: 'DELETE' });
      loadSettings();
    };

    card.append(body, use, edit, remove);
    list.append(card);
  }

  paintClaudeBadges();
}

function applyCatalogDefaults() {
  const entry = state.catalog.find((c) => c.kind === $('pKind').value);
  if (!entry) return;
  $('pBase').value = entry.default_base_url;
  $('pHint').textContent = entry.hint;
  $('pHint').dataset.experimental = Boolean(entry.experimental);
  $('keyField').style.display = entry.needs_key ? '' : 'none';
  if (!entry.needs_key) $('pKey').value = '';
  // Claude pela assinatura: não há endereço nem chave, só o modelo.
  $('baseField').hidden = entry.needs_base_url === false;
  if (entry.default_model && !$('pModel').value) $('pModel').value = entry.default_model;
  if (!$('pLabel').value || state.editingProviderId === null) {
    $('pLabel').value = entry.label;
  }
  refreshModels();
}

/* Claude pela assinatura. O login é do Claude Code, pelo fluxo da Anthropic:
   o Interlinear não pode intermediar o link nem o código, e nunca vê o token.
   O usuário entra pelo container (aba Exec do Docker Desktop, ou docker exec),
   e a página só lê o `claude auth status` até a conta aparecer. */

const CLAUDE_LOGIN = 'claude auth login';

// Cada `claude auth status` roda um processo. O painel e a lista de provedores
// perguntam juntos, então o resultado vale por alguns segundos.
async function claudeStatus(fresh = false) {
  if (!fresh && state.claude && Date.now() - state.claude.at < 5000) {
    return state.claude.value;
  }
  let value;
  try {
    ({ claude: value } = await api('/api/claude/status'));
  } catch (error) {
    value = { installed: true, error: error.message };
  }
  state.claude = { at: Date.now(), value };
  return value;
}

function stopClaudePolling() {
  clearInterval(state.claudeTimer);
  state.claudeTimer = null;
}

function startClaudePolling() {
  if (state.claudeTimer) return;
  state.claudeTimer = setInterval(() => {
    if ($('settingsPage').hidden) stopClaudePolling();
    else checkClaude();
  }, 3000);
}

async function checkClaude() {
  const claude = await claudeStatus(true);
  renderClaudePanel(claude);

  if (claude.logged_in && claude.subscription) {
    // Só avisa se a página estava esperando: no primeiro desenho, não.
    if (state.claudeTimer) toast(`Claude conectado${planText(claude.plan)}.`);
    stopClaudePolling();
    paintClaudeBadges();
  } else if (claude.installed) {
    startClaudePolling();
  }
}

function planText(plan) {
  return plan ? `, plano ${plan[0].toUpperCase()}${plan.slice(1)}` : '';
}

function loginExpiry(value) {
  if (value == null || value === '') return null;
  // Época em segundos ou milissegundos, ou data em texto.
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? null : date;
}

// "Conectado ou não" para a bolinha da lista de provedores. Curto de
// propósito: fica na mesma linha do modelo, e o painel logo abaixo é que
// conta o resto (plano, validade, o que fazer).
function claudeLabel(claude) {
  if (!claude) return { tone: 'wait', text: 'verificando…' };
  if (claude.logged_in && claude.subscription) return { tone: 'ok', text: 'conectado' };
  if (!claude.installed) return { tone: 'todo', text: 'sem o Claude Code' };
  if (claude.error) return { tone: 'todo', text: 'não deu para verificar' };
  if (claude.logged_in) return { tone: 'todo', text: 'usando chave de API' };
  return { tone: 'todo', text: 'não conectado' };
}

// As bolinhas da lista de provedores. A lista é redesenhada do zero a cada
// mudança nas configurações, então cada desenho repinta as suas.
async function paintClaudeBadges() {
  const slots = document.querySelectorAll('[data-claude-badge]');
  if (!slots.length) return;
  const label = claudeLabel(await claudeStatus());
  for (const slot of slots) {
    slot.dataset.tone = label.tone;
    slot.textContent = label.text;
  }
}

function renderClaudePanel(claude) {
  const box = $('claudeConnect');
  box.innerHTML = '';
  const line = (text, className = 'field__hint') => {
    const p = document.createElement('p');
    p.className = className;
    p.textContent = text;
    box.append(p);
    return p;
  };
  // A bolinha é um elemento, não um ✓ no CSS: ela também pisca enquanto
  // esperamos o login terminar.
  const status = (text) => {
    const p = document.createElement('p');
    p.className = 'connect__status';
    const dot = document.createElement('span');
    dot.className = 'connect__dot';
    p.append(dot, text);
    box.append(p);
  };

  // Corpo à parte para o rodapé valer em todos os caminhos.
  const body = () => {
    if (claude.logged_in && claude.subscription) {
      box.dataset.state = 'ok';
      status(`Conectado ao Claude${planText(claude.plan)}.`);
      const expires = loginExpiry(claude.expires_at);
      if (expires) {
        const soon = expires - Date.now() < 7 * 24 * 3600 * 1000;
        line(
          `O login vale até ${expires.toLocaleDateString('pt-BR')}` +
            (soon ? '. Expira em breve: entre de novo do mesmo jeito para renovar.' : '.')
        );
      }
      line(
        'O login fica no volume interlinear-claude e sobrevive a rebuilds. ' +
          'Só o docker compose down -v apaga.'
      );
      return;
    }

    box.dataset.state = 'todo';
    if (!claude.installed) {
      status('Esta imagem do Interlinear foi construída sem o Claude Code.');
      line(
        'Ponha INTERLINEAR_WITH_CLAUDE=1 no arquivo .env ao lado do compose.yaml e ' +
          'reconstrua: docker compose up -d --build.'
      );
      return;
    }
    if (claude.error) {
      status('Não foi possível falar com o Claude Code.');
      line(claude.error);
      return;
    }

    // Logado, mas por chave de API: dá para entrar, só precisa sair antes, e o
    // comando dos passos já faz isso.
    const apiKey = Boolean(claude.logged_in);
    status(
      apiKey
        ? 'O Claude Code está usando uma chave de API, não a assinatura.'
        : 'Não conectado: falta entrar com a sua conta Claude.'
    );
    line(
      apiKey
        ? 'Para usar a chave, cadastre o provedor Anthropic. Para usar a assinatura, ' +
            'entre com a conta Claude.'
        : 'É uma vez só, e vale para todos os livros. O login é feito pelo container:',
      apiKey ? 'field__hint' : 'connect__notice'
    );
    box.append(claudeSteps(claude));
    line(`Ou, num terminal: docker exec -it interlinear ${CLAUDE_LOGIN}`);
    line('Esta página confere sozinha e avisa quando você entrar.', 'field__hint connect__waiting');
  };

  body();

  // Rodapé de monitoração: de quando é o que está na tela.
  const read = new Date(state.claude ? state.claude.at : Date.now());
  line(`Lido do Claude Code às ${read.toLocaleTimeString('pt-BR')}.`, 'connect__stamp');
}

// Os passos para entrar pelo container.
function claudeSteps(claude) {
  const command = claude.logged_in ? `claude auth logout && ${CLAUDE_LOGIN}` : CLAUDE_LOGIN;
  const steps = document.createElement('ol');
  steps.className = 'connect__steps';
  const step = (...nodes) => {
    const li = document.createElement('li');
    li.append(...nodes);
    steps.append(li);
  };

  step('No Docker Desktop, abra Containers → interlinear → aba Exec.');
  const code = document.createElement('code');
  code.textContent = command;
  const copy = document.createElement('button');
  copy.className = 'btn btn--quiet connect__copy';
  copy.textContent = 'Copiar';
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(command);
      toast('Comando copiado');
    } catch {
      toast(`Copie o comando: ${command}`);
    }
  };
  step('Rode ', code, ' ', copy);
  step(
    'Escolha a conta Claude, autorize na página da Anthropic e cole o código que ela ' +
      'mostrar de volta no Exec.'
  );
  return steps;
}

function fillProviderForm(provider) {
  state.editingProviderId = provider.id;
  $('pKind').value = provider.kind;
  $('pLabel').value = provider.label;
  $('pBase').value = provider.base_url;
  $('pModel').value = provider.model;
  $('pKey').value = '';
  $('pKey').placeholder = provider.has_key ? 'chave guardada — deixe vazio para manter' : 'cole aqui';
  const entry = state.catalog.find((c) => c.kind === provider.kind);
  $('keyField').style.display = entry?.needs_key === false ? 'none' : '';
  $('baseField').hidden = entry?.needs_base_url === false;
  $('pHint').textContent = entry?.hint || '';
  $('pHint').dataset.experimental = Boolean(entry?.experimental);
  state.providerHasKey = provider.has_key;
  refreshModels();
}

async function saveProvider() {
  const payload = {
    id: state.editingProviderId || undefined,
    kind: $('pKind').value,
    label: $('pLabel').value.trim() || $('pKind').value,
    base_url: $('pBase').value.trim(),
    model: $('pModel').value.trim(),
    api_key: $('pKey').value.trim(),
  };

  try {
    const data = await api('/api/providers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.editingProviderId = data.provider.id;
    state.providerHasKey = data.provider.has_key;
    $('pKey').value = '';
    toast('Provedor salvo');
    loadSettings();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function testProvider() {
  if (!state.editingProviderId) {
    toast('Salve o provedor antes de testar', 'error');
    return;
  }
  $('testProvider').disabled = true;
  try {
    const data = await api(`/api/providers/${state.editingProviderId}/test`, { method: 'POST' });
    if (data.ok) {
      const plan = data.plan ? ` pelo plano ${data.plan[0].toUpperCase()}${data.plan.slice(1)}` : '';
      toast(`Conectado${plan}. O modelo respondeu: ${data.reply}`);
    }
    else toast(data.error, 'error');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    $('testProvider').disabled = false;
  }
}

/* Modelo: uma lista com busca, carregada do próprio serviço com o que está no
   formulário, sem precisar salvar antes. Digitar um nome fora da lista
   continua valendo (modelo recém-lançado, endpoint que não lista). */

const MODEL_PICKER_MAX = 60;

function currentEntry() {
  return state.catalog.find((c) => c.kind === $('pKind').value);
}

// Carrega sozinho quando dá: serviço sem chave, chave digitada ou guardada.
function refreshModels() {
  state.models = [];
  renderModelField();
  const entry = currentEntry();
  if (!entry) return;
  const hasKey = $('pKey').value.trim() || (state.editingProviderId && state.providerHasKey);
  if (!entry.needs_key || hasKey) loadModels({ quiet: true });
}

async function loadModels({ quiet = false } = {}) {
  const entry = currentEntry();
  if (!entry) return;
  const kind = entry.kind;
  // Se a lista está aberta esperando, ela precisa voltar depois da busca:
  // renderModelField() fecha, e era isso que a fazia piscar e sumir.
  const wasOpen = !$('modelPicker').hidden;
  state.modelsLoading = true;
  state.modelsError = '';
  $('modelHint').textContent = 'Buscando os modelos…';
  if (wasOpen) renderModelPicker();
  try {
    const data = await api('/api/providers/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind,
        base_url: $('pBase').value.trim(),
        api_key: $('pKey').value.trim(),
        provider_id: state.editingProviderId || undefined,
      }),
    });
    if ($('pKind').value !== kind) return; // trocou de serviço no meio
    state.modelsLoading = false;
    state.models = data.models;
    renderModelField();
    // Reabre só se o campo ainda está com o usuário: ele pode ter clicado
    // fora enquanto a busca corria.
    const reopen = wasOpen && document.activeElement === $('pModel');
    if (!entry.fixed_models && (!quiet || reopen)) {
      if (!quiet) $('pModel').focus();
      openModelPicker();
    }
  } catch (error) {
    if ($('pKind').value !== kind) return;
    state.modelsLoading = false;
    state.models = [];
    state.modelsError = error.message;
    renderModelField();
    $('modelHint').textContent = `Não foi possível listar os modelos: ${error.message}`;
    if (!quiet) toast(error.message, 'error');
    // Com a lista aberta, o motivo aparece nela em vez de ela sumir.
    if (!entry.fixed_models && wasOpen) renderModelPicker();
  } finally {
    state.modelsLoading = false;
  }
}

function renderModelField() {
  const fixed = Boolean(currentEntry()?.fixed_models);
  $('modelRow').hidden = fixed;
  $('modelChoices').hidden = !fixed;
  closeModelPicker();
  if (fixed) renderModelChoices();
  describeModel();
}

function renderModelChoices() {
  const box = $('modelChoices');
  box.innerHTML = '';
  for (const model of state.models) {
    const choice = document.createElement('button');
    choice.type = 'button';
    choice.className = 'model-choice';
    choice.setAttribute('role', 'radio');
    choice.setAttribute('aria-checked', model.id === $('pModel').value);
    const name = document.createElement('strong');
    name.textContent = model.name || model.id;
    const note = document.createElement('span');
    note.textContent = model.note;
    choice.append(name, note);
    choice.onclick = () => {
      $('pModel').value = model.id;
      renderModelChoices();
      describeModel();
    };
    box.append(choice);
  }
}

function modelDate(model) {
  if (!model.created) return '';
  return new Date(model.created * 1000).toLocaleDateString('pt-BR', { month: 'short', year: 'numeric' });
}

// A linha embaixo do campo: o que é o modelo escolhido, ou o tamanho da lista.
function describeModel() {
  if (currentEntry()?.fixed_models) {
    $('modelHint').textContent = '';
    return;
  }
  const value = $('pModel').value.trim();
  const chosen = state.models.find((m) => m.id === value);
  if (chosen) {
    $('modelHint').textContent = [chosen.name, modelDate(chosen), chosen.note]
      .filter(Boolean).join(' · ');
  } else if (state.models.length) {
    const dated = state.models.some((m) => m.created);
    $('modelHint').textContent =
      `${state.models.length} ${state.models.length === 1 ? 'modelo' : 'modelos'}` +
      (dated ? ', os mais novos primeiro.' : '.') +
      (value ? ' Este nome não está na lista; vale se o serviço aceitar.' : '');
  } else {
    $('modelHint').textContent = '';
  }
}

function modelMatches() {
  const value = $('pModel').value.trim();
  const query = value.toLowerCase();
  // Com um nome da lista já escolhido, mostra tudo para dar para trocar.
  if (!query || state.models.some((m) => m.id === value)) return state.models;
  return state.models.filter((m) => `${m.id} ${m.name}`.toLowerCase().includes(query));
}

function openModelPicker() {
  if (currentEntry()?.fixed_models) return;
  state.pickerIndex = -1;
  // Abre primeiro, busca depois: assim a lista já aparece dizendo "Buscando
  // os modelos…", e a busca sabe que tem uma lista aberta para repintar.
  // Antes a seta não fazia nada enquanto não houvesse modelos — e parecia
  // quebrada justamente quando era mais necessária.
  renderModelPicker();
  if (!state.models.length && !state.modelsLoading) loadModels({ quiet: true });
}

function closeModelPicker() {
  $('modelPicker').hidden = true;
  $('modelRow').dataset.open = 'false';
  $('pModel').setAttribute('aria-expanded', 'false');
  $('pModel').removeAttribute('aria-activedescendant');
}

// Por que a lista está vazia: é o que a lista mostra no lugar dos modelos.
function modelsEmptyNote() {
  if (state.modelsLoading) return 'Buscando os modelos…';
  if (state.modelsError) return `Não foi possível listar: ${state.modelsError}`;
  const entry = currentEntry();
  const hasKey = $('pKey').value.trim() || (state.editingProviderId && state.providerHasKey);
  if (entry?.needs_key && !hasKey) return 'Digite a chave de API para listar os modelos.';
  return 'Nenhum modelo para listar. Dá para digitar o nome, se o serviço aceitar.';
}

function renderModelPicker() {
  const picker = $('modelPicker');
  const matches = modelMatches();
  picker.innerHTML = '';

  matches.slice(0, MODEL_PICKER_MAX).forEach((model, index) => {
    const option = document.createElement('div');
    option.className = 'model-option';
    option.id = `model-option-${index}`;
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', index === state.pickerIndex);
    const title = document.createElement('span');
    title.className = 'model-option__name';
    title.textContent = model.name && model.name !== model.id ? model.name : model.id;
    const meta = document.createElement('span');
    meta.className = 'model-option__meta';
    meta.textContent = [model.name && model.name !== model.id ? model.id : '', modelDate(model), model.note]
      .filter(Boolean).join(' · ');
    option.append(title, meta);
    // mousedown, não click: o campo perderia o foco e fecharia a lista antes.
    option.onmousedown = (event) => {
      event.preventDefault();
      chooseModel(model.id);
    };
    picker.append(option);
  });

  const footer = (text) => {
    const p = document.createElement('p');
    p.className = 'model-picker__note';
    p.textContent = text;
    picker.append(p);
  };
  if (!state.models.length) footer(modelsEmptyNote());
  else if (!matches.length) footer('Nenhum modelo com esse nome. Pode usar assim mesmo, se o serviço aceitar.');
  else if (matches.length > MODEL_PICKER_MAX) {
    footer(`E mais ${matches.length - MODEL_PICKER_MAX}: digite para filtrar.`);
  }

  picker.hidden = false;
  $('modelRow').dataset.open = 'true';
  $('pModel').setAttribute('aria-expanded', 'true');
  if (state.pickerIndex >= 0) {
    $('pModel').setAttribute('aria-activedescendant', `model-option-${state.pickerIndex}`);
    picker.children[state.pickerIndex]?.scrollIntoView({ block: 'nearest' });
  }
}

function chooseModel(id) {
  $('pModel').value = id;
  closeModelPicker();
  describeModel();
}

function bindModelField() {
  const field = $('pModel');
  field.onfocus = openModelPicker;
  field.onblur = closeModelPicker;
  // mousedown, não click: o click viria depois do blur, que fecha a lista.
  $('modelCaret').onmousedown = (event) => {
    event.preventDefault();
    if ($('modelPicker').hidden) {
      field.focus();
      openModelPicker();
    } else {
      closeModelPicker();
    }
  };
  field.oninput = () => {
    state.pickerIndex = -1;
    if (state.models.length) renderModelPicker();
    describeModel();
  };
  field.onkeydown = (event) => {
    const picker = $('modelPicker');
    const count = Math.min(modelMatches().length, MODEL_PICKER_MAX);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (picker.hidden) return openModelPicker();
      if (!count) return;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      state.pickerIndex = (state.pickerIndex + step + count) % Math.max(count, 1);
      renderModelPicker();
    } else if (event.key === 'Enter' && !picker.hidden && state.pickerIndex >= 0) {
      event.preventDefault();
      chooseModel(modelMatches()[state.pickerIndex].id);
    } else if (event.key === 'Escape' && !picker.hidden) {
      event.preventDefault();
      event.stopPropagation(); // fecha a lista, não as configurações
      closeModelPicker();
    }
  };
  // Chave ou endereço novos: a lista pode mudar.
  $('pKey').onchange = refreshModels;
  $('pBase').onchange = refreshModels;
  $('loadModels').onclick = () => loadModels();
}

async function saveGeneralSettings() {
  try {
    await api('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_language: $('srcLang').value,
        target_language: $('tgtLang').value,
        style_notes: $('styleNotes').value,
        batch_chars: Number($('batchChars').value),
        concurrency: Number($('concurrency').value),
        glossary: parseGlossary($('glossary').value),
      }),
    });
    toast('Preferências salvas');
    loadSettings();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function glossaryToText(glossary) {
  return Object.entries(glossary).map(([k, v]) => `${k} = ${v}`).join('\n');
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text ?? '';
  return div.innerHTML;
}

/* --------------------------------------------------------------- ligações */

function wire() {
  const inputs = { convert: $('fileConvert'), translate: $('fileTranslate') };
  for (const drop of document.querySelectorAll('.drop[data-purpose]')) {
    const purpose = drop.dataset.purpose;
    drop.querySelector('[data-pick]').onclick = () => inputs[purpose].click();
    inputs[purpose].onchange = (event) => importFile(event.target.files[0], purpose);
    drop.ondragover = (event) => { event.preventDefault(); drop.dataset.over = 'true'; };
    drop.ondragleave = () => { drop.dataset.over = 'false'; };
    drop.ondrop = (event) => {
      event.preventDefault();
      drop.dataset.over = 'false';
      importFile(event.dataTransfer.files[0], purpose);
    };
  }
  $('importSource').onchange = syncOcrWithSource;

  $('startImport').onclick = startImport;
  $('closeBookTranslation').onclick = closeBookTranslation;
  $('bookTranslationSheet').onclick = (event) => {
    if (event.target === $('bookTranslationSheet')) closeBookTranslation();
  };
  $('saveBookTranslation').onclick = () => saveBookTranslation(false);
  $('saveAndTranslate').onclick = () => saveBookTranslation(true);
  $('importOcr').onchange = syncOcrFields;
  const closeImport = () => { state.pendingFile = null; $('importSheet').hidden = true; };
  $('closeImport').onclick = closeImport;
  $('importSheet').onclick = (event) => { if (event.target === $('importSheet')) closeImport(); };
  const closeSummary = () => { $('summarySheet').hidden = true; };
  $('closeSummary').onclick = closeSummary;
  $('summaryDone').onclick = () => {
    closeSummary();
    setStage(projectPurpose(state.project));
  };
  $('summarySheet').onclick = (event) => { if (event.target === $('summarySheet')) closeSummary(); };

  $('homeBtn').onclick = goHome;
  $('translateBtn').onclick = startTranslation;
  $('reviewBtn').onclick = (event) => {
    event.stopPropagation();
    if ($('reviewMenu').hidden) openReviewMenu();
    else closeReviewMenu();
  };
  $('reviewText').onclick = () => startReview({});
  $('reviewPages').onclick = () => startReview({ mode: 'visual' });
  $('reviewThisPage').onclick = () => {
    const page = Number($('reviewThisPage').dataset.page);
    if (page) startReview({ mode: 'visual', pages: [page] });
  };
  $('reviewStop').onclick = stopReview;
  $('closeReview').onclick = closeReviewSheet;
  $('reviewSheet').onclick = (event) => { if (event.target === $('reviewSheet')) closeReviewSheet(); };
  $('reviewThreshold').onchange = () => {
    state.reviewThreshold = Number($('reviewThreshold').value);
    try { localStorage.setItem(THRESHOLD_KEY, $('reviewThreshold').value); } catch { /* fica só agora */ }
    renderReviewSheet();
  };
  $('reviewAcceptAll').onclick = () => acceptGroup(null);
  $('reviewRejectAll').onclick = () => {
    const ids = pendingSuggestions().map((s) => s.id);
    if (ids.length && confirm(`Rejeitar as ${ids.length} sugestões pendentes? Elas não voltam numa nova revisão.`)) {
      decideSuggestions(ids, false);
    }
  };
  $('stageConvert').onclick = () => setStage('convert');
  $('stageTranslate').onclick = () => setStage('translate');

  $('exportBtn').onclick = (event) => {
    event.stopPropagation();
    if ($('exportMenu').hidden) openExportMenu();
    else closeExportMenu();
  };
  $('exportOriginal').onclick = () => exportEpub(false);
  $('exportTranslated').onclick = () => exportEpub(true);
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.menu-anchor')) {
      closeExportMenu();
      closeReviewMenu();
    }
  });

  $('stopBtn').onclick = async () => {
    if (!state.jobId) return;
    await api(`/api/jobs/${state.jobId}/stop`, { method: 'POST' });
    toast('Parando depois dos lotes em andamento…');
  };

  $('bookTitle').onchange = async () => {
    try {
      const data = await api(`/api/projects/${state.project.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: $('bookTitle').value }),
      });
      state.project = data.project;
      toast('Título atualizado');
    } catch (error) {
      toast(error.message, 'error');
    }
  };

  $('settingsBtn').onclick = openSettings;
  $('closeSettings').onclick = closeSettings;
  window.addEventListener('hashchange', applyRoute);

  $('claudeRecheck').onclick = async () => {
    await checkClaude();
    toast('Estado do Claude atualizado.');
  };

  $('saveProvider').onclick = saveProvider;
  $('testProvider').onclick = testProvider;
  bindModelField();
  $('saveSettings').onclick = saveGeneralSettings;

  $('viewTranslated').onclick = () => setPreviewMode(true);
  $('viewOriginal').onclick = () => setPreviewMode(false);
  $('refreshPreview').onclick = refreshPreview;

  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 's') {
      event.preventDefault();
      flushSaves();
      toast('Salvo');
    }
    if (event.key === 'Escape') {
      if (!$('settingsPage').hidden) closeSettings();
      closeImport();
      closeSummary();
      closeExportMenu();
      closeBookTranslation();
    }
  });

  window.addEventListener('beforeunload', flushSaves);
}

function openExportMenu() {
  const translated = state.project?.translated_count || 0;
  $('exportTranslated').disabled = !translated;
  $('exportTranslatedHint').textContent = translated
    ? 'O que não foi traduzido sai no original'
    : 'Nada traduzido ainda';
  closeReviewMenu();
  $('exportMenu').hidden = false;
  $('exportBtn').setAttribute('aria-expanded', 'true');
  // Na etapa de tradução, o mais provável é querer o traduzido.
  (state.stage === 'translate' && translated ? $('exportTranslated') : $('exportOriginal')).focus();
}

function closeExportMenu() {
  $('exportMenu').hidden = true;
  $('exportBtn').setAttribute('aria-expanded', 'false');
}

async function exportEpub(translated) {
  closeExportMenu();
  await flushSaves();
  window.location = `/api/projects/${state.project.id}/export?translated=${translated}`;
}

function setPreviewMode(translated) {
  state.previewTranslated = translated;
  $('viewTranslated').setAttribute('aria-pressed', translated);
  $('viewOriginal').setAttribute('aria-pressed', !translated);
  refreshPreview();
}

wire();
applyRoute();
loadProjects();
loadSettings();
resumeImports();
