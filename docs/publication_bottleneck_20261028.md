# Gargalo de publicação — investigação e correção de 28/10/2026

**Registro da primeira investigação.** O procedimento foi ampliado para todas
as classes em [Publicação do WahoJobs](publication.md). A recusa genérica de
mudanças coordenadas e o caminho exclusivo de rotina descritos abaixo foram
substituídos; os números e recibos originais permanecem históricos.

## Conclusão

O gargalo tem duas causas diferentes: o site depende de serviços com custos
reais de inicialização e publicação, e o procedimento tratava alterações de
apresentação como requalificações do rollout de contas. A correção implementada
separa essas classes e fornece um publicador curto para conteúdo/estilo, com
retorno e retomada duráveis. Ela não transforma uma alteração de API em conteúdo.

O caso completo das empresas ainda depende de uma API nova e de ativos S3.
Seu código e sua pesquisa foram preservados. O caminho curto foi ensaiado com
uma alteração independente de estilo sobre uma cópia desse código real.
Não houve publicação, acesso privado ou mudança do orçamento histórico.

## O que foi examinado

- `AGENTS.md` anterior, procedimentos de contas e de publicação exata, CI do
  website e do backend, cliente Vercel existente, controle de publicação e
  protocolo/registro compartilhado de coordenação.
- Website em `.worktrees/remote-company-catalog`: candidato `8ea2cc5` sobre
  `e5bbc22`, 16 arquivos alterados. Backend em `.worktrees/remote-company-inventory`:
  candidato integrado `0867c2b` sobre `2c357f8`, com o delta funcional de quatro
  arquivos do leitor de empresas e sua documentação de integração.
- Pesquisa, auditorias públicas, logos, builds e testes preservados em
  `outputs/remote-companies-20261021`; correções e plano proposto para 29/10 em
  `outputs/remote-companies-20261029-publication`.
- As autorizações originais de 22/10 e 23/10. Elas aprovavam planos específicos;
  não são tratadas como concessões permanentes ou automaticamente renovadas.

Os pins de produção nesses documentos são históricos. Nenhuma consulta ao host
ou a painel privado foi usada para apresentá-los como estado atual de produção.

## Limites reais, exigências do dono e escolhas do procedimento

| Assunto | Origem e implicação |
|---|---|
| Website Next/Vercel, backend Python no DigitalOcean e ativos S3 | Arquitetura real. Alterar texto/CSS não exige reimplantar os outros serviços. A mudança completa das empresas, porém, chama `/_catalog/jobs/companies`, um contrato novo. |
| Leitor do catálogo preparado em memória, lease do banco e inicialização custosa | Limite real do backend. Seu código exige preparação antes da prontidão. Não se elimina esse custo reduzindo um timeout; ele simplesmente não participa de uma alteração que só troca o artefato do website. |
| Duas implementações de rollback | Requisito real de compatibilidade entre componentes. Um retorno de código deve preservar os dados atuais; snapshots não devem sobrescrever dados posteriores. O fluxo de rotina não tem operação de banco. |
| Preparar tudo antes do acesso, respeitar segurança, dados e coordenação | Exigências explícitas suas, mantidas. A revisão operacional foi expressamente autorizada neste pedido. |
| Oito horas | A frase anterior especificava **“for this account rollout”**. Tratá-la como mínimo de publicação de empresas ou de CSS era ampliação de escopo. A janela não era concedida por esse texto. |
| Duas horas para sign-ins humanos | O texto condicionava isso à existência de sign-ins. Introduzir Google/My Jobs/logout numa alteração que não muda contas cria a própria condição que torna o plano longo. |
| Uma hora de contingência e reservas de manutenção | Eram exigências explícitas e amplas do AGENTS anterior, não podem ser atribuídas apenas ao agente. A nova autorização permite restringi-las ao rollout/operação que necessita desses recursos. Teto, débitos e autorizações históricas continuam intactos. |
| Auditoria integral antes de cada chave e contagem legada exatamente 237 | Escolha dos helpers. A identidade de um candidato e o contrato de disponibilidade importam; repetir todo o censo público e exigir contagem fixa não é uma limitação intrínseca da publicação Vercel. O serviço de empresas já consulta o diretório atual. |
| Esperar o próximo health horário, observar 90 minutos e pedir 90 minutos de aceitação humana | Etapas do plano específico; não são requisitos do comando de publicação nem mudanças necessárias de conta demonstradas no diff das empresas. Precisam de justificativa por risco, não herança automática. |
| Requalificar fontes por mudança de dia, copiar pacotes e criar sucessivos namespaces | Implementação do processo. Evidência sobre fonte imutável não envelhece só pela data. Estado mutável de produção deve ser observado de novo, sem restampar os resultados antigos. |
| Exclusão global de trabalhos | O registro legado realmente tem um protocolo global. Não é seguro ignorá-lo com outra trava. O novo operador participa dele; preparação independente deixa de depender de sua liberação. A mutação ainda é serializada durante a curta publicação. |
| Regras do domínio A1 em `docs/agent_operations.md` | Especificação de um domínio dormente do produto. O próprio documento diz que A1 não executa nada. Não é uma camada extra de concessão de acesso para este operador. |

