# Guia do projeto

Tudo o que foi levantado antes e durante a construção do Interlinear, organizado para
servir de ponto de partida. Se você está abrindo este repositório pela primeira
vez, comece por aqui.

## Onde está cada coisa

| Assunto | Onde |
| ------- | ---- |
| Como rodar, instalar, usar | [`../README.md`](../README.md) |
| Formato EPUB, aparelhos, ferramentas, IA local, decisões técnicas | este arquivo |
| Por que converter PDF é difícil, limitações, papel do preview, custos de IA | [`conversao.md`](conversao.md) |
| Licenças das dependências e a armadilha do AGPL | [`licencas.md`](licencas.md) |

---

## 1. O formato EPUB por dentro

O truque que explica quase tudo: **um EPUB é um arquivo ZIP com um site
dentro**. Renomeie qualquer `.epub` para `.zip`, descompacte, e você encontra
páginas HTML e uma folha de estilo.

Esta é a estrutura que o Interlinear gera:

```
livro.epub
├── mimetype                    "application/epub+zip"
├── META-INF/
│   └── container.xml           aponta: "o manifesto está em OEBPS/content.opf"
└── OEBPS/
    ├── content.opf             metadados + inventário + ordem de leitura
    ├── nav.xhtml               sumário (EPUB 3)
    ├── toc.ncx                 sumário (formato antigo, p/ compatibilidade)
    ├── style.css
    ├── text/
    │   ├── ch0001.xhtml        um capítulo = um arquivo
    │   ├── ch0002.xhtml
    │   └── ...
    └── images/
```

### As duas regras esquisitas

O arquivo `mimetype` precisa ser o **primeiro item do ZIP** e precisa estar
**sem compressão**. É assim que um programa identifica que aquele ZIP é um
livro sem abrir o resto. Errar isso faz vários leitores recusarem o arquivo.

Em `core/epub_write.py` isso aparece como:

```python
epub.writestr(
    zipfile.ZipInfo("mimetype"),
    "application/epub+zip",
    compress_type=zipfile.ZIP_STORED,   # sem compressão, e escrito primeiro
)
```

Há um teste dedicado a isso em `tests/test_pipeline.py`.

### O `content.opf` é o cérebro

Guarda três coisas:

- **Metadados** — título, autor, idioma, identificador único.
- **Manifesto** — a lista de tudo que existe no pacote. Arquivo não listado
  aqui, para o leitor, não existe.
- **Espinha** (`spine`) — a ordem de leitura dos capítulos. A ordem física dos
  arquivos dentro do ZIP não importa; quem manda é a espinha.

### Por que isso muda tudo: o EPUB não tem páginas

Um PDF guarda caracteres em posições fixas numa folha de tamanho definido. Por
isso ele é ruim em tela pequena: você precisa dar zoom e arrastar.

O EPUB guarda só o texto marcado com sua estrutura — *isto é um título, isto é
um parágrafo, isto é uma citação*. As páginas são calculadas pelo leitor, na
hora, conforme o tamanho da tela e da fonte. É por isso que dá para aumentar a
letra num e-reader e o livro se reorganiza sozinho.

Essa diferença é também a raiz de toda a dificuldade de converter PDF para
EPUB — assunto de [`conversao.md`](conversao.md).

---

## 2. Onde o EPUB funciona

Resposta curta, e ela costuma surpreender: **em quase tudo, menos
historicamente no Kindle.**

O EPUB é o padrão aberto da indústria editorial. Leem nativamente: Kobo,
PocketBook, Onyx Boox, Apple Books (iPhone, iPad, Mac), Google Play Livros e
praticamente qualquer app de leitura em Android ou iOS. É também o formato das
bibliotecas públicas e de boa parte das livrarias independentes.

