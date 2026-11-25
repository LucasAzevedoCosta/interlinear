/* Interlinear — espaçamento fácil no texto.

   Entre cada dois blocos do editor fica uma faixa (.gap) com duas coisas:

   1. Uma alça para arrastar o espaço entre os blocos, para cima e para baixo.
      O valor se encaixa de 0,5 em 0,5 em (com Alt, de 0,1 em 0,1), aparece
      enquanto se arrasta e vira o mesmo ajuste de bloco que o painel
      Parágrafo edita (space_after de cima, space_before de baixo). Duplo
      clique volta ao espaço do estilo. Pelo teclado: setas.

   2. Um "+" para inserir ali uma linha em branco, um separador de cena, uma
      quebra de página ou (na conversão) um parágrafo novo.

   No CSS, a margem de baixo de um bloco e a de cima do seguinte não se somam:
   vale a maior (margin collapsing). O espaço entre os dois é, então, o maior
   dos dois valores, e arrastar define esse espaço: grava o de cima e, se o de
   baixo for maior que o novo valor, baixa o de baixo também.

   Atalhos num campo de texto: ⌘/Ctrl+Enter quebra de página, ⌘/Ctrl+Shift+
   Enter linha em branco, ⌘/Ctrl+Shift+8 separador de cena, depois do bloco.

   Lê do app.js: state, api, toast, refreshPreview, changeStructure,
   currentChapter, blockById. Do typography.js: Typography.
*/

'use strict';

