"""Gera um PDF de teste parecido com um livro de verdade.

O texto é inventado para o teste. O que importa é o formato: cabeçalho
corrente, número de página, títulos em corpo maior, parágrafos com recuo de
primeira linha e parágrafos que atravessam a quebra de página — exatamente as
armadilhas que o extrator precisa resolver.
"""

from pathlib import Path

from reportlab.lib.pagesizes import A5
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

LIVRO = "A Oficina de Relógios"

CAPITULOS = [
    (
        "Capítulo 1: A chegada",
        [
            "A oficina ficava na esquina de uma rua que ninguém atravessava sem "
            "motivo. Tinha uma vitrine estreita, e atrás dela dezenas de "
            "pêndulos mediam o tempo cada um a seu modo, nenhum deles "
            "concordando com os outros.",
            "O relojoeiro chamava-se Teodoro e não gostava de visitas. Quando a "
            "porta abria, ele erguia os olhos da bancada apenas o suficiente "
            "para saber se valia a pena largar a pinça. Na maioria das vezes "
            "não valia, e ele voltava ao trabalho sem dizer nada.",
            "Naquela terça-feira, porém, quem entrou foi uma menina de uns dez "
            "anos carregando uma caixa de papelão grande demais para ela. "
            "Colocou a caixa sobre o balcão, empurrou-a até o meio e esperou. "
            "Teodoro largou a pinça.",
            "Dentro da caixa havia um relógio de parede desmontado em quarenta "
            "e sete peças, todas separadas por tamanho em saquinhos de papel. "
            "Alguém tinha feito aquilo com muito cuidado, e não tinha sido a "
            "menina.",
        ],
    ),
    (
        "Capítulo 2: As quarenta e sete peças",
        [
            "Teodoro espalhou as peças sobre o feltro verde da bancada. "
            "Reconheceu o mecanismo antes de reconhecer a assinatura gravada "
            "na placa de latão, e quando reconheceu a assinatura precisou "
            "sentar.",
            "A menina não perguntou nada. Puxou o banquinho do canto, sentou-se "
            "do outro lado da bancada e ficou observando as mãos dele, que "
            "tinham parado de se mover.",
            "— Era do meu avô — disse ela, por fim. — Ele falava que o senhor "
            "ia saber o que fazer.",
            "Teodoro pegou uma das engrenagens menores e girou-a entre o polegar "
            "e o indicador. O dente quebrado estava exatamente onde ele "
            "esperava que estivesse, e isso era a pior notícia possível.",
            "Havia três maneiras de consertar aquilo. Duas eram honestas e "
            "levariam meses. A terceira levaria uma tarde, e ele tinha jurado "
            "nunca mais usá-la.",
        ],
    ),
    (
        "Capítulo 3: O que o tempo faz",
        [
            "Trabalharam até escurecer. A menina passava as peças na ordem em "
            "que ele pedia, e aos poucos parou de errar. Teodoro reparou que "
            "ela decorava os nomes depois de ouvi-los uma vez só.",
            "Por volta das oito, o mecanismo fez um barulho seco e começou a "
            "andar. Nenhum dos dois comemorou. Ficaram os dois olhando o "
            "ponteiro dos segundos dar uma volta inteira, depois outra, até ter "
            "certeza de que ia continuar.",
            "— Quanto é? — perguntou a menina.",
            "— Volta amanhã — disse Teodoro. — Ainda falta a parte difícil.",
            "Ela voltou no dia seguinte, e no outro, e no outro. Ficou quatro "
            "anos voltando.",
        ],
    ),
]


def gerar(destino: str | Path) -> Path:
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    largura, altura = A5
    pdf = canvas.Canvas(str(destino), pagesize=A5)

    margem = 18 * mm
    largura_texto = largura - 2 * margem
    base_y = altura - margem - 10 * mm
    pagina = 1
    y = base_y

    def cabecalho() -> None:
        """Cabeçalho corrente e número de página: o extrator deve descartá-los."""
        pdf.setFont("Helvetica", 7)
        pdf.drawString(margem, altura - margem + 4 * mm, LIVRO.upper())
        pdf.drawCentredString(largura / 2, margem - 6 * mm, str(pagina))

    def nova_pagina() -> None:
        nonlocal y, pagina
        pdf.showPage()
        pagina += 1
        y = base_y
        cabecalho()

    def quebrar(texto: str, fonte: str, tamanho: float) -> list[str]:
        pdf.setFont(fonte, tamanho)
        linhas, atual = [], ""
        for palavra in texto.split():
            teste = f"{atual} {palavra}".strip()
            if pdf.stringWidth(teste, fonte, tamanho) > largura_texto:
                linhas.append(atual)
                atual = palavra
            else:
                atual = teste
        if atual:
            linhas.append(atual)
        return linhas

    cabecalho()

    for titulo, paragrafos in CAPITULOS:
        if y < base_y - 10:
            nova_pagina()

        # Título em corpo bem maior: a pista que o extrator usa.
        pdf.setFont("Helvetica-Bold", 15)
        for linha in quebrar(titulo, "Helvetica-Bold", 15):
            pdf.drawString(margem, y, linha)
            y -= 8 * mm
        y -= 4 * mm

        for indice, paragrafo in enumerate(paragrafos):
            linhas = quebrar(paragrafo, "Times-Roman", 10)
            pdf.setFont("Times-Roman", 10)

            for numero, linha in enumerate(linhas):
                if y < margem + 10 * mm:
                    nova_pagina()
                    pdf.setFont("Times-Roman", 10)
                # Recuo de primeira linha, exceto no parágrafo de abertura.
                recuo = 5 * mm if (numero == 0 and indice > 0) else 0
                pdf.drawString(margem + recuo, y, linha)
                y -= 5 * mm

    pdf.save()
    return destino


if __name__ == "__main__":
    caminho = gerar(Path(__file__).parent / "fixtures" / "livro_teste.pdf")
    print(f"PDF de teste gerado em {caminho}")