A Amazon manteve formatos próprios (MOBI, depois AZW3 e KFX) para prender o
leitor ao ecossistema dela. Isso mudou em 2022: o **Send to Kindle** passou a
aceitar EPUB, convertendo automaticamente para o formato interno, e o MOBI foi
aposentado em agosto daquele ano.

### Para mandar um livro do Interlinear para um Kindle

Use o **Send to Kindle** — pelo site, pelo e-mail do aparelho ou pelo app de
desktop. **Não** copie o `.epub` por cabo USB: o arquivo até aparece na pasta,
mas o comportamento é inconsistente e às vezes o aparelho simplesmente ignora.

Só funciona com arquivo **sem DRM**, o que é o seu caso, já que o arquivo é
gerado por você. Há um limite de 200 MB por arquivo.

A vantagem de ter o livro em EPUB: se um dia você trocar de aparelho, ele vai
junto. Não fica preso no formato da Amazon.

---

## 3. O ecossistema de ferramentas

Ferramentas prontas que resolvem partes do problema. Vale conhecer, tanto para
usar quanto para saber o que o Interlinear não precisa refazer.

**Calibre** — o canivete suíço dos e-books. Converte entre formatos, gerencia
biblioteca, tem editor de EPUB embutido. Com o plugin **Ebook Translator**
traduz parágrafo a parágrafo preservando a formatação, e nas versões recentes
aceita apontar para um modelo local (Ollama ou a API do LM Studio).

É a opção mais rápida de colocar para funcionar, e para muita gente é
suficiente. A limitação: a entrada de PDF é a parte mais fraca dele, e não há
onde revisar a estrutura antes de exportar.

**Sigil** — editor de EPUB dedicado, gratuito. Mexe no XHTML na mão. Bom para
ajustes finos depois que o arquivo está pronto.

**ebooklib** — biblioteca Python para ler e escrever EPUB. Seria o caminho
óbvio para o Interlinear; não foi usada por motivo de licença (ver seção 5).

**LM Studio** e **Ollama** — rodam modelos de linguagem localmente. O LM Studio
tem interface gráfica e suporte ao formato MLX, otimizado para Mac. O Ollama é
mais leve e por linha de comando. Ambos expõem um servidor local compatível com
o formato da OpenAI, que é como o Interlinear conversa com eles.

### Quando usar o Interlinear em vez do Calibre

O Interlinear existe para o caso em que a conversão **precisa ser revisada**: ele
mostra a estrutura que deduziu, deixa você corrigir parágrafo a parágrafo e
exibe a página final enquanto você trabalha. Para conversão descartável, o
Calibre resolve mais rápido.

---

## 4. Rodar IA local num Mac

Levantamento feito para um MacBook M5 com 16 GB, mas vale para qualquer Apple
Silicon.

### Por que funciona

A memória é **unificada**: a GPU usa a mesma RAM do sistema, então boa parte
dos 16 GB fica disponível para o modelo — na prática uns 10 a 11 GB, com o
resto para o sistema. Em Macs com chip Intel isso não vale, e a lentidão torna
a tradução de um livro inteiro inviável.

O M5 tem aceleradores de IA dentro da GPU, o que deixa o processamento do texto
de entrada mais rápido que nas gerações anteriores.

### Tamanho de modelo

| Tamanho | Ocupa (4-bit) | Velocidade estimada | Para que serve |
| ------- | ------------- | ------------------- | -------------- |
| 7–8 B | ~5 GB | 30–40 tok/s | Rápido, qualidade menor |
| 12–14 B | ~7–9 GB | 18–25 tok/s | Melhor equilíbrio em 16 GB |
| acima de 14 B | >10 GB | — | Começa a usar swap; evite |

As velocidades são estimativas, não medições. Meça no seu aparelho antes de
planejar em cima delas.

Para 16 GB, a faixa de **12 a 14 bilhões de parâmetros em 4 bits** é o ponto
ideal. Famílias que costumam ir bem em tradução: Gemma, Qwen e Llama. Modelos
novos saem o tempo todo — vale olhar no LM Studio se há versões mais recentes
em vez de fixar num nome.

