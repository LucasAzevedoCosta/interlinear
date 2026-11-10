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
  jobId: null,
  jobTimer: null,
  previewTranslated: true,
  dirty: new Map(),
  saveTimer: null,
  // Importações em andamento, pelo id da tarefa (ou um id local enquanto o
  // arquivo ainda sobe).
  imports: new Map(),
  importTimer: null,
  importOptions: null,
  pendingFile: null,
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

    const name = document.createElement('button');
    name.className = 'project__name';
    name.textContent = project.title;
    name.onclick = () => openProject(project.id);

    const meta = document.createElement('span');
    meta.className = 'project__meta';
    const pct = project.total_translatable
      ? Math.round((project.translated_count / project.total_translatable) * 100)
      : 0;
    meta.textContent =
      `${project.word_count.toLocaleString('pt-BR')} palavras · ${pct}% traduzido`;

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

async function importFile(file) {
  if (!file) return;
  $('fileInput').value = '';  // escolher o mesmo arquivo de novo dispara onchange
  const kind = file.name.toLowerCase().match(/\.(pdf|epub)$/)?.[1];
  if (!kind) { toast('Envie um arquivo .pdf ou .epub', 'error'); return; }

  state.pendingFile = file;
  $('importHeading').textContent = kind === 'pdf' ? 'Importar PDF' : 'Importar EPUB';
  $('importFileName').textContent =
    `${file.name} · ${(file.size / 1024 / 1024).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} MB`;
  $('importTitle').value = '';
  $('importTitle').placeholder = kind === 'pdf' ? 'detectar pela folha de rosto' : 'o que está no EPUB';
  $('importTitleHint').textContent = 'Deixe em branco para usar o título do arquivo. Dá para mudar depois.';
  for (const element of document.querySelectorAll('.pdf-only')) element.hidden = kind !== 'pdf';
  $('importSheet').hidden = false;
  $('startImport').focus();
  if (kind === 'pdf') await loadImportOptions();
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
      'O README explica como instalar (a imagem Docker já traz).';

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
  if (!file) return;
  state.pendingFile = null;
  $('importSheet').hidden = true;

  const form = new FormData();
  form.append('file', file);
  form.append('title', $('importTitle').value.trim());
  if (file.name.toLowerCase().endsWith('.pdf')) {
    form.append('split_on', $('importSplit').value);
    form.append('ocr_mode', $('importOcr').value);
    const langs = [...$('importLangs').querySelectorAll('input:checked')].map((box) => box.value);
    if (langs.length) form.append('ocr_languages', langs.join('+'));
  }

  // Enquanto o arquivo sobe, a importação ainda não tem id no servidor.
  const local = { id: `envio-${Date.now()}`, filename: file.name, status: 'running', stage: 'upload', done: 0, total: 100 };
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

function renderImports() {
  const list = $('importList');
  list.innerHTML = '';
  for (const job of state.imports.values()) {
    const row = document.createElement('div');
    row.className = 'import';

    const name = document.createElement('span');
    name.className = 'import__name';
    name.textContent = job.filename;

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

    $('start').hidden = true;
    for (const id of ['rail', 'editor', 'preview']) $(id).hidden = false;
    for (const id of ['homeBtn', 'translateBtn', 'exportBtn', 'bookBar']) $(id).hidden = false;

    $('bookTitle').value = data.document.title;
    renderRail();
    renderEditor();
    refreshPreview();
    updateStat();
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
  $('start').hidden = false;
  for (const id of ['rail', 'editor', 'preview']) $(id).hidden = true;
  for (const id of ['homeBtn', 'translateBtn', 'exportBtn', 'bookBar', 'progress']) $(id).hidden = true;
  loadProjects();
}

function chapterProgress(chapter) {
  const translatable = chapter.blocks.filter(
    (b) => b.text.trim() && b.kind !== 'image' && b.kind !== 'page_break'
  );
  const done = translatable.filter((b) => b.translated && b.translated.trim());
  return { done: done.length, total: translatable.length };
}

function renderRail() {
  const list = $('chapterList');
  list.innerHTML = '';
  $('chapterCount').textContent = state.doc.chapters.length;

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
    meta.textContent = total ? `${done}/${total} parágrafos` : 'sem texto';

    const gauge = document.createElement('span');
    gauge.className = 'chapter__gauge';
    const fill = document.createElement('i');
    fill.style.width = `${pct}%`;
    gauge.append(fill);

    button.append(title, meta, gauge);
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

function blockState(block) {
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
  const { done, total } = chapterProgress(chapter);
  note.textContent = total
    ? `${done} de ${total} parágrafos traduzidos. O texto original fica acima de cada campo.`
    : 'Capítulo sem texto traduzível.';
  inner.append(note);

  chapter.blocks.forEach((block, index) => {
    inner.append(renderBlock(block, chapter.blocks[index + 1]));
  });
}

const TEXT_KINDS = new Set([
  'heading1', 'heading2', 'heading3', 'paragraph', 'quote', 'list_item', 'footnote', 'table',
]);

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

function renderBlock(block, next) {
  const wrapper = document.createElement('div');
  wrapper.className = 'block';
  wrapper.dataset.id = block.id;
  wrapper.dataset.kind = block.kind;
  wrapper.dataset.state = blockState(block);

  const mark = document.createElement('span');
  mark.className = 'block__mark';
  mark.textContent = MARKS[block.kind] || '¶';
  mark.title = block.page ? `Página ${block.page} do original` : '';
  if (block.kind === 'footnote') {
    // A nota mostra o próprio marcador ("1", "*"), que liga ela à chamada no texto.
    mark.textContent = block.meta?.marker || MARKS.footnote;
    mark.title = `Nota de rodapé${mark.title ? ` · ${mark.title}` : ''}`;
  }
  wrapper.append(mark);

  if (block.kind === 'image') {
    const img = document.createElement('p');
    img.className = 'block__source';
    img.textContent = `[imagem: ${block.src || 'sem arquivo'}]`;
    wrapper.append(img, renderStructureTools(block, next));
    return wrapper;
  }

  // O original vem do arquivo e não se edita, exceto num bloco que o próprio
  // usuário inseriu: ali o original é justamente o que faltou na extração.
  if (block.meta?.inserted) {
    const sourceField = document.createElement('textarea');
    sourceField.className = 'block__source block__source-field';
    sourceField.rows = 1;
    sourceField.value = block.text;
    sourceField.placeholder = 'texto original que faltou';
    sourceField.setAttribute('aria-label', 'Texto original deste parágrafo');
    sourceField.oninput = () => {
      autoGrow(sourceField);
      block.text = sourceField.value;
      queueUpdate(block.id, { text: block.text });
    };
    sourceField.onblur = () => flushSaves();
    wrapper.append(sourceField);
    requestAnimationFrame(() => autoGrow(sourceField));
  } else {
    const source = document.createElement('p');
    source.className = 'block__source';
    source.innerHTML = inlineHtml(block.text);
    wrapper.append(source);
  }

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
    block.meta = { ...(block.meta || {}), edited: true };
    wrapper.dataset.state = blockState(block);
    markDirty(block);
  };
  field.onblur = () => flushSaves();

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
    block.meta = { ...(block.meta || {}), edited: false };
    wrapper.dataset.state = 'pending';
    markDirty(block, true);
    grow();
  };

  const split = document.createElement('button');
  split.className = 'block__tool';
  split.textContent = 'Começar capítulo aqui';
  split.onclick = () => splitHere(block.id);

  tools.append(copy, clear, split, ...renderStructureTools(block, next).children);
  wrapper.append(tools);

  return wrapper;
}

// Juntar, inserir e apagar: os consertos de estrutura. Vale para imagens também
// (apagar uma figura solta, inserir texto depois dela).
function renderStructureTools(block, next) {
  const tools = document.createElement('div');
  tools.className = 'block__tools';

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
      const field = document.querySelector(
        `.block[data-id="${data.block.id}"] .block__source-field`
      );
      field?.focus();
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
  const pct = project.total_translatable
    ? Math.round((project.translated_count / project.total_translatable) * 100)
    : 0;
  $('bookStat').textContent =
    `${project.word_count.toLocaleString('pt-BR')} palavras · ${pct}% traduzido`;
}

/* ---------------------------------------------------------------- preview */

function refreshPreview() {
  if (!state.project || !state.chapterId) return;
  const url =
    `/api/projects/${state.project.id}/preview/${state.chapterId}` +
    `?translated=${state.previewTranslated}&t=${Date.now()}`;
  $('previewFrame').src = url;
}

/* --------------------------------------------------------------- tradução */

async function startTranslation() {
  if (!state.project) return;
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
  const completed = job.done + job.failed;
  $('progressFill').style.width = `${Math.round((completed / total) * 100)}%`;

  let label = `${job.done}/${job.total}`;
  if (job.failed) label += ` · ${job.failed} falhas`;
  if (job.eta_seconds != null) label += ` · faltam ${formatDuration(job.eta_seconds)}`;
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
      const note = job.failed ? ` ${job.failed} parágrafos falharam.` : '';
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
    kindSelect.onchange = applyCatalogDefaults;
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
}

function applyCatalogDefaults() {
  const entry = state.catalog.find((c) => c.kind === $('pKind').value);
  if (!entry) return;
  $('pBase').value = entry.default_base_url;
  $('pHint').textContent = entry.hint;
  $('keyField').style.display = entry.needs_key ? '' : 'none';
  if (!$('pLabel').value || state.editingProviderId === null) {
    $('pLabel').value = entry.label;
  }
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
  $('pHint').textContent = entry?.hint || '';
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
    if (data.ok) toast(`Conectado. O modelo respondeu: ${data.reply}`);
    else toast(data.error, 'error');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    $('testProvider').disabled = false;
  }
}

