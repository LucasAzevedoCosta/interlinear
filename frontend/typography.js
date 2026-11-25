/* Interlinear — painéis de tipografia: Propriedades, Parágrafo, Caractere e
   Estilos de parágrafo.

   Funcionam sobre o bloco selecionado no texto (clicar ou entrar num campo
   dele) e usam as rotas da Fase 3 (core/styles.py no servidor):
     - estilo do bloco e ajuste do bloco: PATCH /blocks com style/overrides;
     - os estilos do livro: GET/POST /styles, PATCH/DELETE /styles/{id}.

   O servidor valida tudo. Aqui os controles são montados a partir do schema
   que ele manda (tipo, faixa, rótulo de cada propriedade), para os dois lados
   não discordarem sobre o que é permitido.

   O ajuste do bloco tem envio próprio, e não a fila de salvamento do app.js:
   aquela junta os campos de um bloco substituindo os anteriores, e dois
   ajustes seguidos (recuo, depois alinhamento) se perderiam.

   Lê do app.js: state, api, toast, refreshPreview, blockById, KIND_LABELS.
*/

'use strict';

const Typography = (() => {
  const HEADINGS = new Set(['heading1', 'heading2', 'heading3']);
  // Mesmo conjunto que STYLED_KINDS em core/styles.py.
  const STYLED = new Set(['paragraph', 'heading1', 'heading2', 'heading3', 'quote', 'list_item']);
  // Propriedades de cada painel. As que não estão aqui não aparecem.
  const PARAGRAPH_PROPS = [
    'align', 'indent', 'line_height', 'space_before', 'space_after',
    'margin_left', 'margin_right', 'page_break_before',
  ];
  const CHARACTER_PROPS = ['font_family', 'font_size', 'bold', 'italic', 'small_caps'];
  const ALIGN_ICONS = {
    left: 'M2 3h12M2 6.5h8M2 10h12M2 13.5h8',
    center: 'M2 3h12M4 6.5h8M2 10h12M4 13.5h8',
    right: 'M2 3h12M6 6.5h8M2 10h12M6 13.5h8',
    justify: 'M2 3h12M2 6.5h12M2 10h12M2 13.5h12',
  };
  const ALIGN_LABELS = { left: 'À esquerda', center: 'Centralizar', right: 'À direita', justify: 'Justificar' };
  const FAMILY_LABELS = { serif: 'Com serifa', 'sans-serif': 'Sem serifa', monospace: 'Monoespaçada' };
  // Tags do painel Caractere: as mesmas que a extração e o inline_html usam.
  const INLINE_TAGS = /<\/?(em|strong|i|b|sup|sub|small)>/gi;

  const el = (id) => document.getElementById(id);

  let styles = [];      // describe() do servidor
  let schema = {};      // nome -> spec
  let loadedFor = null; // id do projeto
  let selectedId = null;
  let editingStyle = null; // id do estilo aberto no painel de estilos
  let lastField = null;    // último campo de texto em que o usuário esteve

  /* ----------------------------------------------------------- dados */

  async function load(force = false) {
    const projectId = state.project?.id;
    if (!projectId) return;
    if (!force && loadedFor === projectId) return;
    try {
      const data = await api(`/api/projects/${projectId}/styles`);
      if (state.project?.id !== projectId) return;
      styles = data.styles;
      schema = Object.fromEntries(data.schema.map((spec) => [spec.name, spec]));
      loadedFor = projectId;
    } catch (error) {
      toast(`Não foi possível ler os estilos: ${error.message}`, 'error');
    }
  }

  const styleById = (id) => styles.find((s) => s.id === id);

  function selectedBlock() {
    return selectedId ? blockById(selectedId) : null;
  }

  function chapterOf(block) {
    return state.doc?.chapters.find((c) => c.blocks.includes(block)) || null;
  }

  // O estilo automático, como book_styles.default_style no servidor: o
  // parágrafo logo depois de um título é "Primeiro parágrafo".
  function autoStyle(block) {
    if (block.kind !== 'paragraph') return block.kind;
    const chapter = chapterOf(block);
    if (!chapter) return 'paragraph';
    const translated = state.stage === 'translate' && state.previewTranslated;
    for (let i = chapter.blocks.indexOf(block) - 1; i >= 0; i -= 1) {
      const previous = chapter.blocks[i];
      if (previous.kind === 'footnote') continue;
      if (previous.kind === 'image' && !previous.src) continue;
      if (previous.kind !== 'image' && previous.kind !== 'page_break') {
        const text = translated ? (previous.translated ?? previous.text) : previous.text;
        if (!(text || '').trim()) continue; // bloco vazio não entra no XHTML
      }
      return HEADINGS.has(previous.kind) ? 'first_paragraph' : 'paragraph';
    }
    return 'paragraph';
  }

  function styleOf(block) {
    const chosen = block.meta?.style;
    return chosen && styleById(chosen) ? chosen : autoStyle(block);
  }

  function overridesOf(block) {
    const value = block.meta?.overrides;
    return value && typeof value === 'object' ? value : {};
  }

  function effectiveOf(block) {
    return { ...(styleById(styleOf(block))?.props || {}), ...overridesOf(block) };
  }

  /* --------------------------------------------------------- gravação */

  // Muda o ajuste do bloco: só o enviado; null numa propriedade a tira; null
  // no lugar do objeto limpa o ajuste inteiro.
  async function setOverrides(block, changes) {
    try {
      await api(`/api/projects/${state.project.id}/blocks`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates: [{ id: block.id, overrides: changes }] }),
      });
    } catch (error) {
      toast(error.message, 'error');
      renderAll();
      return;
    }
    let merged = changes === null ? {} : { ...overridesOf(block) };
    for (const [name, value] of Object.entries(changes || {})) {
      if (value === null) delete merged[name];
      else merged[name] = value;
    }
    const { overrides: _, ...meta } = block.meta || {};
    block.meta = Object.keys(merged).length ? { ...meta, overrides: merged } : meta;
    afterChange();
  }

  async function setBlockStyle(block, styleId) {
    try {
      await api(`/api/projects/${state.project.id}/blocks`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates: [{ id: block.id, style: styleId }] }),
      });
    } catch (error) {
      toast(error.message, 'error');
      return;
    }
    const { style: _, ...meta } = block.meta || {};
    block.meta = styleId ? { ...meta, style: styleId } : meta;
    afterChange();
  }

  async function styleRequest(path, options) {
    try {
      const data = await api(`/api/projects/${state.project.id}/styles${path}`, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      if (data.styles) styles = data.styles;
      return data;
    } catch (error) {
      toast(error.message, 'error');
      return null;
    }
  }

  async function updateStyle(styleId, body) {
    if (await styleRequest(`/${encodeURIComponent(styleId)}`, {
      method: 'PATCH', body: JSON.stringify(body),
    })) afterChange();
  }

  async function createStyleFrom(base) {
    const name = (window.prompt(`Nome do estilo novo, a partir de “${base.name}”:`) || '').trim();
    if (!name) return;
    // Estilo novo parte de um pronto; a partir de um do usuário, leva as
    // mudanças dele junto.
    const body = base.builtin
      ? { name, based_on: base.id }
      : { name, based_on: base.based_on, props: base.changes };
    const data = await styleRequest('', { method: 'POST', body: JSON.stringify(body) });
    if (!data) return;
    editingStyle = data.id;
    const block = selectedBlock();
    if (block && STYLED.has(block.kind)) await setBlockStyle(block, data.id);
    else afterChange();
  }

  async function removeStyle(style) {
    const question = style.builtin
      ? `Voltar “${style.name}” ao padrão? As mudanças feitas nele somem.`
      : `Apagar o estilo “${style.name}”? Os blocos que o usam voltam ao estilo automático.`;
    if (!window.confirm(question)) return;
    const data = await styleRequest(`/${encodeURIComponent(style.id)}`, { method: 'DELETE' });
    if (!data) return;
    if (!style.builtin) {
      for (const chapter of state.doc.chapters) {
        for (const block of chapter.blocks) {
          if (block.meta?.style === style.id) {
            const { style: _, ...meta } = block.meta;
            block.meta = meta;
          }
        }
      }
      if (editingStyle === style.id) editingStyle = null;
    }
    afterChange();
  }

  async function renameStyle(style) {
    const name = (window.prompt('Novo nome do estilo:', style.name) || '').trim();
    if (name && name !== style.name) updateStyle(style.id, { name });
  }

  function afterChange() {
    renderAll();
    refreshPreview();
  }

  /* -------------------------------------------------------- controles */

  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node[key] = value;
      else if (key === 'text') node.textContent = value;
      else node.setAttribute(key, value === true ? '' : value);
    }
    node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
    return node;
  }

  const formatNumber = (value) => String(value).replace('.', ',');

  function parseNumber(text) {
    const value = Number(String(text).trim().replace(',', '.'));
    return Number.isFinite(value) ? value : null;
  }

  // Um controle por propriedade. `changed` marca o que difere do estilo (ou
  // da base); `onChange(valor)` grava; `onReset()` tira a mudança.
  function control(name, value, { changed, onChange, onReset }) {
    const spec = schema[name];
    if (!spec) return null;
    const id = `tp-${name}-${Math.random().toString(36).slice(2, 7)}`;
    const reset = changed
      ? h('button', {
          class: 'tfield__reset', type: 'button', title: 'Voltar ao valor do estilo',
          'aria-label': `Voltar ${spec.label} ao valor do estilo`, onclick: onReset,
        }, '↺')
      : null;
    const label = h('label', { class: 'tfield__label', for: id },
      changed ? h('span', { class: 'tfield__dot', 'aria-label': 'ajustado' }, '●') : null,
      spec.label);

    if (spec.type === 'bool') {
      const box = h('input', { type: 'checkbox', id });
      box.checked = Boolean(value);
      box.onchange = () => onChange(box.checked);
      return h('div', { class: 'tfield tfield--bool' }, box, label, reset);
    }

    if (name === 'align') {
      const group = h('div', { class: 'tseg', role: 'group', 'aria-label': spec.label });
      for (const option of spec.options) {
        const svg = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><path d="${ALIGN_ICONS[option]}"/></svg>`;
        const button = h('button', {
          type: 'button', title: ALIGN_LABELS[option], 'aria-label': ALIGN_LABELS[option],
          'aria-pressed': String(value === option), onclick: () => onChange(option),
        });
        button.innerHTML = svg;
        group.append(button);
      }
      return h('div', { class: 'tfield tfield--wide' },
        h('span', { class: 'tfield__label' }, changed ? h('span', { class: 'tfield__dot' }, '●') : null, spec.label),
        h('div', { class: 'tfield__row' }, group, reset));
    }

    if (spec.type === 'choice') {
      const select = h('select', { id });
      select.append(h('option', { value: '', text: 'Padrão do leitor' }));
      for (const option of spec.options) {
        select.append(h('option', { value: option, text: FAMILY_LABELS[option] || option }));
      }
      select.value = value || '';
      select.onchange = () => (select.value ? onChange(select.value) : onReset());
      return h('div', { class: 'tfield' }, label, h('div', { class: 'tfield__row' }, select, reset));
    }

    // Número: texto com vírgula ou ponto, gravado ao sair do campo ou no Enter.
    const input = h('input', {
      id, type: 'text', inputmode: 'decimal', class: 'tfield__number',
      title: `Entre ${formatNumber(spec.min)} e ${formatNumber(spec.max)}${spec.unit ? ` ${spec.unit}` : ''}`,
    });
    input.value = value === undefined ? '' : formatNumber(value);
    const commit = () => {
      const parsed = parseNumber(input.value);
      if (parsed === null || parsed === value) {
        input.value = value === undefined ? '' : formatNumber(value);
        return;
      }
      onChange(parsed);
    };
    input.onchange = commit;
    input.onkeydown = (event) => {
      if (event.key === 'Enter') { event.preventDefault(); commit(); }
      if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
        event.preventDefault();
        const step = event.shiftKey ? 1 : 0.1;
        const current = parseNumber(input.value) ?? value ?? 0;
        const next = Math.round((current + (event.key === 'ArrowUp' ? step : -step)) * 100) / 100;
        input.value = formatNumber(Math.min(spec.max, Math.max(spec.min, next)));
      }
    };
    return h('div', { class: 'tfield' }, label,
      h('div', { class: 'tfield__row' }, input, spec.unit ? h('span', { class: 'tfield__unit', text: spec.unit }) : null, reset));
  }

  function blockControls(block, names) {
    const effective = effectiveOf(block);
    const overrides = overridesOf(block);
    const grid = h('div', { class: 'tgrid' });
    for (const name of names) {
      const field = control(name, effective[name], {
        changed: name in overrides,
        onChange: (value) => setOverrides(block, { [name]: value }),
        onReset: () => setOverrides(block, { [name]: null }),
      });
      if (field) grid.append(field); // sem schema (falha ao ler os estilos): nada
    }
    return grid;
  }

  function placeholder(panel, message) {
    panel.replaceChildren(h('p', { class: 'tpanel__empty', text: message }));
  }

  // O bloco selecionado pode receber estilo? Se não, o motivo.
  function blockProblem(block) {
    if (!state.project) return 'Abra um livro.';
    if (!block) return 'Clique num bloco do texto para ver e mudar a formatação dele.';
    if (!STYLED.has(block.kind)) {
      const name = KIND_LABELS[block.kind]
        || { image: 'Imagem', page_break: 'Quebra de página' }[block.kind]
        || 'Este bloco';
      return `${name} não usa estilo de parágrafo.`;
    }
    return null;
  }

  function styleLabel(block) {
    const style = styleById(styleOf(block));
    const name = style?.name || styleOf(block);
    return Object.keys(overridesOf(block)).length ? `${name} +` : name;
  }

  function describeOverrides(block) {
    return Object.entries(overridesOf(block)).map(([name, value]) => {
      const spec = schema[name];
      if (!spec) return name;
      if (spec.type === 'bool') return `${spec.label.toLowerCase()} ${value ? 'sim' : 'não'}`;
      if (name === 'align') return `${spec.label.toLowerCase()}: ${ALIGN_LABELS[value].toLowerCase()}`;
      if (name === 'font_family') return `${spec.label.toLowerCase()}: ${FAMILY_LABELS[value]}`;
      return `${spec.label.toLowerCase()} ${formatNumber(value)}${spec.unit ? ` ${spec.unit}` : ''}`;
    });
  }

  /* ---------------------------------------------------- os painéis */

  function renderProps() {
    const panel = el('propsPanel');
    const block = selectedBlock();
    const problem = blockProblem(block);
    if (problem) { placeholder(panel, problem); return; }

    const select = h('select', { id: 'tpStyleSelect' });
    select.append(h('option', {
      value: '', text: `Automático (${styleById(autoStyle(block))?.name || autoStyle(block)})`,
    }));
    for (const style of styles) select.append(h('option', { value: style.id, text: style.name }));
    select.value = block.meta?.style && styleById(block.meta.style) ? block.meta.style : '';
    select.onchange = () => setBlockStyle(block, select.value || null);

    const adjustments = describeOverrides(block);
    const page = block.meta?.boxes?.[0]?.[0] || block.page;
    panel.replaceChildren(
      h('p', { class: 'tpanel__meta' },
        `${KIND_LABELS[block.kind] || block.kind}${page ? ` · p. ${page}` : ''}`),
      h('div', { class: 'tfield' },
        h('label', { class: 'tfield__label', for: 'tpStyleSelect' }, 'Estilo de parágrafo'),
        select),
      h('p', { class: 'tpanel__style-name' }, 'Aparece como ', h('b', { text: styleLabel(block) })),
      h('div', { class: 'tpanel__section' },
        h('p', { class: 'tpanel__label', text: 'Ajustes só deste bloco' }),
        adjustments.length
          ? h('ul', { class: 'tpanel__list' }, ...adjustments.map((text) => h('li', { text })))
          : h('p', { class: 'tpanel__hint', text: 'Nenhum: o bloco segue o estilo.' }),
        adjustments.length
          ? h('button', { class: 'btn', type: 'button', onclick: () => setOverrides(block, null) }, 'Limpar ajuste')
          : null),
      h('p', { class: 'tpanel__hint',
        text: 'Mude o recuo, o espaçamento e o alinhamento no painel Parágrafo, e a fonte no Caractere. O que você muda ali vale só para este bloco.' }),
    );
  }

  function renderParagraph() {
    const panel = el('paraPanel');
    const block = selectedBlock();
    const problem = blockProblem(block);
    if (problem) { placeholder(panel, problem); return; }
    panel.replaceChildren(
      h('p', { class: 'tpanel__meta' }, 'Bloco: ', h('b', { text: styleLabel(block) })),
      blockControls(block, PARAGRAPH_PROPS),
      h('p', { class: 'tpanel__hint',
        text: '● ajustado só neste bloco; ↺ volta ao valor do estilo. Setas ↑↓ mudam 0,1 (com Shift, 1).' }),
    );
  }

  function renderCharacter() {
    const panel = el('charPanel');
    const inline = h('div', { class: 'tpanel__section' },
      h('p', { class: 'tpanel__label', text: 'No trecho selecionado' }),
      h('div', { class: 'ttools', role: 'toolbar', 'aria-label': 'Formatação do trecho' },
        tool('N', 'Negrito (⌘B)', () => toggleTag('strong'), 'font-weight:700'),
        tool('I', 'Itálico (⌘I)', () => toggleTag('em'), 'font-style:italic'),
        tool('x²', 'Sobrescrito', () => toggleTag('sup')),
        tool('x₂', 'Subscrito', () => toggleTag('sub')),
        tool('⌫', 'Tirar a formatação do trecho', clearTags)),
      h('p', { class: 'tpanel__hint',
        text: 'Selecione um trecho num campo de texto e clique. Clicar de novo desfaz.' }));

    const block = selectedBlock();
    const problem = blockProblem(block);
    const whole = problem
      ? h('p', { class: 'tpanel__hint', text: problem })
      : blockControls(block, CHARACTER_PROPS);
    panel.replaceChildren(inline, h('div', { class: 'tpanel__section' },
      h('p', { class: 'tpanel__label', text: 'No parágrafo inteiro' }), whole));
  }

  function tool(text, title, action, style) {
    const button = h('button', { class: 'ttool', type: 'button', title, 'aria-label': title, style });
    button.textContent = text;
    // Sem isto o clique tira o foco do campo e a seleção de texto se perde.
    button.onmousedown = (event) => event.preventDefault();
    button.onclick = action;
    return button;
  }

  function renderStyles() {
    const panel = el('stylesPanel');
    if (!state.project) { placeholder(panel, 'Abra um livro.'); return; }
    const block = selectedBlock();
    const applicable = block && STYLED.has(block.kind);
    const current = applicable ? styleOf(block) : null;

    const list = h('div', { class: 'tstyles', role: 'list' });
    for (const style of styles) {
      const changed = Object.keys(style.changes || {}).length > 0;
      const row = h('div', { class: 'tstyle', role: 'listitem', 'data-current': String(style.id === current) },
        h('button', {
          class: 'tstyle__apply', type: 'button',
          title: applicable ? 'Aplicar ao bloco selecionado' : 'Selecione um bloco no texto para aplicar',
          disabled: !applicable,
          onclick: () => setBlockStyle(block, style.id === autoStyle(block) ? null : style.id),
        },
        h('span', { class: 'tstyle__name', text: style.name }),
        changed ? h('span', { class: 'tstyle__flag', title: style.builtin ? 'Mudado neste livro' : 'Diferente da base', text: '●' }) : null,
        style.builtin ? null : h('span', { class: 'tstyle__tag', text: 'seu' })),
        h('button', {
          class: 'tstyle__edit', type: 'button', 'aria-expanded': String(editingStyle === style.id),
          title: `Editar “${style.name}”`, 'aria-label': `Editar o estilo ${style.name}`,
          onclick: () => { editingStyle = editingStyle === style.id ? null : style.id; renderStyles(); },
        }, '✎'));
      list.append(row);
      if (editingStyle === style.id) list.append(styleEditor(style));
    }
    panel.replaceChildren(
      h('p', { class: 'tpanel__hint',
        text: applicable
          ? 'Clique num estilo para aplicá-lo ao bloco selecionado. ✎ muda o estilo no livro todo.'
          : '✎ muda o estilo no livro todo. Para aplicar um estilo, selecione um bloco no texto.' }),
      list,
    );
  }

  function styleEditor(style) {
    const changes = style.changes || {};
    const grid = h('div', { class: 'tgrid' });
    for (const name of [...PARAGRAPH_PROPS, ...CHARACTER_PROPS]) {
      const field = control(name, style.props[name], {
        changed: name in changes,
        onChange: (value) => updateStyle(style.id, { props: { [name]: value } }),
        onReset: () => updateStyle(style.id, { props: { [name]: null } }),
      });
      if (field) grid.append(field);
    }
    return h('div', { class: 'tstyle__editor' },
      h('p', { class: 'tpanel__hint',
        text: `Vale para todo bloco com “${style.name}”, no livro inteiro.${style.builtin ? '' : ' ● diferente da base.'}` }),
      grid,
      h('div', { class: 'tstyle__actions' },
        h('button', { class: 'btn', type: 'button', onclick: () => createStyleFrom(style) }, 'Novo a partir deste'),
        style.builtin ? null : h('button', { class: 'btn', type: 'button', onclick: () => renameStyle(style) }, 'Renomear'),
        style.builtin && !Object.keys(changes).length ? null
          : h('button', { class: 'btn btn--quiet', type: 'button', onclick: () => removeStyle(style) },
            style.builtin ? 'Voltar ao padrão' : 'Apagar')));
  }

  function renderAll() {
    renderProps();
    renderParagraph();
    renderCharacter();
    renderStyles();
  }

  /* ------------------------------------------------ trecho do texto */

  function targetField() {
    const active = document.activeElement;
    if (active?.tagName === 'TEXTAREA' && active.closest('#editorInner')) return active;
    if (lastField?.isConnected && !lastField.hidden) return lastField;
    return null;
  }

  function toggleTag(tag) {
    const field = targetField();
    if (!field) {
      toast('Clique no texto para editar e selecione um trecho.');
      return;
    }
    const { selectionStart: start, selectionEnd: end, value } = field;
    if (start === end) {
      toast('Selecione o trecho que quer formatar.');
      return;
    }
    const open = `<${tag}>`;
    const close = `</${tag}>`;
    const selected = value.slice(start, end);
    let next;
    let range;
    if (value.slice(start - open.length, start) === open && value.slice(end, end + close.length) === close) {
      // As tags estão logo fora da seleção: tira.
      next = value.slice(0, start - open.length) + selected + value.slice(end + close.length);
      range = [start - open.length, end - open.length];
    } else if (selected.startsWith(open) && selected.endsWith(close)) {
      // A seleção inclui as tags: tira.
      const inner = selected.slice(open.length, selected.length - close.length);
      next = value.slice(0, start) + inner + value.slice(end);
      range = [start, start + inner.length];
    } else {
      next = value.slice(0, start) + open + selected + close + value.slice(end);
      range = [start + open.length, end + open.length];
    }
    applyText(field, next, range);
  }

  function clearTags() {
    const field = targetField();
    if (!field) {
      toast('Clique no texto para editar e selecione um trecho.');
      return;
    }
    const { selectionStart: start, selectionEnd: end, value } = field;
    const inner = value.slice(start, end).replace(INLINE_TAGS, '');
    applyText(field, value.slice(0, start) + inner + value.slice(end), [start, start + inner.length]);
  }

  // Troca o texto do campo e avisa o app.js como se o usuário tivesse
  // digitado: ele grava pela fila de sempre.
  function applyText(field, text, [start, end]) {
    field.focus();
    field.value = text;
    field.setSelectionRange(start, end);
    field.dispatchEvent(new Event('input'));
  }

  /* --------------------------------------------------------- seleção */

  function select(blockId) {
    if (blockId === selectedId) return;
    selectedId = blockId;
    paintSelection();
    renderAll();
  }

  function paintSelection() {
    for (const node of document.querySelectorAll('#editorInner .block--selected')) {
      node.classList.remove('block--selected');
    }
    if (!selectedId) return;
    document.querySelector(`#editorInner .block[data-id="${selectedId}"]`)?.classList.add('block--selected');
  }

  function wire() {
    const editor = el('editor');
    const pick = (event) => {
      const wrapper = event.target.closest?.('#editorInner .block[data-id]');
      if (wrapper) select(wrapper.dataset.id);
    };
    editor.addEventListener('focusin', (event) => {
      if (event.target.tagName === 'TEXTAREA') lastField = event.target;
      pick(event);
    });
    editor.addEventListener('click', pick);
    // ⌘B e ⌘I nos campos de texto, como em qualquer editor.
    editor.addEventListener('keydown', (event) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
      if (event.target.tagName !== 'TEXTAREA') return;
      const key = event.key.toLowerCase();
      if (key !== 'b' && key !== 'i') return;
      event.preventDefault();
      lastField = event.target;
      toggleTag(key === 'b' ? 'strong' : 'em');
    });
  }

  wire();

  return {
    // Livro novo: esquece seleção e estilos e lê os do livro.
    async reset() {
      selectedId = null;
      editingStyle = null;
      lastField = null;
      loadedFor = null;
      styles = [];
      renderAll();
      await load();
      renderAll();
    },
    // O app.js redesenhou o texto (troca de capítulo, junção, etapa): a
    // seleção pode ter sumido (blocos juntados ganham id novo).
    refresh() {
      if (selectedId && !blockById(selectedId)) selectedId = null;
      paintSelection();
      renderAll();
    },
  };
})();