Se o modelo tiver "modo de raciocínio", desligue para tradução: só deixa o
processo mais lento sem melhorar o resultado.

### Estimativa de tempo

Um livro de ~300 páginas com modelo de 12–14 B leva algo entre 3 e 6 horas.
Dá para deixar rodando à noite. O Interlinear salva o progresso a cada lote, então
fechar o navegador no meio não perde o que já foi feito.

### Dicas práticas

1. Traduza em pedaços pequenos — parágrafos ou blocos curtos. Modelos pequenos
   se perdem em textos longos.
2. Use o glossário para nomes de personagens, lugares e termos recorrentes.
   É o que mantém a consistência do começo ao fim do livro.
3. Temperatura baixa (0,2 a 0,3) para tradução mais fiel.
4. Feche navegador e apps pesados durante a execução, para liberar memória.
5. Mantenha o Mac na tomada.
6. **Teste com um capítulo** antes de mandar o livro inteiro.

### Local ou API?

Local não tem custo por token e o texto não sai da sua máquina. A API é muito
mais rápida e, para tradução, surpreendentemente barata — um romance inteiro
sai entre US$ 0,11 e US$ 5 conforme o modelo. As contas completas estão em
[`conversao.md`](conversao.md), seção 6.

O Interlinear suporta os dois, e dá para alternar entre perfis sem reconverter nada.

---

## 5. As decisões técnicas do Interlinear

### Licença: por que MIT e o que custou

As duas bibliotecas mais naturais para este trabalho são **AGPL-3.0**:
**PyMuPDF** para extrair PDF e **EbookLib** para ler e escrever EPUB. Ambas são
melhores que as alternativas que acabaram sendo usadas.

A AGPL é copyleft forte: depender delas obrigaria o projeto inteiro a ser AGPL.
Consequências concretas: ninguém poderia usar o código num produto fechado (o
que afasta contribuição), e uma eventual versão hospedada teria de oferecer o
código-fonte a quem a usasse.

Por isso: **pdfminer.six** (MIT) no lugar do PyMuPDF, e a escrita de EPUB feita
à mão com o `zipfile` da biblioteca padrão no lugar do EbookLib.

O custo foi real — umas 300 linhas que a EbookLib resolveria. O ganho não
previsto: como o XHTML é gerado por código nosso, o painel de prévia usa
exatamente o mesmo gerador da exportação. O que aparece na tela não é
aproximação, é o arquivo final.

Detalhes e o caminho de volta (se um dia valer trocar para PyMuPDF e aceitar a
AGPL) em [`licencas.md`](licencas.md).

### Arquitetura

```
backend/app/
  core/            nada aqui sabe que existe uma web ou uma IA
    blocks.py      estrutura de dados comum a tudo
    pdf_extract.py PDF → blocos, via pistas tipográficas
    structure.py   blocos → capítulos
    epub_write.py  capítulos → EPUB 3
    epub_read.py   EPUB → blocos
    storage.py     projetos em disco, gravação atômica
  ai/
    base.py        interface comum, retentativa com espera exponencial
    providers.py   OpenAI, Anthropic, Google, Ollama, LM Studio
    translator.py  lotes, glossário, contexto, verificação de formato
  routes/          HTTP
frontend/          JavaScript puro, sem build
```

A regra que mantém isso navegável: **`core/` não importa nada de `ai/` nem de
`routes/`**. O conversor funciona como biblioteca, sem subir servidor:

```python
from app.core.pdf_extract import extract_blocks
from app.core.structure import split_into_chapters
from app.core.epub_write import write_epub
from app.core.blocks import Document

blocks, title = extract_blocks("livro.pdf", source_name="livro.pdf")
doc = Document(title=title)
doc.chapters = split_into_chapters(blocks)
write_epub(doc, "livro.epub")
```

