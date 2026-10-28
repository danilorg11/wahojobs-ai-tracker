# Publicação do WahoJobs

Este é o processo padrão para todas as publicações. `AGENTS.md` tem a mesma
abrangência. Planos antigos permanecem como registros de suas operações; o
publicador v1 de rotina é compatibilidade histórica, não uma segunda fila.

## Cinco passos para o operador

1. Identifique a versão realmente aceita e o diff. Separe componentes que podem
   funcionar sozinhos; declare dependências somente quando o consumidor precisa
   do contrato novo. Use `deploy/publication/profiles.json`.
2. Prepare CONFIG v2 com `checks` e `steps`: recursos, domínio, executável fixado,
   observação, alteração, teste, recuperação, limpeza, limites e sua justificativa.
   Uma transição acoplada fica inteira numa etapa transacional de domínio.
3. Execute `python -B scripts/publication.py prepare CONFIG --out PLANO`.
   Código, testes, build, revisão e recuperação ficam antes do acesso privado.
   A saída informa hash e limite completo. Recibos válidos são reaproveitados com
   a data original; falhas conservam o log e não invalidam os outros resultados.
4. Apresente fontes, artefatos, retorno, recursos, tempos e acesso na decisão final
   de produção. Registre a decisão real num arquivo privado; o exemplo
   `authority.proposal.json` não é concessão. Após handoff dos publicadores e a
   decisão efetiva, execute `python -B scripts/publication.py run PLANO
   --approved-plan HASH --authority CONCESSAO`.
5. Para retomar, repita o mesmo comando/PLANO. `accepted` é reobservado sem novo
   deploy; `recovered` preserva a versão compatível; `unknown` conserva a reserva;
   `attention` identifica o impedimento; dependentes aguardam apenas seu produtor.
   Outra publicação pode carregar a etapa exata já aceita, referenciando o recibo
   original. Nunca crie outra operação para contornar um resultado incerto.

Ensaio da fila:
`python -B scripts/publication_queue.py rehearse outputs/ENSAIO-NOVO`.
Se a preparação falhar, `... publication_queue.py resume outputs/ENSAIO-NOVO`
conserva fontes, respostas salvas e testes aprovados. Usa recursos locais, sem
autorização de produção. Os comandos são trabalho do operador, não etapas de
intervenção do dono.

`python -B scripts/publication.py authority-proposal PLANO --out PROPOSTA`
materializa recursos, assinaturas e hashes exatos dos parâmetros. Sai com
`proposal_not_a_grant`: a decisão real ainda precisa ser registrada pelo operador.
Para host privado, o relógio de manutenção começa na abertura real da janela,
`window_started_at`; tempo já consumido mais o procedimento restante precisam
caber na capacidade aprovada. O teto e os registros anteriores permanecem intactos.

Ao alterar o protocolo de recuperação, o ensaio com interrupção real é
`python -B scripts/rehearse_publication_recovery.py OUTPUT_NORMAL OUTPUT_FALHA_NOVO`.
Ele usa a transação original, interrompe o processo após o commit, conserva dados
novos e verifica a nova tentativa conhecida. Não é uma obrigação a cada texto/CSS.

## Caminhos proporcionais e executáveis

| Efeito | Worker | Qualificação e recuperação |
|---|---|---|
| Texto, imagem, estilo | `files` ou `vercel` | Fonte, renderização afetada, artefato e retorno exato. |
| Página, interface, funcionalidade | `vercel` | Build de produção e contrato afetado. Construa uma vez sem domínios; promova esse ID imutável. |
| API/backend | `files`, hooks nativos, `native_gate` | Produtor/consumidor, inicialização, dados atuais e retorno compatível. Troca condicional de ponteiro e chave da política. |
| Dados/schema | `module` do domínio; `sqlite` somente para transformação explicitamente revisada | Ensaio da transação, preservação, retomada e leitor/escritor compatíveis. Commit e recibo juntos; nenhuma restauração de banco. |
| Integração/coleta | `module` | Contrato, identidade, quotas e reconciliação nativa. Resposta salva não exige nova coleta HTTP. |
| Contas/autenticação | Uma etapa `module` contendo a operação finita de domínio; `files` para código compatível | Identidade, sessão e aceitação real do contrato alterado. Enable/test/saída compatível na mesma etapa. Migração é explícita. |
| Infraestrutura | `files` com hooks nativos ou `module` do provedor | Configuração, inicialização real e reverso completo. Claim no host conflita com seus serviços e bancos filhos. |

