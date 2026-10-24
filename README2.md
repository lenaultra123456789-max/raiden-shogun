# Shogun's Garden + Raiden Shogun

Projeto inicial com site público e dashboard local responsivo.

## Executar no Windows
1. Extraia o ZIP.
2. Abra o PowerShell na pasta do projeto.
3. Execute `py -m pip install -r requirements.txt`.
4. Execute `py app.py`.
5. Abra http://127.0.0.1:5000 e o painel em http://127.0.0.1:5000/dashboard.

## Importante
O painel salva configurações localmente em `settings.json`, mas ainda não está conectado ao bot Discord. Login OAuth2, autorização de administradores, banco de dados de produção, deploy HTTPS e webhook Mercado Pago com validação precisam ser implementados antes de expor na internet. Não coloque tokens no frontend. `config.example.env` é apenas um modelo; copie para `.env` e preencha no backend quando a integração for configurada. Edite os exemplos de serviços para refletirem sua oferta real.