const Spacing = (() => {
  const STEP = 0.5;
  const FINE = 0.1;
  const MIN = 0;
  const MAX = 8; // a faixa de space_before/space_after em core/styles.py
  const PX_PER_EM = 22; // quanto arrastar para mudar 1 em
  const BAND_PX_PER_EM = 12; // altura da faixa listrada por em, quando ajustado

  // Margem em em dos blocos que não usam estilo (DEFAULT_CSS).
  const FIXED_MARGIN = { image: 1, scene_break: 1 };

  const INSERTS = [
    { kind: 'spacer', label: 'Linha em branco', keys: '⌘⇧↵' },
    { kind: 'scene_break', label: 'Separador de cena  * * *', keys: '⌘⇧8' },
    { kind: 'page_break', label: 'Quebra de página', keys: '⌘↵' },
    { kind: 'paragraph', label: 'Parágrafo novo', keys: '', convertOnly: true },
  ];

  const formatEm = (value) => `${String(Math.round(value * 100) / 100).replace('.', ',')} em`;
  const clamp = (value) => Math.min(MAX, Math.max(MIN, value));
  const styled = (block) => Boolean(block) && Typography.STYLED.has(block.kind);

  /* -------------------------------------------------------------- valor */

  function sideValue(block, side) {
    if (!block) return 0;
    if (styled(block)) return Typography.effectiveOf(block)[side] ?? 0;
    return FIXED_MARGIN[block.kind] ?? 0;
  }

  // O espaço que aparece entre os dois blocos (o maior dos dois lados).
  function gapValue(above, below) {
    return Math.max(sideValue(above, 'space_after'), sideValue(below, 'space_before'));
  }

  // O par foi ajustado à mão (não é só o que os estilos dão)?
  function adjusted(above, below) {
    return (styled(above) && 'space_after' in Typography.overridesOf(above))
      || (styled(below) && 'space_before' in Typography.overridesOf(below));
  }

  // O ajuste a gravar em cada bloco para o espaço ficar `value`. Um valor
  // igual ao do estilo vira null: some o ajuste, em vez de repetir o estilo.
  function changesFor(above, below, value) {
    const out = [];
    const forBlock = (block, side, wanted) => {
      const fromStyle = Typography.styleValues(block)[side] ?? 0;
      out.push({ block, changes: { [side]: wanted === fromStyle ? null : wanted } });
    };
    if (styled(above)) {
      forBlock(above, 'space_after', value);
      if (styled(below) && sideValue(below, 'space_before') > value) {
        forBlock(below, 'space_before', value);
      }
    } else if (styled(below)) {
      forBlock(below, 'space_before', value);
    }
    return out;
  }

  function resetChanges(above, below) {
    const out = [];
    if (styled(above)) out.push({ block: above, changes: { space_after: null } });
    if (styled(below)) out.push({ block: below, changes: { space_before: null } });
    return out;
  }

  async function save(updates) {
    if (!updates.length) return false;
    try {
      await api(`/api/projects/${state.project.id}/blocks`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          updates: updates.map(({ block, changes }) => ({ id: block.id, overrides: changes })),
        }),
      });
    } catch (error) {
      toast(error.message, 'error');
      return false;
    }
    for (const { block, changes } of updates) Typography.mergeLocal(block, changes);
    Typography.renderPanels();
    refreshPreview();
    return true;
  }

  /* -------------------------------------------------------------- a faixa */

  function gap(above, below) {
    const element = document.createElement('div');
    element.className = 'gap';
    element.dataset.after = above.id;

    const canDrag = Boolean(below) && (styled(above) || styled(below));
    let handle = null;
    let pill = null;

    if (canDrag) {
      handle = document.createElement('div');
      handle.className = 'gap__handle';
      handle.tabIndex = 0;
      handle.setAttribute('role', 'slider');
      handle.setAttribute('aria-label', 'Espaço entre os blocos');
      handle.setAttribute('aria-valuemin', String(MIN));
      handle.setAttribute('aria-valuemax', String(MAX));
      handle.title = 'Arraste para mudar o espaço (Alt: passos de 0,1). Duplo clique volta ao estilo.';
      pill = document.createElement('span');
      pill.className = 'gap__pill';
      handle.append(pill);
      element.append(handle);
    }

    const plus = document.createElement('button');
    plus.type = 'button';
    plus.className = 'gap__plus';
    plus.textContent = '+';
    plus.title = 'Inserir aqui: linha em branco, separador, quebra de página';
    plus.setAttribute('aria-label', 'Inserir entre os blocos');
    plus.setAttribute('aria-haspopup', 'menu');
    plus.setAttribute('aria-expanded', 'false');
    plus.onclick = (event) => {
      event.stopPropagation();
      toggleMenu(element, plus, above);
    };
    element.append(plus);

    // Mostra o valor atual: faixa listrada da altura do espaço, quando ajustado.
    const paint = (value = gapValue(above, below), live = false) => {
      if (!handle) return;
      const marked = live || adjusted(above, below);
      element.dataset.adjusted = String(marked);
      element.style.setProperty('--gap-band', marked ? `${Math.max(6, value * BAND_PX_PER_EM)}px` : '0px');
      pill.textContent = `↕ ${formatEm(value)}`;
      handle.setAttribute('aria-valuenow', String(value));
      handle.setAttribute('aria-valuetext', formatEm(value));
    };
    paint();
    // Os estilos podem chegar (ou mudar) depois de a faixa ser desenhada.
    element.repaint = () => paint();

    if (!handle) return element;

    // Arrastar.
    handle.onpointerdown = (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const startY = event.clientY;
      const start = gapValue(above, below);
      let value = start;
      element.dataset.dragging = 'true';
      handle.onpointermove = (move) => {
        const step = move.altKey ? FINE : STEP;
        const raw = start + (move.clientY - startY) / PX_PER_EM;
        value = clamp(Math.round(raw / step) * step);
        value = Math.round(value * 100) / 100;
        paint(value, true);
      };
      handle.onpointerup = async () => {
        handle.onpointermove = null;
        handle.onpointerup = null;
        delete element.dataset.dragging;
        if (value !== start && (await save(changesFor(above, below, value)))) paint();
        else paint();
      };
    };

    handle.ondblclick = async () => {
      if (await save(resetChanges(above, below))) paint();
    };

    // Teclado: setas mudam 0,5 (Alt: 0,1); Delete volta ao estilo.
    let keyTimer = null;
    let pending = null;
    handle.onkeydown = (event) => {
      if (event.key === 'Delete' || event.key === 'Backspace') {
        event.preventDefault();
        handle.ondblclick();
        return;
      }
      if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
      event.preventDefault();
      const step = event.altKey ? FINE : STEP;
      const current = pending ?? gapValue(above, below);
      pending = Math.round(clamp(current + (event.key === 'ArrowDown' ? step : -step)) * 100) / 100;
      paint(pending, true);
      clearTimeout(keyTimer);
      keyTimer = setTimeout(async () => {
        const value = pending;
        pending = null;
        await save(changesFor(above, below, value));
        paint();
      }, 450);
    };

    return element;
  }

  /* --------------------------------------------------------- inserir */

  let openMenu = null;

  function closeMenu() {
    if (!openMenu) return;
    openMenu.menu.remove();
    openMenu.button.setAttribute('aria-expanded', 'false');
    openMenu = null;
  }

  function toggleMenu(element, button, above) {
    const wasOpen = openMenu?.button === button;
    closeMenu();
    if (wasOpen) return;
    const menu = document.createElement('div');
    menu.className = 'gap__menu';
    menu.setAttribute('role', 'menu');
    for (const item of INSERTS) {
      if (item.convertOnly && state.stage !== 'convert') continue;
      const option = document.createElement('button');
      option.type = 'button';
      option.setAttribute('role', 'menuitem');
      const label = document.createElement('span');
      label.textContent = item.label;
      option.append(label);
      if (item.keys) {
        const keys = document.createElement('kbd');
        keys.textContent = item.keys;
        option.append(keys);
      }
      option.onclick = () => {
        closeMenu();
        insertAfter(above, item.kind);
      };
      menu.append(option);
    }
    element.append(menu);
    button.setAttribute('aria-expanded', 'true');
    openMenu = { menu, button };
    menu.querySelector('button')?.focus();
  }

  function insertAfter(block, kind) {
    const chapter = currentChapter();
    const index = chapter.blocks.findIndex((b) => b.id === block.id) + 1;
    changeStructure(
      `/chapters/${chapter.id}/blocks`,
      { method: 'POST', body: JSON.stringify({ index, kind }) },
      (data) => {
        if (kind === 'paragraph') {
          document.querySelector(`.block[data-id="${data.block.id}"] .block__text`)?.click();
        }
      }
    );
  }

  /* ---------------------------------------------------------- atalhos */

  function shortcut(event) {
    if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
    if (event.target.tagName !== 'TEXTAREA') return;
    let kind = null;
    if (event.key === 'Enter') kind = event.shiftKey ? 'spacer' : 'page_break';
    else if (event.shiftKey && event.code === 'Digit8') kind = 'scene_break';
    if (!kind) return;
    const wrapper = event.target.closest('#editorInner .block[data-id]');
    const block = wrapper && blockById(wrapper.dataset.id);
    if (!block) return;
    event.preventDefault();
    insertAfter(block, kind);
  }

  function wire() {
    document.addEventListener('click', (event) => {
      if (!event.target.closest('.gap__menu, .gap__plus')) closeMenu();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeMenu();
    });
    document.getElementById('editor').addEventListener('keydown', shortcut);
  }

  wire();

  // Redesenha as faixas com os valores atuais: quando os estilos do livro
  // terminam de carregar e quando um estilo muda no painel.
  function refresh() {
    for (const element of document.querySelectorAll('#editorInner .gap')) element.repaint?.();
  }

  return { gap, closeMenu, refresh };
})();
