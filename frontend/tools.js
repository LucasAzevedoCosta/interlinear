/* Interlinear — barra de ferramentas vertical e atalhos, como nos apps da Adobe.

   Ferramentas (uma de cada vez, letra entre parênteses):
     Selecionar (V)   clicar escolhe o bloco; o texto não se edita, para
                      mexer na formatação sem mudar o texto sem querer
     Texto (T)        o padrão: clicar põe o cursor no texto
     Espaçamento (E)  mostra o espaço entre todos os blocos, com o valor
     Conta-gotas (I)  copia a formatação (estilo e ajuste) do bloco
                      selecionado para cada bloco clicado depois

   Ações: inserir separador de cena depois do bloco selecionado (S), zoom do
   texto, desfazer e refazer a formatação, recolher os painéis (Tab).

   Atalhos:
     V T E I S, Tab        fora de um campo de texto
     ⌘Z, ⌘⇧Z (Ctrl+Y)      desfazer e refazer a formatação, fora de um campo
                           (dentro, é o desfazer do próprio texto)
     Ctrl+Alt+1…9          aplica o estilo 1…9 da lista ao bloco selecionado,
                           mesmo digitando; Ctrl+Alt+0 volta ao automático

   Lê do app.js (state, toast, blockById), do layout.js (Layout), do
   typography.js (Typography), do spacing.js (Spacing), do history.js
   (History) e do richtext.js (RichText).
*/

'use strict';

