# Como contribuir

Obrigado pelo interesse no Interlinear. Este guia explica como preparar o
ambiente, o que o projeto espera de um pull request e as poucas regras que
mantêm o código navegável.

## Antes de começar

- **Bug ou ideia pequena:** abra uma [issue](https://github.com/LucasAzevedoCosta/interlinear/issues)
  usando um dos modelos. Para bugs de conversão, um PDF de exemplo (ou uma
  página dele) ajuda mais que qualquer descrição.
- **Mudança grande** (novo formato, nova etapa, reorganização): abra uma
  issue para conversar antes de escrever o código. Evita trabalho perdido.
- **Falha de segurança:** não abra issue. Siga o [SECURITY.md](SECURITY.md).

Leitura recomendada antes do primeiro PR: o [guia](docs/guia.md) (decisões
técnicas) e, se for mexer na extração de PDF, o [docs/conversao.md](docs/conversao.md).

## Preparando o ambiente

O Interlinear só roda em container, inclusive no desenvolvimento: precisa do
git e do Docker, e de nada mais. Python, Tesseract e as ferramentas de
qualidade vêm na imagem `dev` do `Dockerfile`.

```bash
git clone https://github.com/<seu-usuario>/interlinear.git   # o seu fork
cd interlinear

docker compose run --rm dev pytest    # tudo deve passar antes de você mexer
```

Para subir o app enquanto desenvolve, com recarga automática do backend:

```bash
docker compose up dev                 # http://127.0.0.1:8001
```

A pasta do projeto é montada no container **só para leitura**: o que você
edita aparece na hora lá dentro, e nada que os testes ou o servidor gerem
(caches, PDFs de teste, projetos) vai parar no seu computador. Os dados do
app de desenvolvimento ficam num volume separado, `interlinear-dev-data`.

O frontend é JavaScript puro, sem build: edite `frontend/` e recarregue a
página. Mudou `requirements*.txt` ou o `Dockerfile`? Reconstrua com
`docker compose build dev`.

### O que a CI confere

Os mesmos comandos, todos dentro da imagem `dev`. Rode antes de abrir o PR:

```bash
docker compose run --rm dev ruff format --check .                 # formatação
docker compose run --rm dev ruff check .                          # linter
docker compose run --rm dev pytest                                # testes
docker compose run --rm dev bandit -q -c pyproject.toml -r backend/app   # padrões inseguros
docker compose run --rm dev pip-audit -r requirements.txt         # dependências vulneráveis
```

A CI também constrói a imagem de produção e confere que ela sobe e responde.

## Regras da arquitetura

- **`backend/app/core/` não importa nada de `ai/` nem de `routes/`.** O
  conversor precisa continuar utilizável como biblioteca, sem servidor e sem
  IA. O README mostra o exemplo.
- **O original nunca é descartado.** Toda mudança no modelo de dados mantém o
  texto de origem ao lado da tradução.
- **A prévia e a exportação usam o mesmo gerador de XHTML** (`epub_write.py`).
  Não crie um caminho separado só para a tela.
- **Nada de dependências AGPL ou GPL.** O projeto é MIT, e isso só funciona
  com dependências permissivas. Antes de adicionar uma, confira a licença e
  leia o [docs/licencas.md](docs/licencas.md).
- **Frontend sem build.** Nada de bundler, transpilador ou framework que
  exija `npm install` para trocar uma cor.
- **Tudo roda localmente.** Nenhum texto do livro sai da máquina, a não ser
  para o provedor de IA que o usuário configurou. Nada de telemetria.
- **Só roda em container.** O app, os testes e a CI rodam dentro da imagem
  (Linux, Python 3.12). Nada de scripts ou instruções para rodar direto no
  computador: quem usa precisa só do Docker, em Windows, macOS ou Linux.

## Testes

- Todo bug corrigido ganha um teste que falhava antes da correção.
- Toda funcionalidade nova ganha testes do caminho principal e dos erros
  previsíveis.
- Testes não acessam a rede. Provedores de IA são testados com
  `httpx.MockTransport` (veja `tests/test_providers.py`) ou com o provedor
  falso de `tests/test_translation_job.py`.
- PDFs de teste são gerados por código (`tests/make_fixture.py`, com
  `reportlab`, pela fixture `livro_pdf` do `tests/conftest.py`), numa pasta
  temporária, e não versionados. Isso evita problemas de direitos autorais e
  deixa claro qual armadilha cada PDF reproduz.
- O Tesseract está na imagem, então os testes de OCR sempre rodam.

## Estilo

- Comentários, docstrings, mensagens de erro, interface e documentação em
  **português**. Nomes de variáveis, funções e classes em inglês; nomes de
  testes em português, descrevendo o comportamento
  (`test_api_nunca_devolve_a_chave_inteira`).
- Comentários explicam o *porquê*, não o *quê*.
- O `.editorconfig` define indentação e fim de linha: 4 espaços em Python, 2
  em JavaScript, CSS e HTML.
- O Python é formatado pelo `ruff format` (linhas de até 100 colunas), e a CI
  recusa código fora do padrão. Para formatar:

  ```bash
  docker compose run --rm format
  ```

  É o único serviço que monta o projeto com escrita, porque precisa
  reescrever os arquivos.

  Commits só de formatação entram no `.git-blame-ignore-revs`.

## Commits

O projeto usa [Conventional Commits](https://www.conventionalcommits.org/pt-br/)
com a descrição em português, no imperativo da 3ª pessoa:

```text
tipo(escopo): descrição curta em minúsculas
```

| Tipo | Quando |
| ---- | ------ |
| `feat` | Funcionalidade nova |
| `fix` | Correção de bug |
| `docs` | Só documentação |
| `test` | Só testes |
| `refactor` | Mudança de código sem mudar comportamento |
| `ci` | GitHub Actions, Docker |
| `chore` | Manutenção (dependências, configuração) |

Escopos comuns: `pdf`, `ocr`, `epub`, `editor`, `importacao`, `traducao`,
`provedores`, `config`. Exemplos reais do histórico:

```text
feat(pdf): lê páginas de duas colunas na ordem certa
fix(traducao): só marca a tarefa como concluída depois de gravar
```

## Pull requests

1. Faça um fork e crie um branch a partir do `main`
   (`git switch -c fix/hifen-composto`).
2. Mantenha o PR focado num assunto só. Dois assuntos, dois PRs.
3. Rode os comandos de [O que a CI confere](#o-que-a-ci-confere) antes de enviar.
4. Preencha o modelo do PR. Se mudar a interface, anexe um print.
5. A CI precisa passar.

### Revisão

Todo PR é lido por inteiro, e a revisão pode usar um LLM como apoio para
procurar regressões, código morto, duplicação e falta de teste. Ele não lê só
a descrição: lê o código. A decisão de aceitar, pedir mudanças ou fechar é
sempre de um mantenedor.

Ao enviar uma contribuição, você concorda que ela seja distribuída sob a
[licença MIT](LICENSE) do projeto.
