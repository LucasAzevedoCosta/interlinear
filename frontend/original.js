/* Interlinear — painel "Original · PDF".

   Mostra a página do PDF de onde o texto veio, para comparar a conversão (e
   a tradução) com o livro de verdade. É opcional: começa como aba atrás do
   Texto e só pede imagens ao servidor quando está visível.

   Cada bloco extraído de um PDF guarda onde estava em meta.boxes:
   [[página, x0, topo, x1, base], ...], em pontos, do canto de cima à
   esquerda. Um parágrafo que atravessa a página ou a coluna tem mais de um
   retângulo. Aqui eles viram caixas por cima da imagem, em porcentagem do
   tamanho da página, o que dispensa recalcular a cada zoom:
     - a do bloco selecionado fica destacada;
     - as outras aparecem ao passar o mouse e, clicadas, levam ao bloco no
       texto (goToBlock, do app.js).

   Lê do app.js, que é carregado antes: state, blockById, goToBlock e
   visiblePage.
*/

'use strict';

const Original = (() => {
  // As mesmas resoluções que o servidor aceita (PAGE_DPI em routes/projects.py).
  const DPIS = [72, 110, 150, 200];
  const CSS_PX_PER_POINT = 96 / 72;

  const el = (id) => document.getElementById(id);

  let source = null; // { projectId, kind, pages: [[w, h], ...] }
  let loading = null;
  let page = 1;
  let selectedId = null;
  let scrollTimer = null;

  /* ------------------------------------------------------------ dados */

  function loadSource() {
    const projectId = state.project?.id;
    if (!projectId) return Promise.resolve(null);
    if (source?.projectId === projectId) return Promise.resolve(source);
    if (loading?.projectId === projectId) return loading.promise;
    const promise = api(`/api/projects/${projectId}/source`)
      .then((data) => {
        if (state.project?.id !== projectId) return null; // trocou de livro no meio
        source = { projectId, kind: data.kind, pages: data.pages || [] };
        return source;
      })
      .catch(() => null)
      .finally(() => { loading = null; });
    loading = { projectId, promise };
    return promise;
  }

  function boxesOf(block) {
    const boxes = block?.meta?.boxes;
    return Array.isArray(boxes) ? boxes.filter((b) => Array.isArray(b) && b.length === 5) : [];
  }

  // Todos os blocos do livro com retângulo nesta página, na ordem do livro.
  function blocksOnPage(number) {
    const found = [];
    for (const chapter of state.doc?.chapters || []) {
      for (const block of chapter.blocks) {
        const boxes = boxesOf(block).filter((box) => box[0] === number);
        if (boxes.length) found.push({ block, boxes });
      }
    }
    return found;
  }

  function firstPageOf(block) {
    return boxesOf(block)[0]?.[0] || block?.page || null;
  }

  /* --------------------------------------------------------- desenho */

  function visible() {
    // Aba escondida atrás de outra, painel fechado (no estacionamento) ou
    // bancada fechada: nada a desenhar.
    return el('origScroll').getClientRects().length > 0;
  }

  function empty(message) {
    el('origSheet').hidden = true;
    el('origEmpty').hidden = false;
    el('origEmpty').textContent = message;
    el('origPageLabel').textContent = '';
    el('origPrev').disabled = true;
    el('origNext').disabled = true;
  }

  // Largura da folha na tela, em px de CSS.
  function sheetWidth(widthPt) {
    const zoom = el('origZoom').value;
    if (zoom === 'fit') {
      const available = el('origScroll').clientWidth - 2 * 20;
      return Math.max(160, available);
    }
    return widthPt * CSS_PX_PER_POINT * Number(zoom);
  }

  // A menor resolução que ainda fica nítida na tela (inclusive Retina).
  function dpiFor(widthPt, cssWidth) {
    const needed = (cssWidth * (window.devicePixelRatio || 1) * 72) / widthPt;
    return DPIS.find((dpi) => dpi >= needed) || DPIS[DPIS.length - 1];
  }

  async function render() {
    if (!visible()) return;
    if (!state.project) return;
    const info = await loadSource();
    if (!info) {
      empty('Não foi possível ler o arquivo original.');
      return;
    }
    if (info.kind !== 'pdf' || !info.pages.length) {
      empty(info.kind === 'epub'
        ? 'Este livro veio de um EPUB. O original dele é o próprio texto do painel ao lado.'
        : 'O PDF original não está mais na pasta deste projeto.');
      return;
    }

    const total = info.pages.length;
    page = Math.min(Math.max(1, page), total);
    const [widthPt, heightPt] = info.pages[page - 1];
    const cssWidth = sheetWidth(widthPt);

    el('origEmpty').hidden = true;
    const sheet = el('origSheet');
    sheet.hidden = false;
    sheet.style.width = `${cssWidth}px`;
    sheet.style.aspectRatio = `${widthPt} / ${heightPt}`;

    const url = `/api/projects/${info.projectId}/source/pages/${page}?dpi=${dpiFor(widthPt, cssWidth)}`;
    const img = el('origImg');
    if (img.getAttribute('src') !== url) {
      img.setAttribute('src', url);
      img.alt = `Página ${page} do PDF original`;
    }

    el('origPageLabel').textContent = `p. ${page} de ${total}`;
    el('origPrev').disabled = page <= 1;
    el('origNext').disabled = page >= total;

    drawBoxes(widthPt, heightPt);
  }

  function drawBoxes(widthPt, heightPt) {
    const layer = el('origBoxes');
    layer.innerHTML = '';
    for (const { block, boxes } of blocksOnPage(page)) {
      for (const [, x0, top, x1, bottom] of boxes) {
        const box = document.createElement('button');
        box.type = 'button';
        box.className = 'original__box';
        if (block.id === selectedId) box.classList.add('original__box--selected');
        box.style.left = `${(x0 / widthPt) * 100}%`;
        box.style.top = `${(top / heightPt) * 100}%`;
        box.style.width = `${((x1 - x0) / widthPt) * 100}%`;
        box.style.height = `${((bottom - top) / heightPt) * 100}%`;
        const excerpt = (block.text || block.src || '').replace(/<[^>]+>/g, '').slice(0, 60);
        box.title = excerpt ? `Ir para: ${excerpt}` : 'Ir para este bloco';
        box.setAttribute('aria-label', box.title);
        box.onclick = () => {
          selectedId = block.id;
          goToBlock(block.id);
          render();
        };
        layer.append(box);
      }
    }
  }

  // Rola a página até o retângulo destacado, sem mexer no resto da tela.
  function revealSelected() {
    const mark = document.querySelector('.original__box--selected');
    if (!mark) return;
    const scroller = el('origScroll');
    const markTop = mark.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
    const target = scroller.scrollTop + markTop - scroller.clientHeight / 3;
    scroller.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
  }

  /* --------------------------------------------------------- eventos */

  // O usuário escolheu um bloco no texto (clicou ou entrou num campo dele).
  function selectFromEditor(event) {
    const wrapper = event.target.closest?.('#editorInner .block[data-id]');
    if (!wrapper) return;
    const id = wrapper.dataset.id;
    if (id === selectedId) return;
    selectedId = id;
    if (!el('origFollow').checked) {
      if (visible()) render();
      return;
    }
    const target = firstPageOf(blockById(id));
    if (target) page = target;
    if (visible()) render().then(revealSelected);
  }

  // Seguindo a seleção sem nada selecionado na tela, a página acompanha a
  // rolagem do texto.
  function followScroll() {
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      if (!el('origFollow').checked || !visible()) return;
      const selected = selectedId && document.querySelector(`#editorInner .block[data-id="${selectedId}"]`);
      if (selected) {
        const rect = selected.getBoundingClientRect();
        const view = el('editor').getBoundingClientRect();
        if (rect.bottom > view.top && rect.top < view.bottom) return; // ainda à vista
      }
      const shown = visiblePage();
      if (shown && shown !== page) {
        page = shown;
        render();
      }
    }, 150);
  }

  function step(delta) {
    page += delta;
    render();
    el('origScroll').scrollTop = 0;
  }

  // Livro novo ou bancada fechada: esquece a página e a seleção.
  function reset() {
    source = null;
    selectedId = null;
    page = 1;
    el('origImg').removeAttribute('src');
    el('origBoxes').innerHTML = '';
  }

  function wire() {
    const editor = el('editor');
    editor.addEventListener('focusin', selectFromEditor);
    editor.addEventListener('click', selectFromEditor);
    editor.addEventListener('scroll', followScroll, { passive: true });
    el('origPrev').onclick = () => step(-1);
    el('origNext').onclick = () => step(1);
    el('origZoom').onchange = () => render();

    // Redesenha quando o painel aparece (troca de aba, abrir pelo menu
    // Janela) ou muda de tamanho (no zoom "ajustar à largura").
    let lastWidth = 0;
    new ResizeObserver(() => {
      // Escondido, a largura vai a zero: guardar o zero faz a volta, mesmo
      // com a largura de antes, contar como mudança.
      const width = el('origScroll').clientWidth;
      if (width === lastWidth) return;
      lastWidth = width;
      if (width) render();
    }).observe(el('origScroll'));
  }

  wire();

  return {
    reset,
    // Para o app.js chamar quando o livro muda por dentro (juntar blocos,
    // trocar de capítulo): as caixas são relidas de state.doc.
    refresh: () => { if (visible()) render(); },
  };
})();