const Tools = (() => {
  const ZOOM_KEY = 'interlinear.editorZoom';
  const ZOOMS = [0.8, 0.9, 1, 1.1, 1.25, 1.4, 1.6];

  const ICONS = {
    select: '<path d="M4 3l12 6-5 1.5L9 16z"/>',
    text: '<path d="M4 5V3h12v2M10 3v14M7 17h6"/>',
    spacing: '<path d="M3 2h14M3 18h14M10 5v10M7 8l3-3 3 3M7 12l3 3 3-3"/>',
    eyedropper: '<path d="M12 4l4 4M10.5 5.5l4 4L7 17H3v-4z"/>',
    scene: '<path d="M2 10h3M8.5 10h3M15 10h3M10 3v3M10 14v3"/>',
    zoomIn: '<circle cx="9" cy="9" r="5"/><path d="M13 13l4 4M7 9h4M9 7v4"/>',
    zoomOut: '<circle cx="9" cy="9" r="5"/><path d="M13 13l4 4M7 9h4"/>',
    undo: '<path d="M7 5L3 9l4 4M3 9h9a5 5 0 010 10h-2"/>',
    redo: '<path d="M13 5l4 4-4 4M17 9H8a5 5 0 000 10h2"/>',
    focus: '<path d="M3 7V3h4M13 3h4v4M17 13v4h-4M7 17H3v-4"/>',
  };

  const TOOLS = [
    { id: 'select', key: 'v', label: 'Selecionar', hint: 'clicar escolhe o bloco, sem editar o texto' },
    { id: 'text', key: 't', label: 'Texto', hint: 'clicar põe o cursor no texto' },
    { id: 'spacing', key: 'e', label: 'Espaçamento', hint: 'mostra o espaço entre todos os blocos' },
    { id: 'eyedropper', key: 'i', label: 'Conta-gotas', hint: 'copia a formatação do bloco selecionado para os que você clicar' },
  ];

  const el = (id) => document.getElementById(id);
  let tool = 'text';
  let source = null; // bloco de onde o conta-gotas copia
  let zoom = 1;

  function icon(name) {
    return `<svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  }

  function button({ id, iconName, label, onClick, pressed }) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'tools__button';
    if (id) node.dataset.tool = id;
    node.title = label;
    node.setAttribute('aria-label', label);
    if (pressed !== undefined) node.setAttribute('aria-pressed', String(pressed));
    node.innerHTML = icon(iconName);
    // Não tira o foco do texto: a seleção continua onde estava.
    node.onmousedown = (event) => event.preventDefault();
    node.onclick = onClick;
    return node;
  }

  function separator() {
    const line = document.createElement('span');
    line.className = 'tools__sep';
    line.setAttribute('aria-hidden', 'true');
    return line;
  }

  function render() {
    const bar = el('tools');
    bar.replaceChildren();
    for (const item of TOOLS) {
      bar.append(button({
        id: item.id,
        iconName: item.id,
        label: `${item.label} (${item.key.toUpperCase()}): ${item.hint}`,
        pressed: tool === item.id,
        onClick: () => setTool(item.id),
      }));
    }
    bar.append(separator());
    bar.append(button({
      iconName: 'scene',
      label: 'Separador de cena depois do bloco selecionado (S)',
      onClick: insertScene,
    }));
    bar.append(separator());
    bar.append(button({ iconName: 'zoomOut', label: 'Diminuir o texto', onClick: () => stepZoom(-1) }));
    const level = document.createElement('span');
    level.className = 'tools__zoom';
    level.id = 'toolsZoom';
    level.textContent = `${Math.round(zoom * 100)}%`;
    bar.append(level);
    bar.append(button({ iconName: 'zoomIn', label: 'Aumentar o texto', onClick: () => stepZoom(1) }));
    bar.append(separator());
    const undo = History.nextUndo();
    const redo = History.nextRedo();
    const undoButton = button({
      iconName: 'undo',
      label: undo ? `Desfazer: ${undo} (⌘Z)` : 'Nada para desfazer',
      onClick: () => History.undo(),
    });
    undoButton.disabled = !undo;
    const redoButton = button({
      iconName: 'redo',
      label: redo ? `Refazer: ${redo} (⌘⇧Z)` : 'Nada para refazer',
      onClick: () => History.redo(),
    });
    redoButton.disabled = !redo;
    bar.append(undoButton, redoButton);
    bar.append(separator());
    bar.append(button({
      iconName: 'focus',
      label: 'Recolher os painéis e ficar só com o texto (Tab)',
      onClick: () => Layout.toggleFocus(),
    }));
  }

  /* --------------------------------------------------------- ferramentas */

  function setTool(id) {
    if (id === 'eyedropper') {
      const block = Typography.selected();
      if (!block || !Typography.STYLED.has(block.kind)) {
        toast('Selecione o bloco de onde copiar a formatação e escolha o conta-gotas de novo.');
        return;
      }
      source = block;
      toast(`Conta-gotas: clique nos blocos que devem ficar como “${block.text.replace(/<[^>]+>/g, '').slice(0, 30)}…”. Esc termina.`);
    } else {
      source = null;
    }
    tool = id;
    el('editor').dataset.tool = id;
    RichText.setEditable(id !== 'select' && id !== 'eyedropper');
    render();
  }

  // Conta-gotas: o clique num bloco aplica a formatação em vez de selecionar.
  async function eyedrop(event) {
    if (tool !== 'eyedropper' || !source) return;
    const wrapper = event.target.closest?.('#editorInner .block[data-id]');
    if (!wrapper) return;
    event.preventDefault();
    event.stopPropagation();
    const target = blockById(wrapper.dataset.id);
    if (!target || target.id === source.id) return;
    if (!Typography.STYLED.has(target.kind)) {
      toast('Esse bloco não usa estilo de parágrafo.');
      return;
    }
    if (await Typography.copyFormat(source, target)) {
      wrapper.classList.remove('block--flash');
      void wrapper.offsetWidth;
      wrapper.classList.add('block--flash');
    }
  }

  function insertScene() {
    const block = Typography.selected();
    if (!block) {
      toast('Selecione o bloco depois do qual vai o separador.');
      return;
    }
    Spacing.insertAfter(block, 'scene_break');
  }

  /* --------------------------------------------------------------- zoom */

  function applyZoom() {
    el('editorInner').style.setProperty('--editor-zoom', String(zoom));
    const level = el('toolsZoom');
    if (level) level.textContent = `${Math.round(zoom * 100)}%`;
  }

  function stepZoom(direction) {
    const index = ZOOMS.indexOf(zoom);
    zoom = ZOOMS[Math.min(ZOOMS.length - 1, Math.max(0, (index < 0 ? 2 : index) + direction))];
    try { localStorage.setItem(ZOOM_KEY, String(zoom)); } catch { /* fica só nesta sessão */ }
    applyZoom();
  }

  /* ------------------------------------------------------------ atalhos */

  function typing(target) {
    return Boolean(target?.closest?.('input, textarea, select, [contenteditable="true"]'));
  }

  function onKeydown(event) {
    if (el('dock').hidden || !state.project) return;
    if (!el('settingsPage').hidden) return;
    if (document.querySelector('.sheet:not([hidden])')) return;

    // Ctrl+Alt+número: estilo da lista, mesmo digitando.
    if (event.ctrlKey && event.altKey && /^Digit\d$/.test(event.code)) {
      const block = Typography.selected();
      if (!block || !Typography.STYLED.has(block.kind)) return;
      event.preventDefault();
      const number = Number(event.code.slice(5));
      if (number === 0) {
        Typography.setBlockStyle(block, null);
        return;
      }
      const style = Typography.styles()[number - 1];
      if (style) Typography.setBlockStyle(block, style.id);
      return;
    }

    if (typing(event.target)) return;

    const mod = event.metaKey || event.ctrlKey;
    if (mod && !event.altKey && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      if (event.shiftKey) History.redo();
      else History.undo();
      return;
    }
    if (mod && !event.altKey && event.key.toLowerCase() === 'y') {
      event.preventDefault();
      History.redo();
      return;
    }
    if (mod || event.altKey) return;

    if (event.key === 'Tab' && !event.shiftKey && document.activeElement === document.body) {
      event.preventDefault();
      Layout.toggleFocus();
      return;
    }
    if (event.key === 'Escape' && tool === 'eyedropper') {
      setTool('text');
      return;
    }
    const key = event.key.toLowerCase();
    const chosen = TOOLS.find((item) => item.key === key);
    if (chosen) {
      event.preventDefault();
      setTool(chosen.id);
    } else if (key === 's') {
      event.preventDefault();
      insertScene();
    }
  }

  function wire() {
    try {
      const saved = Number(localStorage.getItem(ZOOM_KEY));
      if (ZOOMS.includes(saved)) zoom = saved;
    } catch { /* armazenamento bloqueado: 100% */ }
    applyZoom();
    el('editor').dataset.tool = tool;
    // Na fase de captura, antes de o clique selecionar o bloco.
    el('editor').addEventListener('click', eyedrop, true);
    document.addEventListener('keydown', onKeydown);
    History.onChange(render);
    render();
  }

  wire();

  return {
    open() { el('tools').hidden = false; },
    close() {
      el('tools').hidden = true;
      if (tool !== 'text') setTool('text');
    },
  };
})();
