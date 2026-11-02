"""Hífen de fim de linha: quebra de sílaba ou parte da palavra?"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from reportlab.lib.pagesizes import A5  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.core.blocks import Vocabulary, join_hyphenated, keeps_hyphen  # noqa: E402
from app.core.pdf_extract import extract_blocks  # noqa: E402


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        # quebra de sílaba: some
        ("exem", "plo", False),
        ("pe", "na", False),       # "-na" solto não é pronome aqui
        ("pas", "se", False),
        ("ex", "cesso", False),    # "ex" não entra como prefixo
        ("sem", "pre", False),
        ("mal", "dade", False),
        # sem precisar do livro
        ("fazê", "lo", True),
        ("pô", "la", True),
        ("deram", "na", True),
        ("põe", "no", True),
        ("segunda", "feira", True),
        ("pré", "história", True),
        ("recém", "chegado", True),
        ("vice", "versa", True),
    ],
)
def test_regras_sem_vocabulario(before, after, expected):
    assert keeps_hyphen(before, after) is expected


def test_o_livro_decide_quando_tem_evidencia():
    vocabulary = Vocabulary.from_texts([
        "Abriu o <em>guarda-chuva</em> e disse-lhe adeus.",
        "Não havia excesso nenhum, e a pena era leve.",
    ])
    assert keeps_hyphen("guarda", "chuva", vocabulary)
    assert keeps_hyphen("disse", "lhe", vocabulary)
    assert not keeps_hyphen("guarda", "chuva")  # sem o livro, na dúvida, junta
    # A palavra inteira no livro vence até uma regra: "ex" + "cesso".
    assert not keeps_hyphen("ex", "cesso", vocabulary)


def test_join_hyphenated():
    vocabulary = Vocabulary.from_texts(["um guarda-chuva"])
    assert join_hyphenated("o guarda-", "chuva aberto", vocabulary) == "o guarda-chuva aberto"
    assert join_hyphenated("um exem-", "plo", vocabulary) == "um exemplo"
    assert join_hyphenated("Jean-", "Paul", vocabulary) == "Jean-Paul"  # maiúscula
    assert join_hyphenated("e então -", "Disse ele") == "e então - Disse ele"  # travessão solto
    assert join_hyphenated("fim.", "Começo") == "fim. Começo"


def test_pdf_preserva_compostos_e_desfaz_quebras(tmp_path):
    lines = [
        "Ela abriu o guarda-chuva e disse-lhe adeus. Tinha esquecido o guarda-",
        "chuva antes, e por isso disse-",
        "lhe que voltaria. Para fazê-",
        "lo era preciso coragem, uma pe-",
        "na qualquer e nenhum ex-",
        "cesso na segunda-",
        "feira de manhã.",
    ]
    path = tmp_path / "hifen.pdf"
    c = canvas.Canvas(str(path), pagesize=A5)
    c.setFont("Times-Roman", 10)
    for i, line in enumerate(lines):
        c.drawString(30, A5[1] - 80 - i * 13, line)
    c.showPage()
    c.save()

    blocks, _ = extract_blocks(str(path))
    assert blocks[0].text == (
        "Ela abriu o guarda-chuva e disse-lhe adeus. Tinha esquecido o guarda-chuva "
        "antes, e por isso disse-lhe que voltaria. Para fazê-lo era preciso coragem, "
        "uma pena qualquer e nenhum excesso na segunda-feira de manhã."
    )
