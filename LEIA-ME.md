# Robô de Prospecção de Obras — XYZ Tecnologia em Concreto

Todo dia às 07:50 (horário de Belém) o robô faz isto:

1. Baixa o **Diário Oficial do Estado do Pará** (IOEPA) e lê só as páginas com publicações de terceiros ("torna público…").
2. Baixa o **Diário Oficial de Belém** (PMB) e o de **Ananindeua** (PMA) direto dos sites das prefeituras.
3. Separa os pedidos e as licenças ambientais de Belém, Ananindeua, Marituba, Santa Isabel e Castanhal.
4. Dá uma nota de "parece obra". LP/LI de edifício, condomínio ou galpão recebe nota alta. LO e renovação recebem nota baixa.
5. Grava os leads na coleção `prospeccao_obras`, que já aparece no app.

As três fontes são lidas na mesma rodada, na mesma janela de dias.

O robô verifica os últimos 3 dias e não duplica. Um lead já gravado nunca é sobrescrito, então status, fase e anotações da equipe ficam preservados.

Ele roda de graça no **GitHub Actions**. Não precisa de servidor nem de computador ligado.

### Como cada fonte é lida

| Fonte | Como o robô acha |
|---|---|
| DOE-PA (IOEPA) | PDF do dia em `ioepa.com.br/arquivos/AAAA/AAAA.MM.DD.DOE.pdf`, filtrando páginas com "torna público" |
| DOM Belém (PMB) | `sistemas.belem.pa.gov.br/diario-consulta-api/diarios` — lista por data; o PDF é a mesma rota do detalhe com `Accept: application/octet-stream` |
| DOM Ananindeua (PMA) | `ananindeua.pa.gov.br/diario_oficial` — a página traz os PDFs com a data ao lado |

Nos diários municipais não existe "torna público", então o texto é cortado em **janelas ancoradas** nos títulos de contrato (`Contrato Administrativo`, `Contratação de empresa`, `Objeto:`, `Extrato de contrato`…), com 450 caracteres de recuo para pegar o nome da empresa, que vem antes do título.

### Portão de qualidade

Só vira lead o que tem **identidade de empresa** — CNPJ ou sufixo societário (LTDA, SPE, EPP, S/A). O que passava antes disso era texto corrido do próprio Diário: "ublicado no Diário Oficial nº 36.582" entrava como nome e o bloco recebia nota 3.

Também entram na penalidade compras de consumo (medicamento, material de expediente, uniforme): elas não põem tijolo e casavam com "armazém" e "distribuição".

## Instalação (uma vez só)

### 1. Chave do Firebase (conta de serviço)
1. No console do Firebase, abra o projeto **prospectar-comercial** e vá em ⚙️ **Configurações do projeto → Contas de serviço**.
2. Clique em **Gerar nova chave privada**. Vai baixar um arquivo `.json`.
3. **Esse arquivo dá acesso total ao banco.** Não mande por WhatsApp, não suba no Netlify e não coloque no repositório.

### 2. Repositório no GitHub
1. Crie um repositório **privado**, por exemplo `robo-prospeccao`.
2. Envie estes arquivos mantendo as pastas:
   - `robo_doe.py`
   - `requirements.txt`
   - `.gitignore`
   - `.github/workflows/robo-doe.yml`

### 3. Segredos
No repositório, vá em **Settings → Secrets and variables → Actions → New repository secret**:

| Nome | Valor |
|---|---|
| `FIREBASE_SERVICE_ACCOUNT` | o conteúdo inteiro do `.json` baixado no passo 1 |
| `CONTATO_NOMINATIM` | (opcional) um e-mail de contato. O serviço de mapas OpenStreetMap pede um contato de quem usa |

Depois de colar o JSON como segredo, apague o arquivo `.json` do computador.

### 4. Primeiro teste
Vá em **Actions → Robô DOE-PA → Run workflow**. Na primeira rodada, coloque **dias = 30** para trazer o último mês.

Abra a execução e leia o log. No final aparece o resumo de cada dia, por exemplo:
`Resumo 30/09/2026: 12 publicações das 5 cidades, 3 parecem obra, 3 novas gravadas`.

Esse resumo responde à pergunta do teste: **quantas obras por mês o DOE realmente rende.**

## Ajustes
- **Nota mínima**: o padrão é 5. Para receber menos ruído, crie a variável `NOTA_MINIMA` com o valor `7` (só Quentes). Isso exige acrescentar `NOTA_MINIMA: "7"` no bloco `env` do workflow.
- **Horário**: altere a linha `cron` do workflow. O horário é em UTC; Belém é UTC−3.
- **Histórico**: cada execução fica registrada na coleção `robo_execucoes`.

## Testar no computador (opcional)
```
pip install -r requirements.txt
python robo_doe.py --dry-run --data 2026-09-30   # mostra sem gravar
python robo_doe.py --pdf diario.pdf --dry-run    # testa com um PDF baixado
```

## O que ele rende hoje

Rodada de 12 dias em 06/10/2026, com as três fontes ligadas: **10 leads novos** em 6min17s, totalizando 14 na coleção — 8 Belém, 4 Ananindeua, 2 Castanhal, todos geolocalizados.

Dois vieram do DOE-PA, quatro do DOM de Belém e três do de Ananindeua. Exemplos reais gravados: `CONSTRUTORA SOBERANA LTDA` (contratação de engenharia de conservação predial, nota 10), `LONART ARTEFATOS DE LONA LTDA` (execução de serviços de reforma, nota 13), `MANALI OBRAS LTDA` (nota 9), `SUPER POSTO INDEPENDÊNCIA LTDA` (terraplanagem, LP, nota 8).

## Limitações conhecidas
- **Marituba, Santa Isabel do Pará e Castanhal só aparecem pelo DOE-PA.** As prefeituras dessas cidades não publicam diário em endereço estável que o robô saiba ler; o DOE estadual as cobre, e é por lá que Castanhal entra.
- **Obra pequena com apenas alvará** não aparece em nenhuma das fontes; para essas, use o registro em campo do app.
- **A leitura é por padrão de texto.** Publicação fora do formato comum pode sair com nome ou endereço vazio. O trecho original fica salvo no lead para conferência.
- **O endereço vem do OpenStreetMap**, que é fraco em endereços como "Tv. WE 30". Quando não acha, o pin vai para o centro da cidade marcado como "aproximada". A equipe corrige com o GPS na visita.
- **O endereço do PDF foi verificado** (`ioepa.com.br/arquivos/AAAA/AAAA.MM.DD.DOE.pdf`). Se a IOEPA mudar o site, o robô passa a registrar "sem edição" todo dia. Se o log mostrar isso vários dias seguidos, o endereço mudou.
- **O endereço dos diários municipais também foi verificado** em 06/10/2026. Se a prefeitura mudar o site, a linha `DOM Belém (PMB)` ou `DOM Ananindeua (PMA)` passa a aparecer como `lista indisponível` no log — o DOE continua rodando normalmente.