## O caso concreto

A auditoria pública original encontrou 21 empresas no catálogo de vagas, nove
perfis já existentes e 12 ausentes. Os 12 perfis e seus logos foram pesquisados;
a interface evita transformar falha de inventário em zero oportunidades. Isso
continua preservado no commit `8ea2cc5`.

O plano para 29/10 reservava **15 horas**, com **13h58min20s** de caminho completo
e **12h26min50s** do procedimento privado. A soma nominal das fases era 55.370 s;
o caminho crítico era menor por sobreposição. Entre as parcelas estavam:

| Parcela do plano | Reserva |
|---|---:|
| Auditor público e supervisor | 80min30s |
| Dois censos privados completos | 69min |
| Espera pelo health horário | 66min40s |
| Observação aquecida | 93min |
| Aceitação humana dedicada | 90min |
| Envio condicional dos logos | 50min |
| Bootstrap da chave | 20min |
| Nova transição de backend | 44min |

Esses são **orçamentos**, não medições do tempo efetivamente gasto. Não podem ser
comparados com um ensaio local como se fossem latência de produção medida.

Os arquivos registram duas capturas públicas com falha, correções sucessivas de
controle e uma retomada não iniciada depois de perder o limite de início por
15 segundos. Também registram que uma operação de firewall terminou com limpeza
sem publicar backend, frontend ou logos. Isso mostra que grande parte do esforço
se consumiu na própria preparação/admissão, antes de alterar o produto.

O teto de 769.600 s e a proteção carregada de 712.820 s no plano eram contabilidade
protetiva. O débito histórico efetivo permanecia desconhecido. Este trabalho não
o converteu em zero nem devolveu capacidade de operações anteriores.

## Simplificação implementada

O novo `AGENTS.md` separa rotina de alterações coordenadas, permite preparação
independente, reaproveita validações por seus insumos e concentra a decisão de
produção no artefato concreto. Os mínimos excepcionais não são aplicados ao
website quando a operação não usa contas/host privado.

`scripts/routine_release.py` implementa:

1. Classificação fechada do diff contra a versão aceita. Arquivos desconhecidos
   e contratos de serviço não passam automaticamente. No JSON de empresas, o
   caminho editorial preserva identidade, mapeamento, links e participação no
   diretório; não permite disfarçar uma empresa nova como simples descrição.
2. Testes/build com recibos originais e cache ligado a arquivos, lockfiles,
   comandos, ambiente não secreto e ferramentas. Falhas conservam seus logs.
   Mudança de fonte relevante invalida a prova; uma mudança independente pode
   conservar resultados cujo conjunto de insumos permanece igual.
3. Plano ligado aos commits, equipe/projeto, deployment candidato exato e
   deployment aceito. A publicação recebe um artefato já construído para produção
   e não inicia novo build, SSH, alteração de ambiente ou migração.
4. Admissão durável, participação na trava/registro legado, intenção antes da
   promoção e reserva sobrevivendo ao processo. Não reescreve linhas alheias.
5. Observações atuais dos aliases, checagens semânticas limitadas e confirmação
   de término da promoção no provedor. Convergência dos aliases, sozinha, não
   libera uma operação que ainda está pendente.
6. Retorno automático de falha conhecida para a versão aceita. Resultado incerto
   é observado sem repetição; sucessor alheio ou aliases mistos impedem retorno
   destrutivo. Dados atuais permanecem intocados.

