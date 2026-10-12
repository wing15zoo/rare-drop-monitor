# RARE DROP monitor

1. Crée un repo GitHub **public** (minutes Actions illimitées) et dépose ces fichiers.
2. Discord → paramètres du salon → Intégrations → Webhooks → Nouveau → copier l'URL.
3. GitHub → Settings → Secrets and variables → Actions → New secret :
   - `DISCORD_WEBHOOK` = l'URL du webhook
   - `DISCORD_ROLE_ID` (optionnel) = ID du rôle à pinger (Discord en mode développeur → clic droit sur le rôle → Copier l'identifiant)
4. Onglet Actions → "RARE DROP monitor" → Run workflow pour tester. Regarde les logs : chaque produit doit afficher `rupture` ou `EN STOCK`, pas `HTTP 403`.

`products.json` : 15 produits Play-in (mode `jsonld`, lit le statut schema.org de la page).
Une page retirée (404) compte comme rupture : si Play-in remet le produit en ligne, l'alerte part.
Options par produit : `role_id` (rôle spécifique), `max_price` (pas d'alerte au-dessus de ce prix), `disabled: true`.
Autres modes : `shopify` (boutiques Shopify) et `keywords` (mots-clés dans la page).

Test local : `pip install requests` puis `DISCORD_WEBHOOK=... python monitor.py --test`.
