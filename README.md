# Monitor de leilões do WoW Forever

Busca a cada 30 minutos os preços da Casa de Leilões do WoW Forever
pela API oficial da Blizzard e mostra o que vale fabricar com Alfaiataria e Encantamento.

- `fetch_prices.py`: busca os preços (só biblioteca padrão do Python).
- `fetch_recipes.py`: baixa as receitas de todas as profissões do Forever (rode com a opção "receitas").
- `config.json`: itens vendidos por NPC (preço e onde comprar). Edite aqui.
- `index.html`: painel.
- `.github/workflows/wow-leiloes.yml`: roda a cada 30 minutos.
- Os dados ficam no branch `wow-leiloes-dados`.

## Configurar

1. Crie um cliente em https://develop.battle.net (Create Client).
2. No GitHub: Settings → Secrets and variables → Actions → **New repository secret**:
   - `BLIZZARD_CLIENT_ID`
   - `BLIZZARD_CLIENT_SECRET`
3. Opcional, em **Variables**: `WOW_REGION` (`us`), `HOUSE_FILTER` (`Aliança|Alliance`),
   `HOUSE_IDS` (ids separados por vírgula), `RULESET_ID`.
4. Actions → **Leilões WoW Forever** → **Run workflow**.

## Testar local

```
python3 fetch_prices.py --demo   # gera dados de exemplo
python3 -m http.server           # abra http://localhost:8000
```

A API do Forever não tem documentação oficial: em vez de reinos, ela usa "rulesets" e várias
casas de leilões. Na primeira execução o log mostra todas as casas encontradas.
