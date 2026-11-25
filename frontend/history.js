/* Interlinear — desfazer e refazer da formatação.

   Guarda as mudanças de formatação feitas na bancada: ajuste de bloco
   (painéis Parágrafo e Caractere, alça de espaço), estilo escolhido para um
   bloco, mudança num estilo do livro e o conta-gotas. Cada entrada sabe se
   desfazer e se refazer (as duas gravam no servidor, como qualquer edição).

   O texto não entra aqui: dentro de um campo, ⌘Z é o desfazer do próprio
   navegador, que conhece cada letra digitada. Estrutura (inserir, apagar,
   juntar) tem o "Desfazer" do aviso que aparece na hora.

   Quem registra: typography.js e spacing.js. Quem chama undo/redo: tools.js
   (⌘Z, ⌘⇧Z e os botões da barra de ferramentas).
*/

'use strict';

const History = (() => {
  const LIMIT = 100;
  let past = [];
  let future = [];
  // Enquanto desfaz ou refaz, as gravações não viram entradas novas.
  let replaying = false;
  const listeners = new Set();

  function notify() {
    for (const listener of listeners) listener();
  }

  /* entry: { label, undo: async () => bool, redo: async () => bool } */
  function record(entry) {
    if (replaying) return;
    past.push(entry);
    if (past.length > LIMIT) past.shift();
    future = [];
    notify();
  }

  async function step(from, to, action, verb) {
    const entry = from.pop();
    if (!entry) {
      toast(verb === 'undo' ? 'Nada para desfazer.' : 'Nada para refazer.');
      notify();
      return;
    }
    replaying = true;
    let ok = false;
    try {
      ok = await entry[action]();
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      replaying = false;
    }
    if (ok) {
      to.push(entry);
      toast(`${verb === 'undo' ? 'Desfeito' : 'Refeito'}: ${entry.label}`);
    }
    // Se falhou (o bloco foi apagado ou juntado, por exemplo), a entrada sai.
    notify();
  }

  return {
    record,
    undo: () => step(past, future, 'undo', 'undo'),
    redo: () => step(future, past, 'redo', 'redo'),
    clear() {
      past = [];
      future = [];
      notify();
    },
    nextUndo: () => past[past.length - 1]?.label || null,
    nextRedo: () => future[future.length - 1]?.label || null,
    onChange: (listener) => listeners.add(listener),
    get replaying() { return replaying; },
  };
})();
