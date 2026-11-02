# A conversão de PDF: dificuldades, preview e onde a IA entra

Este documento registra por que a conversão de PDF para EPUB é difícil, quais
limitações o Interlinear tem hoje, como o painel de prévia compensa parte delas, e o
que um modelo de IA pequeno poderia resolver — com as contas de quanto isso
custa.

Se você veio parar aqui tentando entender por que o resultado da sua conversão
saiu torto, comece pela seção "Limitações conhecidas".

---

## 1. Por que é difícil: um problema inverso com perda

O PDF não guarda estrutura. Guarda aparência.

O livro nasceu num editor onde cada trecho tinha um rótulo explícito — *isto é
Título 1*, *isto é parágrafo de corpo*, *isto é legenda*. Na exportação para
PDF, esses rótulos são descartados. Sobra a posição de cada caractere numa
folha de tamanho fixo.

Converter de volta é reconstruir uma informação que foi deliberadamente
apagada. Não existe "conversão" no sentido estrito; existe **inferência**. E
inferência erra.

Isso explica por que PDF é o único formato de origem realmente problemático.
Converter EPUB para MOBI, ou DOCX para EPUB, é fácil: a origem já tem a
estrutura marcada, é só traduzir de uma marcação para outra. Só no sentido
`PDF → qualquer coisa` a informação precisa ser deduzida do zero.

### A consequência operacional

**Nada verifica o resultado.** Não há soma de verificação, não há erro de
compilação. Um EPUB terrível abre normalmente em qualquer leitor. O único teste
possível é um humano olhando.

Esse é o motivo de existir o painel de prévia, e o motivo de ele não ser um
enfeite.

---

## 2. Limitações conhecidas

Divididas entre o que é falha de projeto (consertável) e o que é limite da
abordagem.

### Bugs identificados, ainda não corrigidos

**Junção de hífen erra em palavras compostas.** A regra em
`blocks.join_hyphenated` é: se a linha termina em hífen e a próxima começa com
minúscula, junta e remove o hífen.

```
"exem-" + "plo"     → "exemplo"       correto
"guarda-" + "chuva" → "guardachuva"   errado
```

A heurística atual usa a capitalização da palavra seguinte como pista, o que
só pega compostos com nome próprio ("Bem-" + "Vindo"). Precisa de dicionário,
de lista de prefixos, ou de um modelo (ver seção 5).

### Limites da abordagem heurística

**Os limiares são palpites calibrados em uma fixture sintética.** Os valores em
`core/pdf_extract.py`:

| Constante | Valor | O que faz |
| --------- | ----- | --------- |
| `HEADING_RATIO_H1` | 1.40 | Linha vira título se for 40% maior que o corpo |
| `HEADING_RATIO_H2` | 1.20 | |
| `HEADING_RATIO_H3` | 1.08 | |
| `MAX_HEADING_WORDS` | 18 | Acima disso é texto grande, não título |
| `RUNNING_HEAD_THRESHOLD` | 0.45 | Fração de páginas em que uma linha precisa repetir para ser cabeçalho |
| limiar de quebra | 1.45× | Espaçamento vertical que quebra parágrafo |

Esses números saíram de raciocínio sobre diagramação de livros mais o teste em
`tests/fixtures/livro_teste.pdf` — que é gerado pelo próprio projeto. Isso é
circular: a fixture foi desenhada por quem escreveu o extrator. Livros reais
vão furar pelo menos um desses valores.

O extrator trabalha em **proporção** ao corpo detectado em cada documento, não
em valores absolutos, o que já o torna mais robusto que um conversor genérico.
Mas proporção não resolve tudo.

**Parágrafo que atravessa a quebra de página.** A regra é: só quebra o
parágrafo se a última linha da página terminou em pontuação de fim de frase.
Erra nos dois sentidos:

- diálogo terminando em travessão, sem ponto → é colado indevidamente ao
  parágrafo seguinte;
- frase que por acaso termina no pé da página → parte um parágrafo que
  continuava.

É o erro mais frequente em conversão de livro, e o que a heurística tem menos
chance de acertar sozinha.

