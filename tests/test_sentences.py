"""Divisão de um bloco em frases, para a tradução frase a frase."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.sentences import join_sentences, split_sentences  # noqa: E402


# ------------------------------------------------------------ o básico


def test_vazio_e_sem_ponto():
    assert split_sentences("") == []
    assert split_sentences("   ") == []
    assert split_sentences("Uma frase sem ponto final") == ["Uma frase sem ponto final"]


def test_tres_frases_simples():
    assert split_sentences("Era cedo. Ninguém viu. Ele saiu.", "pt-BR") == [
        "Era cedo.",
        "Ninguém viu.",
        "Ele saiu.",
    ]


def test_exclamacao_interrogacao_e_combinacoes():
    assert split_sentences("Quem era? Ninguém sabia! Mesmo assim?! Ele foi.", "pt") == [
        "Quem era?",
        "Ninguém sabia!",
        "Mesmo assim?!",
        "Ele foi.",
    ]


def test_aspas_e_parenteses_que_fecham_ficam_com_a_frase():
    assert split_sentences('Ele disse "Pare." Então parou. (Era tarde.) Fim de tudo.', "pt") == [
        'Ele disse "Pare."',
        "Então parou.",
        "(Era tarde.)",
        "Fim de tudo.",
    ]


def test_aspas_angulares_e_curvas():
    assert split_sentences(
        "«Vamos embora.» Ela riu. “Agora mesmo?” Ele assentiu devagar.", "pt"
    ) == [
        "«Vamos embora.»",
        "Ela riu.",
        "“Agora mesmo?”",
        "Ele assentiu devagar.",
    ]


# --------------------------------------------------- onde não cortar


def test_minuscula_depois_do_ponto_nao_corta():
    texto = "Ele hesitou... e então entrou. Não havia ninguém lá."
    assert split_sentences(texto, "pt") == [
        "Ele hesitou... e então entrou.",
        "Não havia ninguém lá.",
    ]


def test_reticencias_com_maiuscula_corta():
    assert split_sentences("Ela esperou… Nada aconteceu por horas.", "pt") == [
        "Ela esperou…",
        "Nada aconteceu por horas.",
    ]


@pytest.mark.parametrize(
    ("texto", "idioma"),
    [
        ("O Sr. Almeida chegou cedo. A Dra. Souza também chegou.", "pt"),
        ("Mr. Smith arrived early. Mrs. Jones came along too.", "en"),
        ("El Sr. García llegó temprano. La Dra. López también vino.", "es"),
        ("M. Dupont est arrivé tôt. Mme Martin est venue aussi.", "fr"),
        ("Hr. Müller kam früh. Prof. Weber kam ebenfalls dazu.", "de"),
        ("Il Sig. Rossi arrivò presto. Il Dott. Bianchi venne pure.", "it"),
    ],
)
def test_abreviacoes_de_cada_idioma(texto, idioma):
    assert len(split_sentences(texto, idioma)) == 2, split_sentences(texto, idioma)


def test_abreviacao_com_ponto_no_meio():
    assert split_sentences("Use tools, e.g. Hammers and saws. Then rest a while.", "en") == [
        "Use tools, e.g. Hammers and saws.",
        "Then rest a while.",
    ]
    assert len(split_sentences("Das ist z.B. Ein Beispiel. Und noch eins hier.", "de")) == 2


def test_iniciais_nao_cortam():
    assert split_sentences("J. R. R. Tolkien wrote it. Everyone read it later.", "en") == [
        "J. R. R. Tolkien wrote it.",
        "Everyone read it later.",
    ]


def test_numeros():
    assert split_sentences("Custava 3.5 libras. Era caro demais.", "pt") == [
        "Custava 3.5 libras.",
        "Era caro demais.",
    ]
    assert split_sentences("Nasceu em 1990. Depois mudou de cidade.", "pt") == [
        "Nasceu em 1990.",
        "Depois mudou de cidade.",
    ]
    # Número no começo do bloco é numeração, não fim de frase.
    assert split_sentences("1. Abra a caixa com cuidado.", "pt") == ["1. Abra a caixa com cuidado."]


def test_etc_no_meio_da_frase():
    assert split_sentences("Trouxe pão, leite etc. e foi embora cedo.", "pt") == [
        "Trouxe pão, leite etc. e foi embora cedo."
    ]


# --------------------------------------------------------------- diálogo


def test_travessao_com_minuscula_continua_a_frase():
    texto = "— Vamos embora? — perguntou ela. — Agora não — disse ele."
    assert split_sentences(texto, "pt") == [
        "— Vamos embora? — perguntou ela.",
        "— Agora não — disse ele.",
    ]


def test_travessao_com_maiuscula_e_fala_nova():
    assert split_sentences("— Venha logo. — Ele não respondeu nada.", "pt") == [
        "— Venha logo.",
        "— Ele não respondeu nada.",
    ]


def test_pergunta_espanhola():
    assert split_sentences("Lo vio ayer. ¿Dónde estaba ella? ¡Nadie lo sabe!", "es") == [
        "Lo vio ayer.",
        "¿Dónde estaba ella?",
        "¡Nadie lo sabe!",
    ]


# -------------------------------------------------------- japonês e chinês


def test_japones_corta_sem_espaco():
    assert split_sentences("今日は雨です。傘を持っていきます。", "ja") == [
        "今日は雨です。",
        "傘を持っていきます。",
    ]


def test_chines_corta_sem_espaco():
    assert split_sentences("他来了。你好吗？我很好！", "zh-CN") == [
        "他来了。",
        "你好吗？",
        "我很好！",
    ]


# ------------------------------------------------------------------ tags


def test_tag_que_atravessa_o_corte_e_fechada_e_reaberta():
    texto = "Ele leu <em>o livro. Depois dormiu</em> em paz."
    assert split_sentences(texto, "pt") == [
        "Ele leu <em>o livro.</em>",
        "<em>Depois dormiu</em> em paz.",
    ]


def test_tag_fechando_logo_depois_do_ponto_fica_com_a_frase():
    assert split_sentences("Leu <em>Dom Casmurro.</em> Gostou muito.", "pt") == [
        "Leu <em>Dom Casmurro.</em>",
        "Gostou muito.",
    ]


def test_tags_aninhadas_e_link():
    texto = 'Veja <a href="#x"><strong>isto. E aquilo</strong> também</a> aqui.'
    frases = split_sentences(texto, "pt")
    assert frases == [
        'Veja <a href="#x"><strong>isto.</strong></a>',
        '<a href="#x"><strong>E aquilo</strong> também</a> aqui.',
    ]


def test_chamada_de_nota_e_quebra_de_linha_nao_atrapalham():
    assert split_sentences("Era o fim<sup>1</sup>. Começou outro<br>dia ali.", "pt") == [
        "Era o fim<sup>1</sup>.",
        "Começou outro<br>dia ali.",
    ]


@pytest.mark.parametrize(
    "texto",
    [
        "Ele leu <em>o livro. Depois dormiu</em> em paz.",
        'Veja <a href="#x"><strong>isto. E aquilo</strong> também</a> aqui.',
        "<strong>Tudo em negrito. Até a segunda frase. E a terceira.</strong>",
        "Normal. <em>Itálico inteiro.</em> Normal de novo.",
    ],
)
def test_cada_frase_e_xml_valido(texto):
    for frase in split_sentences(texto, "pt"):
        etree.fromstring(f"<p>{frase}</p>".encode())


def test_menor_que_e_generico_de_codigo_sao_texto():
    """Num livro técnico, "<T>" e "i < n" não são tags."""
    texto = "Use StackNode<T> aqui. Depois compare i < n e pare."
    assert split_sentences(texto, "en") == [
        "Use StackNode<T> aqui.",
        "Depois compare i < n e pare.",
    ]
    assert join_sentences(split_sentences(texto, "en")) == texto


def test_bloco_so_com_tag_vira_uma_frase():
    assert split_sentences("<br>", "pt") == ["<br>"]


# -------------------------------------------------------- frase de uma palavra


def test_palavra_sozinha_vai_com_a_seguinte():
    assert split_sentences("Sim. Ele concordou com tudo.", "pt") == ["Sim. Ele concordou com tudo."]


def test_palavra_sozinha_no_fim_vai_com_a_anterior():
    assert split_sentences("Ele perguntou se podia. Não.", "pt") == ["Ele perguntou se podia. Não."]


def test_so_uma_palavra():
    assert split_sentences("Fim.", "pt") == ["Fim."]


# -------------------------------------------------------------- ida e volta


@pytest.mark.parametrize(
    ("texto", "idioma"),
    [
        ("Era cedo. Ninguém viu. Ele saiu.", "pt"),
        ("Ele leu <em>o livro. Depois dormiu</em> em paz.", "pt"),
        ("<strong>Tudo em negrito. Até a segunda frase. E a terceira.</strong>", "pt"),
        ('Veja <a href="#x"><strong>isto. E aquilo</strong> também</a> aqui.', "pt"),
        ("— Vamos embora? — perguntou ela. — Agora não — disse ele.", "pt"),
        ("Mr. Smith arrived early. Mrs. Jones came along too.", "en"),
        ("Sim. Ele concordou com tudo.", "pt"),
        ("今日は雨です。傘を持っていきます。", "ja"),
    ],
)
def test_ida_e_volta(texto, idioma):
    frases = split_sentences(texto, idioma)
    separador = "" if idioma == "ja" else " "
    if idioma == "ja":
        assert "".join(frases) == texto
    else:
        assert join_sentences(frases) == texto, (frases, separador)


def test_paragrafo_real_do_livro_de_teste():
    texto = (
        "O relojoeiro chamava-se Teodoro e não gostava de visitas. Quando a porta abria, "
        "ele erguia os olhos da bancada apenas o suficiente para saber se valia a pena largar "
        "a pinça. Na maioria das vezes não valia, e ele voltava ao trabalho sem dizer nada."
    )
    frases = split_sentences(texto, "pt-BR")
    assert len(frases) == 3
    assert join_sentences(frases) == texto