As sete famílias usam o mesmo preparador e executor; as categorias de evidência
necessárias são verificadas. Não existe recusa genérica de mudanças coordenadas.
Uma etapa pode executar qualquer domínio revisado que implemente o protocolo:
JSON por stdin/stdout, mesmo ID/assinatura, ações `observe`, `apply`, `check`,
`recover`, `cleanup`. Estados permitidos estão em `scripts/publication.py`.
Executáveis e entradas reais são fixados; um adaptador não concede acesso.

`files` troca arquivo, symlink ou somente uma chave JSON. O reverso da chave
conserva os demais campos. `native_gate` usa o inode de manutenção já existente.
O domínio continua responsável por lease, transação, captura, OAuth/identidade e
limites do provedor. Não use o SQL genérico para contornar o schema protegido do
produto. Migrações sem retorno compatível precisam de expansão/contração ou
reparo adiante qualificado dentro da mesma etapa, antes de serem admitidas.

Código compatível e reparo de conteúdo podem usar `scripts/publication_beta.py`:
uma etapa, um gate nativo e uma parada/inicialização. O retorno mantém o conteúdo
aceito e troca somente código/política. Os dois leitores precisam ser compatíveis
com os dados; agrupar componentes incompatíveis para economizar tempo é inválido.
Inclua planos de dados, artefatos e código nativo em `worker.files`: um caminho
para um arquivo mutável não fixa seus conteúdos.

Quando uma autenticação alterada só puder ser aceita depois de ativada, use
`human_acceptance.mode=inside_finite_domain`: o módulo contém enable/test/saída,
um controlador de recuperação independente, categoria qualificada `finite-exit`
e `finite_account_exit=true`. O indicador não instala o controlador: sua
implementação real e seu teste de interrupção são parte da qualificação. A
aceitação humana é conferida depois do domínio, antes do aceite final; ausência
ou rejeição aciona a saída compatível. O recibo pode ligar o ator e o resultado
ao `effect_signature`, permitindo reaproveitá-lo quando apenas o procedimento
mudar. Esse caminho permite aceitar a funcionalidade no próprio ensaio finito.

## Tempo, evidência e decisões

O limite completo soma observações, aplicação, testes, recuperação e limpeza,
mais 10% ou 5 segundos de contingência. É limite de admissão, não espera
obrigatória. Hooks internos precisam caber na fase externa; todo o caminho ainda
precisa caber na concessão. Um teste terminado segue imediatamente. Calendário
não invalida resultado imutável; estado mutável é reobservado antes da alteração.

No backend atual há preparação de 600 s, prontidão de 630 s e limite systemd de
660 s. O perfil preserva observador de 690 s e parada de 90 s tanto no candidato
quanto no retorno. Meça no Linux antes de diminuir esses limites. Eles não
participam de um deploy só de interface. Não há mínimo global de oito horas,
observação de 90 minutos, login social ou espera pelo próximo health horário.

As 58 descrições Mindrift sem resposta completa preservada exigem novas
observações sob quotas reais: 70 HTTP, 360 s, 20 detalhes por coleta e cooldown
de 12 h do domínio. As 15 respostas já salvas exigem zero HTTP e não renovam
disponibilidade. Prolific continua sem evidência suficiente; publicar seu código
de avaliação não cria uma entrega útil de cobertura.

## Transição dos canais existentes

`python -B scripts/publication_transition.py prepare --out DIRETORIO` prepara o
inventário dos nove canais e os substitutos dos workflows que faziam
SSH/migração/restart diretamente. Os substitutos apenas constroem artefatos.
Arquivo local não comprova workflow remoto desativado ou ausência de runs.

O responsável verifica estado atual, runs/intenções pendentes, instala substitutos
e guards, registra evidência original por hash e aprova a proposta concreta.
`activate PROPOSTA --receipts RECIBOS --approved-plan HASH` exige essa evidência.
`scoped` requer todos os publicadores no coordenador canônico. `legacy-bridge`
admite canais comprovadamente inativos e conserva a exclusão antiga. Um canal
capaz de publicar fora do protocolo impede ativação; um novo lock não o cerca.

O coordenador canônico é `outputs/publication-state`, com SQLite FULL sync,
reservas duráveis e diário. Todas as threads do recurso usam esse mesmo local
no host coordenador. Não o copie para cada worktree/runner. Workers remotos têm
seu próprio diário de domínio e recebem o mesmo ID. O modelo systemd em
`deploy/publication/` não concede root nem acesso ao banco: usuário, paths e
comandos específicos são preparados e autorizados na instalação.

Uma decisão final pode autorizar o handoff e o lote preparado juntos. Não faça
uma sequência de pedidos por teste, chave e reinício. Aceitação humana adicional
só participa se o contrato alterado realmente a requer, como OAuth. O hash/JSON
vincula a operação; a autoridade vem da decisão humana real e do acesso concedido.
Preserve todos os ledgers, tetos, prazos e resultados incertos originais.