**Duas colunas.** Não tratado. O `pdfminer` faz alguma inferência de ordem de
leitura pelo parâmetro `boxes_flow`, mas em PDF acadêmico o texto sai
embaralhado e não há conserto no estado atual.

**Capitular (drop cap).** A letra grande e decorativa que abre o capítulo
costuma ser um objeto de texto separado no PDF. Tem boa chance de virar um
bloco com um caractere só, e o parágrafo começar na segunda letra. Caso não
testado.

**Livro sem nenhum título.** Boa parte da ficção literária separa seções apenas
com linha em branco ou um ornamento gráfico. Sem título para detectar, o livro
vira um capítulo único. Existe um fallback em `structure.split_into_chapters`
que desce de `heading1` para `heading2` e `heading3`, mas se não há título de
nível nenhum, não há o que fazer automaticamente.

**Notas de rodapé e tabelas.** Notas entram como parágrafos comuns no corpo.
Tabelas viram parágrafos soltos.

**Imagens só nos formatos comuns.** Fotos em JPEG e bitmaps de 8 bits em RGB ou
tons de cinza são extraídos e encaixados entre os parágrafos pela posição na
página. Imagens em CMYK, com paleta, de 1 bit, JPEG 2000 ou JBIG2 são
ignoradas, assim como ornamentos pequenos (menos de 40 pt) e imagens que se
repetem em três páginas ou mais (logotipos, molduras).

**Itálico depende do nome da fonte.** Itálico e negrito viram `<em>` e
`<strong>` a partir do nome da fonte de cada caractere ("Times-Italic",
"MinionPro-It", "Garamond-BoldIt"). Uma fonte itálica com nome que não diz
isso passa como texto normal.

**PDF escaneado.** Sem OCR não há texto. O extrator levanta `ValueError` com
mensagem explícita em vez de produzir um EPUB vazio.

---

## 3. Como o preview compensa

Todos os problemas acima são **silenciosos**. Nenhum gera exceção, nenhum
impede a exportação. O preview é o mecanismo de erro do sistema.

A chave: cada tipo de erro tem uma assinatura visual que aparece em segundos,
sem que você precise ler o livro. São erros de **forma**, e forma se vê de
relance.

| Erro | Como aparece na prévia |
| ---- | ---------------------- |
| Parágrafo cortado errado | Parágrafo começando com letra minúscula |
| Cabeçalho corrente que escapou | Linha solta com o nome do livro no meio do texto |
| Título falso | Parágrafo em corpo enorme |
| Título não detectado | Capítulo que deveria começar, mas segue em corpo normal |
| Dois capítulos fundidos | Outro título aparece no meio da rolagem |
| Capitular quebrada | Letra sozinha, seguida de parágrafo começando errado |

### O preview usa o gerador real

`routes/projects.preview_chapter` chama `epub_write.chapter_to_xhtml` — a mesma
função da exportação. Não é uma aproximação em HTML feita para a tela.

Isso elimina uma classe inteira de falha: a de "na tela estava certo, no leitor
saiu torto". Se está bom no painel, está bom no arquivo.

### Três visões, três classes de erro

A bancada separa de propósito:

- **Barra colorida na margem** (âmbar / azul / preto) — mostra *onde falta
  trabalho*: não traduzido, traduzido por máquina, revisado por humano.
- **Original acima de cada campo** — pega erros de *tradução*: sentido
  alterado, nome trocado, registro errado.
- **Painel de prévia** — pega erros de *estrutura*.

São problemas distintos e cada um tem seu lugar de aparecer.

O botão Tradução/Original fecha o ciclo: dá para exportar os dois EPUBs e
comparar lado a lado no mesmo leitor.

### Uso menos óbvio: calibragem

Como os limiares da seção 2 são palpites, o preview é como se descobre que um
livro específico precisa de outros números. Converteu, olhou, nenhum título foi
detectado? Aquele livro provavelmente usa títulos só 20% maiores que o corpo.

Hoje isso exige editar o código. Expor esses valores na interface é melhoria
pendente.

### O que o preview não resolve

Ele **mostra** o erro, mas não **acha** o erro. Num livro de 3.000 parágrafos,
revisar tudo visualmente leva horas, e o olho cansa e passa batido.

Isso motiva a próxima seção.

---

## 4. Marcação automática de suspeitos