async function loadModels() {
  if (!state.editingProviderId) {
    toast('Salve o provedor primeiro para listar os modelos', 'error');
    return;
  }
  try {
    const data = await api(`/api/providers/${state.editingProviderId}/models`);
    const list = $('modelOptions');
    list.innerHTML = '';
    for (const model of data.models) {
      const option = document.createElement('option');
      option.value = model;
      list.append(option);
    }
    toast(`${data.models.length} modelos disponíveis. Clique no campo para ver.`);
  } catch (error) {
    toast(error.message, 'error');
  }
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
  $('pickBtn').onclick = () => $('fileInput').click();
  $('fileInput').onchange = (event) => importFile(event.target.files[0]);

  const drop = $('drop');
  drop.ondragover = (event) => { event.preventDefault(); drop.dataset.over = 'true'; };
  drop.ondragleave = () => { drop.dataset.over = 'false'; };
  drop.ondrop = (event) => {
    event.preventDefault();
    drop.dataset.over = 'false';
    importFile(event.dataTransfer.files[0]);
  };

  $('startImport').onclick = startImport;
  $('importOcr').onchange = syncOcrFields;
  const closeImport = () => { state.pendingFile = null; $('importSheet').hidden = true; };
  $('closeImport').onclick = closeImport;
  $('importSheet').onclick = (event) => { if (event.target === $('importSheet')) closeImport(); };
  const closeSummary = () => { $('summarySheet').hidden = true; };
  $('closeSummary').onclick = closeSummary;
  $('summaryDone').onclick = closeSummary;
  $('summarySheet').onclick = (event) => { if (event.target === $('summarySheet')) closeSummary(); };

  $('homeBtn').onclick = goHome;
  $('translateBtn').onclick = startTranslation;
  $('exportBtn').onclick = async () => {
    await flushSaves();
    window.location = `/api/projects/${state.project.id}/export?translated=true`;
  };

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

  $('settingsBtn').onclick = () => { $('settingsSheet').hidden = false; loadSettings(); };
  $('closeSettings').onclick = () => { $('settingsSheet').hidden = true; };
  $('settingsSheet').onclick = (event) => {
    if (event.target === $('settingsSheet')) $('settingsSheet').hidden = true;
  };

  $('saveProvider').onclick = saveProvider;
  $('testProvider').onclick = testProvider;
  $('loadModels').onclick = loadModels;
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
      $('settingsSheet').hidden = true;
      closeImport();
      closeSummary();
    }
  });

  window.addEventListener('beforeunload', flushSaves);
}

function setPreviewMode(translated) {
  state.previewTranslated = translated;
  $('viewTranslated').setAttribute('aria-pressed', translated);
  $('viewOriginal').setAttribute('aria-pressed', !translated);
  refreshPreview();
}

wire();
loadProjects();
loadSettings();
resumeImports();
