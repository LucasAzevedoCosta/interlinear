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

Precisa de Python 3.11 ou mais recente e do git. O Tesseract é opcional: sem
ele, os testes de OCR são pulados.

```bash
git clone https://github.com/<seu-usuario>/interlinear.git   # o seu fork
cd interlinear

python3 -m venv .venv                 # Windows: py -3.12 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

pytest                                # tudo deve passar antes de você mexer
```

Para subir o app enquanto desenvolve, com recarga automática do backend:

```bash
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

O frontend é JavaScript puro, sem build: edite `frontend/` e recarregue a
página.

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
- **Funciona em Windows, macOS e Linux.** Evite APIs exclusivas de um
  sistema (`os.uname`, `fcntl`, caminhos com `/` montados à mão) e abra
  arquivos de texto com `encoding="utf-8"` explícito. A CI roda nos três.

## Testes

- Todo bug corrigido ganha um teste que falhava antes da correção.
- Toda funcionalidade nova ganha testes do caminho principal e dos erros
  previsíveis.
- Testes não acessam a rede. Provedores de IA são testados com
  `httpx.MockTransport` (veja `tests/test_providers.py`) ou com o provedor
  falso de `tests/test_translation_job.py`.
- PDFs de teste são gerados por código (`tests/make_fixture.py`, com
  `reportlab`), não versionados. Isso evita problemas de direitos autorais e
  deixa claro qual armadilha cada PDF reproduz.
- Testes que dependem do Tesseract usam
  `@pytest.mark.skipif(not ocr.available(), ...)`.

## Estilo

- Comentários, docstrings, mensagens de erro, interface e documentação em
  **português**. Nomes de variáveis, funções e classes em inglês; nomes de
  testes em português, descrevendo o comportamento
  (`test_api_nunca_devolve_a_chave_inteira`).
- Comentários explicam o *porquê*, não o *quê*.
- O `.editorconfig` define indentação e fim de linha: 4 espaços em Python, 2
  em JavaScript, CSS e HTML.

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
3. Rode `pytest` antes de enviar.
4. Preencha o modelo do PR. Se mudar a interface, anexe um print.
5. A CI precisa passar em Linux, macOS e Windows.

Ao enviar uma contribuição, você concorda que ela seja distribuída sob a
[licença MIT](LICENSE) do projeto.