Antes de recorrer a IA, boa parte do problema se resolve com regras triviais.
A ideia é marcar na margem os blocos provavelmente errados e oferecer um
"ir para o próximo suspeito", para o olho ir direto aos ~40 lugares duvidosos
em vez de varrer 3.000.

Regras que custam zero:

- parágrafo começando com letra minúscula → provável corte indevido;
- título com mais de `MAX_HEADING_WORDS` palavras → provável falso positivo;
- bloco com um ou dois caracteres → provável capitular ou número de página;
- bloco cujo texto se repete quase igual em várias páginas → cabeçalho que
  escapou do filtro;
- parágrafo terminando sem pontuação, antes de quebra de página → candidato a
  junção;
- capítulo com menos de N palavras → provável divisão indevida.

Essa marcação é também **a porta de entrada da IA**: os mesmos blocos marcados
são exatamente os que vale a pena mandar para um modelo decidir.

---

## 5. Onde a IA ajuda na conversão

### O ponto central: é classificação, não geração

Para a conversão, a IA não escreve texto. Ela **responde perguntas curtas sobre
fragmentos curtos**. Isso muda tudo em relação a custo e a tamanho de modelo:

- a entrada é um fragmento de duas linhas, não o livro;
- a saída é um rótulo ou um "sim/não", não um parágrafo;
- não é preciso janela de contexto grande;
- um modelo pequeno e barato resolve, e resolve rápido;
- roda bem local — num Mac com Apple Silicon, de graça.

### Tarefas, por ordem de valor

**1. Decidir junção na quebra de página.** O maior ganho. A pergunta é:
*"o fragmento B continua a frase do fragmento A?"*. A heurística só enxerga
pontuação; um modelo entende se a frase está gramaticalmente em aberto.
Entrada: últimos ~120 caracteres de A + primeiros ~120 de B. Saída: sim/não.

**2. Resolver hífen de palavra composta.** Resolve o bug do `guarda-chuva` sem
precisar de dicionário por idioma. Entrada: as duas partes. Saída: a palavra
unida.

**3. Classificar blocos ambíguos.** Para os blocos marcados como suspeitos:
*"isto é título de capítulo, parágrafo, legenda, nota de rodapé ou cabeçalho
que escapou?"*. Vale mandar junto as pistas numéricas que o extrator já tem —
razão de tamanho de fonte, posição na página, contagem de palavras. Modelos
usam bem evidência numérica quando ela vem explícita no prompt.

**4. Achar fronteira de capítulo em livro sem títulos.** Pela leitura do texto:
mudança de cena, salto temporal, encerramento.

### O que a IA não resolve

Vale ser claro, porque nem todo problema é problema de IA:

- **Itálico** — era bug de código, já corrigido: o modelo não pode recuperar
  uma informação que não chegou até ele, então o extrator passou a marcar
  `<em>` e `<strong>` pela fonte de cada caractere.
- **Duas colunas** — é problema de geometria, não de linguagem. Resolve-se com
  análise de layout (ou trocando para PyMuPDF), não com prompt.
- **PDF escaneado** — precisa de OCR. Modelo de texto não lê imagem.

### Arquitetura recomendada: híbrido

As heurísticas resolvem ~97% e são instantâneas e gratuitas. A IA arbitra os 3%
que as heurísticas marcaram como duvidosos. Isso é mais barato *e* melhor que
qualquer um dos dois sozinho — e usa exatamente a mesma lógica de marcação da
seção 4.

```
extração heurística
   ↓
marcação de suspeitos  ──→  ~3% dos blocos
   ↓                              ↓
97% resolvido              modelo decide
   ↓                              ↓
            preview  ←────────────┘
               ↓
         revisão humana
```

---

## 6. Custos

Contas para um romance de **100.000 palavras** (~350 páginas, ~3.000 blocos,
~580.000 caracteres).

Preços consultados em outubro de 2026. **Confirme antes de se comprometer** —
tabelas de preço mudam com frequência.

### Conversão assistida por IA

O volume é pequeno porque só fragmentos curtos são enviados.

**Só os suspeitos** (~100 blocos, 25 por requisição, 4 requisições):
≈ 7.600 tokens de entrada, 800 de saída.

