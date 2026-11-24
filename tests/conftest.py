"""Fixtures comuns a mais de um arquivo de teste."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def livro_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """O PDF de teste (tests/make_fixture.py), gerado uma vez por execução.

    Vai para uma pasta temporária, e não para tests/: a suíte roda com o
    código montado só para leitura no container.
    """
    from make_fixture import gerar

    return gerar(tmp_path_factory.mktemp("fixtures") / "livro_teste.pdf")
