# Como sai uma versão

Uma versão nasce de uma tag `vX.Y.Z`. Empurrar a tag dispara o workflow
[`release.yml`](../.github/workflows/release.yml), que faz o resto sozinho.

## Passo a passo

1. Escolha o número pelo [Versionamento Semântico](https://semver.org/lang/pt-BR/):
   correção muda o último número (`0.1.1`), funcionalidade nova o do meio
   (`0.2.0`). Sufixo (`0.2.0-rc.1`) publica como pré-versão, sem mexer no
   `latest`.
2. Troque a versão nos dois lugares em que ela aparece:
   - `pyproject.toml` (`version = "X.Y.Z"`)
   - `backend/app/main.py` (`version="X.Y.Z"`)
3. No `CHANGELOG.md`, renomeie `## [Unreleased]` para `## [X.Y.Z] - AAAA-MM-DD`,
   abra um `## [Unreleased]` vazio em cima e atualize os links do rodapé.
4. Commit, tag e push:

   ```bash
   git commit -am "chore(release): vX.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

## O que o pipeline faz

1. Confere que `pyproject.toml`, `main.py` e `CHANGELOG.md` estão na versão da
   tag. Se algum não estiver, para antes de publicar qualquer coisa.
2. Roda os testes na imagem `dev`.
3. Constrói a imagem uma vez por arquitetura (amd64 e arm64) e publica no
   GitHub Container Registry:
   - `ghcr.io/lucasazevedocosta/interlinear:X.Y.Z` e `:latest`
   - `ghcr.io/lucasazevedocosta/interlinear:X.Y.Z-claude` e `:claude`, com o
     Claude Code
4. Cria o GitHub Release com a seção da versão no CHANGELOG, as instruções de
   instalação e o digest de cada imagem.

Para publicar de novo uma tag que já existe (depois de um erro no pipeline,
por exemplo), rode o workflow **Release** pela aba **Actions** informando a tag.

## Primeira publicação

O pacote no ghcr.io nasce **privado**. Depois da primeira release, abra
**Packages → interlinear → Package settings** no GitHub e mude a visibilidade
para **Public**. Sem isso, `docker compose up -d` não consegue baixar a imagem
para quem não tem acesso ao repositório.