**Todos os blocos** (3.000 blocos, 120 requisições):
≈ 230.000 de entrada, 24.000 de saída.

| Estratégia | Entrada | Saída | Custo (Flash-Lite / Haiku) |
| ---------- | ------- | ----- | -------------------------- |
| Só suspeitos | 7,6 K | 0,8 K | **~US$ 0,001** |
| Junções de página (~200 decisões) | 15 K | 0,5 K | **~US$ 0,002** |
| Todos os blocos | 230 K | 24 K | **~US$ 0,03** |

**Conclusão: a conversão assistida por IA é gratuita na prática.** Mesmo
classificando o livro inteiro, bloco por bloco, o custo fica em torno de três
centavos de dólar. Não há razão econômica para economizar aqui.

### Tradução — onde o custo realmente está

Com os parâmetros do Interlinear (`batch_chars = 3500`), um livro de 100.000 palavras
dá ~170 lotes:

```
entrada por lote   ≈ 1.430 tokens   (instruções + glossário + contexto + texto)
saída por lote     ≈ 1.200 tokens   (português ocupa ~20% mais que inglês)

total entrada      ≈ 243.000 tokens  (0,25 M)
total saída        ≈ 204.000 tokens  (0,20 M)
```

| Modelo | Entrada / Saída (US$/M) | Custo do livro |
| ------ | ----------------------- | -------------- |
| Gemini Flash-Lite 2.5 | 0,10 / 0,40 | **~US$ 0,11** |
| Claude Haiku 5.5 | 0,10 / 0,50 | **~US$ 0,13** |
| Gemini Flash 2.5 | 0,30 / 2,50 | **~US$ 0,58** |
| Claude Sonnet 5.5 | 2,00 / 10,00 | **~US$ 2,50** |
| Claude Opus 5.5 | 4,00 / 20,00 | **~US$ 5,00** |
| Modelo local (LM Studio / Ollama) | — | **US$ 0** (só tempo) |
| Gemini via AI Studio, camada gratuita | — | **US$ 0** (ver ressalva) |

### Ressalvas

**Camada gratuita do Google AI Studio.** Existe e cobre os modelos Flash, mas
o Google usa o conteúdo enviado para melhorar seus produtos. Para um livro sob
direitos autorais que não é seu, pondere. Os limites de requisição por minuto
também são bem menores que os da camada paga, o que em `concurrency = 3` pode
gerar erro 429 — o tradutor tem retentativa com espera exponencial, mas a
tradução fica lenta.

**Retentativas custam.** Quando um lote volta fora do formato, o tradutor cai
para tradução individual (`_translate_individually`), que gasta mais por bloco
porque repete o prompt de sistema a cada parágrafo. Modelos muito pequenos
erram o formato com mais frequência, o que corrói parte da economia.

**É custo único por livro**, não assinatura.

**Como refazer a conta para o seu livro:** multiplique os totais pela razão
entre o seu número de palavras e 100.000. Um livro de 50.000 palavras custa
metade. A tela de progresso do Interlinear mostra tokens de entrada e saída
consumidos em tempo real, então dá para traduzir um capítulo, ver o gasto real
e extrapolar antes de soltar o livro inteiro.

### Estratégia recomendada

Traduza **um capítulo** em dois ou três modelos, compare a qualidade no painel
e veja o custo real medido. A diferença entre Haiku e Opus é de ~40× no preço;
se a qualidade do barato for suficiente para o seu livro, a escolha é óbvia.
Se não for, você ainda está falando de US$ 5 por um romance inteiro.

---

## 7. Se a origem for EPUB, nada disto se aplica

Vale registrar porque muda completamente o quadro: **traduzir um EPUB pronto
não passa por nenhum dos problemas das seções 1 a 5.**

`core/epub_read.py` lê a estrutura diretamente do arquivo — os capítulos já
estão separados, os parágrafos já estão marcados com `<p>`, os títulos com
`<h1>`. Não há inferência, não há palpite, não há limiar para calibrar. O
itálico inclusive é preservado nesse caminho, porque `_text_of` mantém as tags
inline.

Nesse caso o único custo é a tradução, e a única revisão necessária é de
sentido, não de forma.
