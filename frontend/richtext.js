/* Interlinear — campo de texto formatado (contenteditable).

   O livro guarda cada parágrafo como texto com algumas tags inline:
   <em>, <strong>, <sup>, <sub>, <small> e <br> (as mesmas que o
   inline_html do servidor deixa passar). Antes, o editor mostrava esse texto
   cru num <textarea>, com as tags à vista. Este campo mostra o texto
   formatado e devolve o mesmo formato:

     texto do livro --inlineHtml()--> HTML do campo --serialize()--> texto do livro

   Regras para o formato não se perder nem ganhar lixo:
     - só as tags permitidas saem do campo; <b> vira <strong>, <i> vira <em>;
       qualquer outra coisa (span, font, div…) some e fica o texto de dentro;
     - colar e arrastar para dentro entram sempre como texto puro;
     - Enter não faz nada (um parágrafo é um bloco: para outro, use o "+"
       entre os blocos); Shift+Enter põe uma quebra de linha (<br>);
     - quebra de linha no texto (o que o Shift+Enter põe com pre-wrap, ou o
       que um texto antigo tenha) vira <br>, para sair no livro como aparece;
     - white-space: pre-wrap no campo, para o navegador não trocar espaços
       por espaço sem quebra enquanto se digita.

   A tabela continua num <textarea> (app.js): o texto dela tem estrutura
   (" | " e quebras de linha), não formatação.

   Usa inlineHtml, do app.js, carregado antes.
*/

'use strict';

const RichText = (() => {
  const ALLOWED = new Set(['em', 'strong', 'sup', 'sub', 'small', 'br']);
  const ALIASES = { b: 'strong', i: 'em' };
  const BLOCKS = new Set(['div', 'p', 'li']);

  // O DOM do campo de volta para o texto do livro.
  function serialize(root) {
    let out = '';
    const walk = (node, first) => {
      for (const child of node.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          // Com pre-wrap, a quebra de linha aparece na tela; no XHTML ela
          // seria só um espaço. Vira <br>: o que se vê é o que sai.
          out += child.nodeValue.replace(/\r?\n/g, '<br>');
          continue;
        }
        if (child.nodeType !== Node.ELEMENT_NODE) continue;
        let tag = child.tagName.toLowerCase();
        tag = ALIASES[tag] || tag;
        if (tag === 'br') {
          out += '<br>';
        } else if (ALLOWED.has(tag)) {
          out += `<${tag}>`;
          walk(child, false);
          out += `</${tag}>`;
        } else {
          // Um <div> que o navegador criou vira quebra de linha, não some.
          if (BLOCKS.has(tag) && out && !first) out += '<br>';
          walk(child, false);
        }
      }
    };
    walk(root, true);
    return tidy(out);
  }

  // Tira o que não muda nada: tag vazia, </em><em> colado, <br> no fim (o
  // navegador deixa um quando o campo fica vazio).
  function tidy(text) {
    let previous;
    do {
      previous = text;
      text = text
        .replace(/<(em|strong|sup|sub|small)><\/\1>/g, '')
        .replace(/<\/(em|strong|sup|sub|small)><\1>/g, '');
    } while (text !== previous);
    return text.replace(/(<br>)+$/, '');
  }

  function setValue(field, text) {
    field.innerHTML = inlineHtml(text || '');
    field.dataset.empty = String(!(text || '').trim());
  }

  function insertPlain(text) {
    // insertText mantém o desfazer (⌘Z) do próprio campo.
    if (!document.execCommand('insertText', false, text)) {
      const selection = window.getSelection();
      if (!selection.rangeCount) return;
      const range = selection.getRangeAt(0);
      range.deleteContents();
      range.insertNode(document.createTextNode(text));
      range.collapse(false);
    }
  }

  // Formatação no trecho selecionado. Devolve false se não havia seleção num
  // campo formatado.
  function format(command) {
    const selection = window.getSelection();
    const field = selection.anchorNode?.parentElement?.closest('.block__rich')
      || (selection.anchorNode?.classList?.contains('block__rich') ? selection.anchorNode : null);
    if (!field || field.contentEditable !== 'true') return false;
    document.execCommand('styleWithCSS', false, false); // <b>, nunca <span style>
    document.execCommand(command, false, null);
    return true;
  }

  // Com a ferramenta Selecionar (tools.js) o texto não se edita: um clique só
  // escolhe o bloco, para mexer no estilo sem mudar o texto sem querer.
  const editing = () => document.getElementById('editor')?.dataset.tool !== 'select';

  function setEditable(on) {
    for (const element of document.querySelectorAll('#editorInner .block__rich')) {
      if (element.dataset.readonly !== 'true') element.contentEditable = on ? 'true' : 'false';
    }
  }

  /* options: { value, placeholder, label, className, readOnly, onInput(text), onBlur() } */
  function field(options) {
    const element = document.createElement('div');
    element.className = `block__rich ${options.className || ''}`.trim();
    if (options.readOnly) element.dataset.readonly = 'true';
    element.contentEditable = !options.readOnly && editing() ? 'true' : 'false';
    element.spellcheck = true;
    element.setAttribute('role', 'textbox');
    element.setAttribute('aria-multiline', 'true');
    if (options.label) element.setAttribute('aria-label', options.label);
    if (options.placeholder) element.dataset.placeholder = options.placeholder;
    setValue(element, options.value);

    element.addEventListener('input', () => {
      const text = serialize(element);
      element.dataset.empty = String(!text.trim());
      options.onInput?.(text);
    });
    element.addEventListener('blur', () => options.onBlur?.());

    element.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (event.shiftKey && !(event.metaKey || event.ctrlKey)) {
          document.execCommand('insertLineBreak');
        }
        return;
      }
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      // Sublinhado não existe no livro: o navegador sublinharia e a tag
      // sumiria ao salvar.
      if (key === 'u') { event.preventDefault(); return; }
      if (key === 'b' || key === 'i') {
        event.preventDefault();
        format(key === 'b' ? 'bold' : 'italic');
      }
    });

    element.addEventListener('paste', (event) => {
      event.preventDefault();
      const text = event.clipboardData?.getData('text/plain') || '';
      // Quebras de linha coladas viram espaço, como no XHTML.
      insertPlain(text.replace(/\s*\r?\n\s*/g, ' '));
    });
    element.addEventListener('drop', (event) => {
      event.preventDefault();
      const text = event.dataTransfer?.getData('text/plain') || '';
      if (text) insertPlain(text.replace(/\s*\r?\n\s*/g, ' '));
    });

    element.getValue = () => serialize(element);
    element.setValue = (text) => setValue(element, text);
    return element;
  }

  return { field, serialize, format, tidy, setEditable };
})();