O protocolo limita os recursos: nenhum token é colocado no plano; usa a sessão
Vercel existente. O hash passado pelo operador confirma o plano exato, mas não
é apresentado como uma assinatura criptográfica da autorização humana.

Há instruções curtas em `docs/routine_publication.md`, configuração de exemplo
em `deploy/routine-website.example.json` e CI de recuperação sem credenciais em
`.github/workflows/routine-publication.yml`. O workflow foi preparado localmente;
não é apresentado como um workflow já enviado ou executado no GitHub.

## Ensaio e limites da evidência

O ensaio cria uma cópia do website real e altera apenas a margem de rolagem da
seção de vagas de empresas, de 24 para 32 px. Executa os testes existentes,
o build Next, o servidor Next real e requisições HTTP às páginas de empresas.
Usa os fixtures públicos já preservados, com chave sintética e rede de execução
restrita ao loopback. O build usa apenas as leituras públicas delimitadas pelo
verificador existente. A fonte de produto validada não é alterada.

A Vercel é simulada por um serviço HTTP local e um executável de transporte de
teste. O mesmo publicador faz as observações, reservas, promoção, retorno e
retomada. Não foram medidos DNS/TLS, fila, latência, políticas de proteção ou
credenciais atuais da Vercel de produção.

O primeiro ensaio completo mediu 121,062 s, dos quais 107,765 s de preparação;
reaproveitar a preparação levou 0,610 s. Os quatro cenários concluíram em
2,250–2,828 s cada no provedor local. Essa medição permanece no recibo original
`outputs/publication-simplification-20261028/rehearsal-01/report.json`.

A revisão de retomada acrescentou confirmação explícita do job assíncrono do
provedor e tratamento de timeout de qualificação. A rodada final, com o hash do
operador final confirmado, mediu **62,516 s** no total: **48,875 s** de preparação,
**0,781 s** para reaproveitá-la e **2,547–3,516 s** por cenário no provedor local.
O resultado está em
`outputs/publication-simplification-20261028/rehearsal-01/cycle-1793222520573426000/report.json`.
O primeiro resultado não foi restampado ou apresentado como se usasse essa revisão.

Passaram os **26 testes do novo operador**. Na cópia do produto, a suíte existente
teve 104 resultados aprovados e quatro skips opcionais preexistentes, sem falhas;
o build Next, incluindo lint e tipos, terminou com sucesso. Os quatro cenários
HTTP finais preservaram cinco registros de dados sintéticos — o inicial e os
quatro criados após promoção — incluindo os dados criados antes de um retorno.

Os testes do novo operador cobrem sucesso, pré-verificação com versão inalterada,
retorno, resposta perdida, morte do processo, promoção ainda pendente, reserva
remanescente, identidade incorreta, sucessor alheio, trava entre processos,
retomada do retorno, orçamento integral e invalidação/reuso das qualificações.

## Como esse resultado libera a fila

Uma alteração independente de CSS/descrição não abre relógio de acesso privado,
não recria a publicação do backend, não espera ciclos agendados e não precisa
da participação do dono em login social. Depois da preparação válida e do
artefato pronto, resta uma decisão concreta e uma publicação curta com recuperação.

Não foi concedido acesso permanente nem removido um bloqueio alheio. Quem publica
o mesmo projeto precisa continuar usando o coordenador comum. Enquanto houver
publicadores legados, esse coordenador ainda serializa mutações de produção;
não impede outros trabalhos de preparar código, testes e pacotes.

O conjunto original das empresas continua sendo uma publicação coordenada de
API + website + ativos. Deve conservar os artefatos já válidos e reobservar apenas
suas dependências mutáveis, com um plano específico. Oito horas, login social e
um novo censo integral não são pré-requisitos automáticos desse novo plano.
O fluxo rápido não pode ser usado para publicar uma interface que promete um
endpoint ainda não aceito.

Não há, portanto, uma afirmação de que a publicação completa das 12 empresas foi
executada ou de que sua implantação de backend cabe em minutos. O ganho de rotina
foi demonstrado de ponta a ponta localmente; a medição final no provedor real
fica para a primeira publicação concreta autorizada, sem abrir acesso privado
apenas para produzir um número.

Referências primárias de infraestrutura: [deploy sem atribuição de domínios](https://vercel.com/docs/cli/deploy)
e [promoção, status e semântica de timeout](https://vercel.com/docs/cli/promote).
