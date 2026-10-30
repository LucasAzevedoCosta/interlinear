# Licenças das dependências

Resumo: o Interlinear pode ser MIT porque todas as dependências são MIT ou BSD. Essa
não é a escolha óbvia neste domínio, e vale explicar por quê — para que ninguém
troque uma biblioteca por outra sem perceber o efeito.

## O que está em uso

| Biblioteca       | Licença | Para quê                     |
| ---------------- | ------- | ---------------------------- |
| `pdfminer.six`   | MIT     | Extração de texto de PDF     |
| `lxml`           | BSD     | Leitura de XHTML e OPF       |
| `fastapi`        | MIT     | Servidor HTTP                |
| `uvicorn`        | BSD     | Execução do servidor         |
| `httpx`          | BSD     | Cliente dos provedores de IA |
| `python-multipart` | Apache-2.0 | Upload de arquivos     |

A escrita de EPUB não usa biblioteca nenhuma: o pacote é montado com o
`zipfile` da biblioteca padrão. Foi decisão consciente, pelos dois motivos
abaixo.

## A armadilha do AGPL

As duas bibliotecas mais populares para este tipo de trabalho são **AGPL-3.0**:

- **PyMuPDF** (`fitz`) — extração de PDF. É mais rápida e mais precisa que
  `pdfminer.six`, especialmente em layouts complicados.
- **EbookLib** — leitura e escrita de EPUB. Resolveria boa parte de
  `epub_write.py` e `epub_read.py`.

A AGPL é uma licença copyleft forte: um projeto que depende delas precisa ser
distribuído sob AGPL também. Para um aplicativo local isso costuma ser aceitável,
mas tem duas consequências concretas:

1. **Ninguém pode usar seu código num produto fechado.** Isso elimina boa parte
   das contribuições vindas de empresas.
2. **A cláusula de rede.** Se alguém hospedar o Interlinear como serviço, precisa
   oferecer o código-fonte aos usuários. Se um dia você quiser oferecer uma
   versão hospedada paga, a AGPL molda esse caminho.

Nada disso é ruim em si — a AGPL existe exatamente para garantir essas coisas.
Mas é uma decisão de projeto, não um detalhe técnico, e merece ser tomada de
propósito.

## Se você quiser trocar

`pdfminer.six` é bom, mas o PyMuPDF é melhor em PDFs difíceis. Se a qualidade da
extração virar o gargalo, a troca vale a pena. O isolamento em
`core/pdf_extract.py` existe para isso: só aquele arquivo precisa mudar, porque
o resto do sistema conhece apenas a lista de `Block`.

Ao fazer a troca, **mude a licença do projeto para AGPL-3.0** e deixe isso
explícito no README. Não é opcional.

Um meio-termo possível: manter os dois extratores e escolher em tempo de
execução, distribuindo o PyMuPDF como extra opcional. Funciona, mas a fronteira
legal de "extra opcional" é discutível, então na dúvida consulte alguém que
entenda do assunto antes de distribuir assim.

## Fontes

A interface carrega a **Literata** do Google Fonts, sob SIL Open Font License,
que permite uso e redistribuição livres. Para que o app funcione sem internet,
baixe os arquivos da fonte para `frontend/assets/` e troque o `@import` no topo
de `styles.css` por uma regra `@font-face` local.