É também o que torna barata a troca do extrator: só `core/pdf_extract.py`
precisaria mudar, porque o resto do sistema conhece apenas a lista de `Block`.

### Outras escolhas

**JavaScript puro, sem build.** Um app local que o usuário vai querer modificar
não deveria exigir instalar um empacotador antes de trocar uma cor.

**Sem banco de dados.** Um projeto é uma pasta com um JSON legível, o arquivo
original e as imagens. Facilita inspecionar, versionar e recuperar.

**O original nunca é sobrescrito.** Cada bloco guarda `text` (origem) e
`translated` separadamente. Permite retraduzir sem perder nada e exportar o
EPUB original para comparar no mesmo leitor.

**Chaves de API ficam em arquivo local com permissão 0600**, e a API nunca
devolve a chave inteira para a interface — só os últimos caracteres. Quem
preferir não gravar nada usa as variáveis de ambiente `INTERLINEAR_*_KEY`.

---

## 6. O essencial sobre conversão e custos

Resumo do que está detalhado em [`conversao.md`](conversao.md):

**Por que é difícil.** O PDF guarda aparência, não estrutura. Os rótulos
originais ("isto é Título 1") foram descartados na exportação. Converter de
volta é inferência, não tradução de formato — e inferência erra. Pior: nada
verifica o resultado, porque um EPUB ruim abre normalmente.

**Por isso o preview existe.** Ele é o mecanismo de erro do sistema. Cada tipo
de falha tem assinatura visual — parágrafo começando em minúscula, cabeçalho
solto no meio do texto, título em corpo errado — e aparece em segundos, sem
precisar ler o livro.

**Custos, para um romance de 100 mil palavras:**

| | Custo |
| --- | --- |
| Conversão assistida por IA (todos os blocos) | ~US$ 0,03 |
| Tradução, modelo barato (Flash-Lite / Haiku) | ~US$ 0,11 |
| Tradução, modelo intermediário (Sonnet) | ~US$ 2,50 |
| Tradução, modelo caro (Opus) | ~US$ 5,00 |
| Qualquer um dos dois, modelo local | US$ 0 |

Preços de outubro de 2026 — confirme antes de se comprometer.

**Se a origem for EPUB, nada disso se aplica.** `epub_read.py` lê a estrutura
direto do arquivo: capítulos separados, parágrafos marcados, itálico
preservado. Sem inferência, sem limiar para calibrar. O custo é só tradução e a
revisão é só de sentido.

---

## 7. Por onde continuar

Em ordem de retorno sobre esforço:

1. **Recuperar o itálico na extração.** Rápido, e o que mais melhora o
   resultado em romance. O extrator já calcula a flag `italic` em
   `core/pdf_extract.py` mas a descarta; o resto do sistema já suporta `<em>`.
2. **Corrigir a junção de hífen composto.** Hoje `"guarda-" + "chuva"` vira
   `guardachuva`. Dicionário, lista de prefixos ou um modelo pequeno.
3. **Marcação automática de suspeitos.** Regras triviais (parágrafo começando
   em minúscula, título longo demais, bloco de um caractere) marcadas na
   margem, com um "ir para o próximo suspeito". Transforma horas de revisão em
   minutos.
4. **IA como árbitro dos suspeitos.** Mesma marcação do item 3, mas roteada
   para um modelo pequeno antes de chegar ao humano. Custa centavos.
5. **Expor os limiares na interface**, para calibrar por livro sem editar
   código.
6. **Extrair imagens do PDF.**
7. **OCR** para PDFs escaneados.

Os itens 1 e 2 são bugs conhecidos, não funcionalidades novas.

---

## 8. Sobre direitos autorais

Converter e traduzir um livro para uso pessoal é uma coisa; publicar ou
distribuir a tradução é outra, e depende de autorização de quem detém os
direitos. Esta ferramenta não verifica nada disso — a responsabilidade é de
quem usa.
