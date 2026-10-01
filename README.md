# NFe CLI

CLI para emissão de NFS-e pelo Sistema Nacional (Sefin Nacional / ADN), com assinatura do DPS via certificado digital A1 (.pfx) e geração de DANFSe em PDF.

## Instalação

> Pressupõe que o Python 3 já esteja instalado. Caso não esteja, veja as instruções de instalação em [python.org/downloads](https://www.python.org/downloads/).

```bash
pip install -r requirements.txt
```

Na primeira execução, se não existir um `config.json`, o programa cria o arquivo automaticamente a partir de `config.json.example` e pergunta, ali mesmo no terminal:

1. Se você quer usar o ambiente de produção em vez de homologação (padrão).
2. O CNPJ/CPF e o código IBGE do município do prestador (campos obrigatórios).

Para o CNPJ/CPF, se houver exatamente um arquivo `.pfx` na pasta do projeto, o programa usa esse certificado automaticamente: pede a senha dele e extrai o CNPJ/CPF do próprio certificado, em vez de perguntar o documento manualmente. Se não quiser esse comportamento (por exemplo, para digitar o documento manualmente mesmo havendo um `.pfx` na pasta), use a flag `--dont-use-pfx-file`:

```bash
python3 main.py --dont-use-pfx-file
```

Para o código IBGE do município, o programa tenta preencher o campo automaticamente, nesta ordem, e sempre deixa o valor sugerido pré-preenchido (você pode aceitar com Enter ou digitar outro):

1. **NFS-e local mais recente** — se já existir algum XML emitido anteriormente em `dados/<ambiente>/xml/`, o código do município é reaproveitado dela.
2. **BrasilAPI** (apenas para CNPJ) — consulta `https://brasilapi.com.br/api/cnpj/v1/<cnpj>` e usa o código de município cadastrado na Receita Federal para aquele CNPJ.
3. Se nenhuma das opções acima funcionar (sem internet, CPF, ou API fora do ar), o campo fica em branco e precisa ser digitado manualmente.

Com isso o `config.json` já fica pronto e a execução continua normalmente — não é preciso rodar o programa de novo. Os demais campos (certificado, senha, padrões de serviço etc.) ficam com os valores padrão do exemplo e podem ser ajustados depois editando `config.json`. Se preferir, você também pode copiar o arquivo manualmente antes de rodar:

```bash
cp config.json.example config.json
```

Edite `config.json` com os dados do prestador antes do primeiro uso.

## Certificado digital

O emissor assina a DPS usando um certificado digital A1 no formato `.pfx` (ou `.p12`).

Há duas formas de indicar qual certificado usar:

1. **Automático** — coloque o arquivo `.pfx` na pasta do projeto (mesmo diretório do `main.py`) e deixe o campo `"certificado"` vazio (`""`) no `config.json`. Ao rodar, o programa procura automaticamente por um `.pfx` na pasta e o utiliza.
   - Se não encontrar nenhum `.pfx`, o programa para com um erro pedindo para configurar o caminho.
   - Se encontrar **mais de um** `.pfx` na pasta, o programa também para e pede para você especificar explicitamente qual usar (via `certificado` no config), já que não há como adivinhar o certificado correto.

2. **Explícito** — preencha o campo `"certificado"` no `config.json` com o caminho do arquivo, por exemplo:

   ```json
   "certificado": "meu-certificado.pfx"
   ```

   Caminhos relativos são resolvidos a partir da pasta do projeto; também é possível usar um caminho absoluto (ex: `/Users/voce/certificados/empresa.pfx`).

> Certificados `.pfx` nunca devem ser commitados no git — o `.gitignore` do projeto já ignora `*.pfx` por padrão.

## Senha do certificado

A senha do certificado pode ser fornecida de três formas, nesta ordem de prioridade:

1. **No `config.json`**, no campo `"senha_certificado"`. Se preenchido, o programa usa essa senha diretamente, sem perguntar nada.
2. **Via variável de ambiente** `NFSE_CERT_SENHA` (útil para scripts/automação sem deixar a senha em disco).
3. **Perguntada no terminal** — se nenhuma das opções acima estiver definida, o programa pede a senha interativamente (via `getpass`, então ela não aparece na tela ao digitar).

Se você optar por guardar a senha no `config.json`, o programa verifica a permissão do arquivo (em sistemas POSIX) e avisa caso ele esteja legível por outros usuários do sistema, sugerindo rodar:

```bash
chmod 600 config.json
```

## Importar uma NFS-e/DPS a partir de um XML

É possível pré-preencher os dados de emissão a partir de um XML de NFS-e ou DPS já existente, usando a flag `--importar`:

```bash
python3 main.py --importar caminho/para/nota.xml
```

Isso extrai os dados do tomador, serviço e valores do XML informado e os usa como base para a nova emissão (equivalente a "replicar" essa nota), sem precisar redigitar tudo manualmente.

## Últimas NFS-e como base para uma nova emissão

Ao iniciar o programa (sem `--importar` nem `--dry-run`), ele sincroniza com o ADN e lista as últimas NFS-e emitidas (as mais recentes, guardadas em `dados/<ambiente>/recentes.json`):

```
Últimas NFS-e emitidas:
 [1] 2026-09-28  nº 123    Fulano de Tal                  R$     500.00  Consultoria em TI
 [2] 2026-09-20  nº 122    Ciclano Ltda                    R$    1200.00  Desenvolvimento de software
 [n] nova, preencher manualmente
 [q] sair
Replicar qual? [1]:
```

Basta escolher o número da nota desejada (padrão `1`, a mais recente) para reaproveitar todos os dados dela — tomador, serviço, alíquota etc. O programa então pede apenas o **novo valor do serviço**, já com o valor anterior como sugestão padrão; é só confirmar (Enter) ou digitar o novo valor. A competência é automaticamente atualizada para a data atual.

Se preferir preencher tudo do zero, escolha `n`.

## Uso básico

```bash
# emitir uma nova NFS-e (modo interativo)
python3 main.py

# emitir em produção em vez de homologação (padrão do config.json)
python3 main.py --ambiente producao

# gerar e assinar o XML sem enviar (teste)
python3 main.py --dry-run

# importar dados de um XML existente
python3 main.py --importar nota-anterior.xml

# gerar o PDF (DANFSe) de uma NFS-e já emitida
python3 main.py --pdf

# gerar o PDF de uma chave de acesso específica
python3 main.py --pdf 41277002245263801000188000000000001626090973474823

# não gerar o PDF automaticamente após emitir
python3 main.py --sem-danfse

# usar um arquivo de config alternativo
python3 main.py --config outro-config.json
```

## Estrutura de dados local

```
dados/<ambiente>/
  xml/          XMLs das NFS-e emitidas (nomeados pela chave de acesso)
  estado.json   último nDPS utilizado
  recentes.json últimas NFS-e emitidas (usadas para "replicar")
  dry-run/      DPS assinadas geradas com --dry-run
  erros/        DPS rejeitadas pela Sefin
```
