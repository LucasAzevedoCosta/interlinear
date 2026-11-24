# Changelog

Todas as mudanças relevantes do Interlinear ficam aqui. O formato segue o
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/), e as versões
seguem o [Versionamento Semântico](https://semver.org/lang/pt-BR/).

A seção de cada versão vira o texto do GitHub Release (veja
[docs/release.md](docs/release.md)).

## [Unreleased]

## [0.1.0] - 2026-11-24

Primeira versão pública.

### Adicionado

- Importação de PDF com texto nativo, PDF escaneado (OCR com Tesseract, só nas
  páginas que precisam) e EPUB, em segundo plano, com opções, progresso e um
  resumo do que foi encontrado.
- Reconstrução de capítulos, títulos e parágrafos a partir das pistas
  tipográficas do PDF, descartando cabeçalhos correntes e números de página.
- Itálico, negrito, imagens, notas de rodapé (inclusive as que continuam na
  página seguinte), tabelas e páginas de duas colunas.
- Etapas separadas de conversão e de tradução, com o original sempre guardado
  ao lado da tradução e traduções desatualizadas refeitas sob demanda.
- Editor bloco a bloco com salvamento automático: apagar, juntar e inserir
  blocos, com prévia ao vivo gerada pelo mesmo código da exportação.
- Tradução com OpenAI, Anthropic, Google Gemini, endpoints compatíveis
  (OpenRouter, DeepSeek, Groq), Ollama e LM Studio, com glossário, idiomas e
  provedor por livro.
- Claude pela assinatura, pelo Claude Code dentro do container (experimental),
  com painel de conexão nas configurações.
- Seletor de modelo com busca, que lista os modelos do serviço antes de salvar.
- Exportação em EPUB 3, com a tradução ou com o original.
- Imagens Docker publicadas no GitHub Container Registry (amd64 e arm64), com e
  sem o Claude Code.

### Segurança

- API local protegida contra DNS rebinding e CSRF (checagem de `Host` e
  `Origin`).
- Chaves de API nunca devolvidas inteiras pela API; arquivo de chaves só
  legível pelo dono; container roda sem privilégios.

[Unreleased]: https://github.com/LucasAzevedoCosta/interlinear/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/LucasAzevedoCosta/interlinear/releases/tag/v0.1.0
