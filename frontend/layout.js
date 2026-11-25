/* Interlinear — painéis encaixáveis da bancada.

   Os painéis (Capítulos, Texto, Prévia, Original) são os mesmos elementos de sempre,
   com os mesmos ids: este arquivo só os muda de lugar. O app.js continua
   achando tudo por getElementById e não sabe que existe um dock.

   Quem arruma os painéis é o dockview-core (MIT, frontend/vendor/). Ele
   permite arrastar uma aba para qualquer lado, agrupar em abas, soltar como
   janela flutuante e devolve a disposição como JSON.

   Áreas de trabalho, como nas ferramentas da Adobe:
     - há áreas prontas e as que o usuário salva com nome;
     - o que se mexe fica na área atual ("viva") até Redefinir ou trocar de área;
     - Redefinir volta a área pronta ao desenho original, ou a salva ao que
       foi salvo.
   Tudo é gravado no servidor (/api/ui/workspace), para sobreviver a limpar o
   navegador. Um JSON que não dê para montar (versão antiga da biblioteca,
   painel que deixou de existir) é descartado e a área volta ao desenho base.
*/

'use strict';

const Layout = (() => {
  const dockview = window['dockview-core'];
  const VERSION = 1;
  const SAVE_DELAY = 700;

  // id do painel -> título da aba e id do elemento que ele mostra.
  const PANELS = {
    chapters: { title: 'Capítulos', node: 'rail' },
    editor: { title: 'Texto', node: 'editor' },
    preview: { title: 'Prévia EPUB', node: 'preview' },
    // A página do PDF (original.js). Opcional: nas áreas prontas entra como
    // aba atrás do Texto, e só aparece quando o usuário clica nela.
    original: { title: 'Original · PDF', node: 'original' },
  };

  // Onde um painel reaberto pelo menu Janela entra, se não houver referência.
  const REOPEN = {
    chapters: { direction: 'left' },
    editor: { referencePanel: 'chapters', direction: 'right' },
    preview: { direction: 'right' },
    // Pelo menu Janela, abre junto do Texto (e já à frente, por ser nova).
    original: { referencePanel: 'editor', direction: 'within' },
  };

  function addChapters(api) {
    api.addPanel({ id: 'chapters', component: 'chapters', title: PANELS.chapters.title });
  }

  // O Original como aba atrás do Texto. Arrastar a aba para o lado deixa os
  // dois visíveis ao mesmo tempo, e a área de trabalho guarda isso.
  function addOriginalTab(api) {
    api.addPanel({ id: 'original', component: 'original', title: PANELS.original.title,
      position: { referencePanel: 'editor', direction: 'within' }, inactive: true });
  }

  function sizeGroup(api, panelId, width) {
    const panel = api.getPanel(panelId);
    if (panel) panel.group.api.setSize({ width });
  }

  // As áreas prontas. Constroem a disposição a partir do zero.
  const PRESETS = {
    padrao: {
      name: 'Padrão',
      build(api) {
        addChapters(api);
        api.addPanel({ id: 'editor', component: 'editor', title: PANELS.editor.title,
          position: { referencePanel: 'chapters', direction: 'right' } });
        addOriginalTab(api);
        api.addPanel({ id: 'preview', component: 'preview', title: PANELS.preview.title,
          position: { referencePanel: 'editor', direction: 'right' } });
        sizeGroup(api, 'chapters', 232);
        sizeGroup(api, 'preview', Math.round(api.width * 0.36));
      },
    },
    foco: {
      name: 'Foco no texto',
      build(api) {
        addChapters(api);
        api.addPanel({ id: 'editor', component: 'editor', title: PANELS.editor.title,
          position: { referencePanel: 'chapters', direction: 'right' } });
        addOriginalTab(api);
        // A prévia fica como aba atrás do texto: um clique e ela aparece.
        api.addPanel({ id: 'preview', component: 'preview', title: PANELS.preview.title,
          position: { referencePanel: 'editor', direction: 'within' }, inactive: true });
        sizeGroup(api, 'chapters', 200);
      },
    },
    lado: {
      name: 'Prévia lado a lado',
      build(api) {
        addChapters(api);
        api.addPanel({ id: 'editor', component: 'editor', title: PANELS.editor.title,
          position: { referencePanel: 'chapters', direction: 'right' } });
        addOriginalTab(api);
        api.addPanel({ id: 'preview', component: 'preview', title: PANELS.preview.title,
          position: { referencePanel: 'editor', direction: 'right' } });
        sizeGroup(api, 'chapters', 200);
        sizeGroup(api, 'preview', Math.round((api.width - 200) / 2));
      },
    },
  };

  let api = null;
  let applying = false;
  // A disposição logo depois de montada, sem os tamanhos. Mudar só o tamanho
  // da janela não conta como mexer na área.
  let baseline = '';
  let saveTimer = null;
  // { version, active, workspaces: { id: { name, saved?, live? } } }
  //   saved: a disposição gravada com "Nova área" (só nas do usuário);
  //   live: o que mudou desde então. Ausente = desenho base.
  let store = emptyStore();
  let loaded = null;

  function emptyStore() {
    const workspaces = {};
    for (const [id, preset] of Object.entries(PRESETS)) workspaces[id] = { name: preset.name };
    return { version: VERSION, active: 'padrao', workspaces };
  }

  /* -------------------------------------------------------- persistência */

  function load() {
    if (!loaded) {
      loaded = fetch('/api/ui/workspace')
        .then((response) => (response.ok ? response.json() : { workspace: null }))
        .then((data) => { store = normalize(data.workspace); })
        .catch(() => { store = emptyStore(); });
    }
    return loaded;
  }

  // Aceita só o que tem a forma esperada; o resto vira o padrão. As áreas
  // prontas sempre existem, mesmo que o arquivo não as tenha.
  function normalize(raw) {
    const clean = emptyStore();
    if (!raw || raw.version !== VERSION || typeof raw.workspaces !== 'object') return clean;
    for (const [id, entry] of Object.entries(raw.workspaces)) {
      if (!entry || typeof entry !== 'object') continue;
      const builtin = id in PRESETS;
      if (!builtin && !isLayout(entry.saved)) continue;
      clean.workspaces[id] = {
        name: builtin ? PRESETS[id].name : String(entry.name || 'Sem nome').slice(0, 60),
        ...(builtin ? {} : { saved: entry.saved }),
        ...(isLayout(entry.live) ? { live: entry.live } : {}),
      };
    }
    if (raw.active in clean.workspaces) clean.active = raw.active;
    return clean;
  }

  function isLayout(value) {
    return Boolean(value && typeof value === 'object' && value.grid);
  }

  function persist() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      fetch('/api/ui/workspace', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(store),
      }).catch(() => { /* fica só nesta sessão; a próxima mudança tenta de novo */ });
    }, SAVE_DELAY);
  }

  /* ------------------------------------------------------------ o dock */

  function createComponent({ name }) {
    const spec = PANELS[name];
    if (!spec) throw new Error(`Painel desconhecido: ${name}`);
    const element = document.createElement('div');
    element.className = 'dock-panel';
    element.append(document.getElementById(spec.node));
    return { element, init() {} };
  }

  // Painel fechado: o elemento volta para o estacionamento, que está no
  // documento. Se ficasse solto, o app.js deixaria de achá-lo pelo id.
  function park(panelId) {
    const spec = PANELS[panelId];
    if (!spec) return;
    // Ao montar uma disposição o dockview remove e recria os painéis; só
    // estaciona se o painel não voltou.
    queueMicrotask(() => {
      if (api && api.getPanel(panelId)) return;
      document.getElementById('dockParking').append(document.getElementById(spec.node));
    });
  }

  function create() {
    const element = document.getElementById('dock');
    api = dockview.createDockview(element, {
      theme: dockview.themeDark,
      createComponent,
      // Painel escondido atrás de outra aba continua montado: a prévia não
      // recarrega e o campo em edição não perde o cursor ao trocar de aba.
      defaultRenderer: 'always',
      floatingGroupBounds: 'boundedWithinViewport',
    });
    api.onDidRemovePanel((panel) => park(panel.id));
    api.onDidLayoutChange(() => {
      if (applying) return;
      const entry = store.workspaces[store.active];
      if (!entry) return;
      const layout = api.toJSON();
      if (shape(layout) === baseline) {
        if (!entry.live) return;
        delete entry.live;
      } else {
        entry.live = layout;
      }
      persist();
    });
  }

  // A estrutura da disposição (quem está em qual grupo, em que ordem), sem
  // as medidas, que mudam com o tamanho da janela.
  function shape(layout) {
    return JSON.stringify(layout, (key, value) => (
      ['size', 'width', 'height', 'position', 'box'].includes(key) ? undefined : value
    ));
  }

  function build(id) {
    const entry = store.workspaces[id];
    if (id in PRESETS) PRESETS[id].build(api);
    else api.fromJSON(entry.saved);
  }

  // Monta a área `id`: a versão viva se houver, senão o desenho base. Se
  // nada disso montar, cai na área Padrão do zero.
  function apply(id) {
    applying = true;
    try {
      const entry = store.workspaces[id];
      try {
        api.clear();
        if (entry.live) api.fromJSON(entry.live);
        else build(id);
      } catch (error) {
        console.warn('Área de trabalho não pôde ser montada; voltando ao padrão.', error);
        delete entry.live;
        try {
          api.clear();
          build(id);
        } catch {
          api.clear();
          id = 'padrao';
          PRESETS.padrao.build(api);
        }
      }
      store.active = id;
      // Área já mexida: não há com o que comparar até Redefinir.
      baseline = store.workspaces[id].live ? '' : shape(api.toJSON());
    } finally {
      applying = false;
    }
    renderPicker();
  }

  /* ------------------------------------------------------------ ações */

  function switchTo(id) {
    if (!api || !(id in store.workspaces) || id === store.active) return;
    apply(id);
    persist();
  }

  function reset() {
    const entry = store.workspaces[store.active];
    if (!entry) return;
    delete entry.live;
    apply(store.active);
    persist();
  }

  function saveAs() {
    const name = (window.prompt('Nome da nova área de trabalho:') || '').trim().slice(0, 60);
    if (!name) return;
    const id = `u-${Date.now().toString(36)}`;
    store.workspaces[id] = { name, saved: api.toJSON() };
    store.active = id;
    renderPicker();
    persist();
    window.toast?.(`Área “${name}” salva`);
  }

  function remove() {
    const id = store.active;
    const entry = store.workspaces[id];
    if (!entry || id in PRESETS) return;
    if (!window.confirm(`Excluir a área de trabalho “${entry.name}”?`)) return;
    delete store.workspaces[id];
    apply('padrao');
    persist();
  }

  function togglePanel(panelId) {
    const panel = api.getPanel(panelId);
    if (panel) {
      api.removePanel(panel);
      return;
    }
    let position = REOPEN[panelId];
    if (position.referencePanel && !api.getPanel(position.referencePanel)) {
      position = { direction: 'left' };
    }
    if (!api.panels.length) position = undefined;
    api.addPanel({ id: panelId, component: panelId, title: PANELS[panelId].title,
      ...(position ? { position } : {}) });
  }

  function floatActive() {
    const panel = api.activePanel;
    if (panel) api.addFloatingGroup(panel);
  }

  /* ------------------------------------------------------ menu e seletor */

  function renderPicker() {
    const select = document.getElementById('workspacePick');
    select.innerHTML = '';
    for (const [id, entry] of Object.entries(store.workspaces)) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = entry.name;
      select.append(option);
    }
    select.value = store.active;
  }

  function menuItem(label, onClick, { hint = '', checked = null, disabled = false } = {}) {
    const button = document.createElement('button');
    button.setAttribute('role', checked === null ? 'menuitem' : 'menuitemcheckbox');
    if (checked !== null) button.setAttribute('aria-checked', String(checked));
    button.disabled = disabled;
    const title = document.createElement('b');
    title.textContent = (checked === null ? '' : checked ? '✓ ' : ' ') + label;
    button.append(title);
    if (hint) {
      const small = document.createElement('span');
      small.textContent = hint;
      button.append(small);
    }
    button.onclick = () => { closeMenu(); onClick(); };
    return button;
  }

  function separator() {
    const line = document.createElement('hr');
    line.className = 'menu__sep';
    return line;
  }

  function renderMenu() {
    const menu = document.getElementById('windowMenu');
    menu.innerHTML = '';
    for (const [id, spec] of Object.entries(PANELS)) {
      menu.append(menuItem(spec.title, () => togglePanel(id), { checked: Boolean(api.getPanel(id)) }));
    }
    menu.append(menuItem('Soltar o painel ativo', floatActive, {
      hint: 'Vira janela flutuante. Para encaixar de novo, arraste a aba de volta',
      disabled: !api.activePanel,
    }));
    menu.append(separator());

    const entry = store.workspaces[store.active];
    const custom = !(store.active in PRESETS);
    menu.append(menuItem('Nova área de trabalho…', saveAs, {
      hint: 'Guarda a disposição atual com um nome',
    }));
    menu.append(menuItem(`Redefinir “${entry.name}”`, reset, {
      hint: custom ? 'Volta ao que foi salvo' : 'Volta ao desenho original',
      disabled: !entry.live,
    }));
    if (custom) menu.append(menuItem(`Excluir “${entry.name}”`, remove));
  }

  function openMenu() {
    renderMenu();
    document.getElementById('windowMenu').hidden = false;
    document.getElementById('windowBtn').setAttribute('aria-expanded', 'true');
    document.querySelector('#windowMenu button:not(:disabled)')?.focus();
  }

  function closeMenu() {
    document.getElementById('windowMenu').hidden = true;
    document.getElementById('windowBtn').setAttribute('aria-expanded', 'false');
  }

  function wire() {
    document.getElementById('windowBtn').onclick = (event) => {
      event.stopPropagation();
      if (document.getElementById('windowMenu').hidden) openMenu();
      else closeMenu();
    };
    document.addEventListener('click', (event) => {
      if (!event.target.closest('#windowAnchor')) closeMenu();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeMenu();
    });
    document.getElementById('workspacePick').onchange = (event) => switchTo(event.target.value);
  }

  /* ------------------------------------------------------- abrir/fechar */

  // Mostra a bancada. A primeira vez cria o dock e monta a área ativa; nas
  // seguintes (outro livro) a disposição já está lá.
  async function open() {
    await load();
    document.getElementById('dock').hidden = false;
    document.getElementById('windowAnchor').hidden = false;
    document.getElementById('workspacePickLabel').hidden = false;
    if (!api) {
      create();
      apply(store.active);
    }
  }

  function close() {
    closeMenu();
    document.getElementById('dock').hidden = true;
    document.getElementById('windowAnchor').hidden = true;
    document.getElementById('workspacePickLabel').hidden = true;
  }

  wire();
  load();

  return { open, close, switchTo, reset };
})();
