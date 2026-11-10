# Music Radar

Bot Telegram auto-hébergé : il surveille les sorties d'environ 185 artistes sur
Apple Music (API publique iTunes, sans clé) et leurs featurings (MusicBrainz),
puis envoie un récap quotidien avec un tri en un appui.

## Règle absolue

**Ne jamais changer le comportement du bot sans l'accord explicite de Galaan.**
Proposer d'abord, attendre l'approbation, coder ensuite.

« Comportement » = tout ce que l'utilisateur perçoit : quelles sorties sont
envoyées et quand, ce qui entre ou sort de `/trier`, le texte des messages et
des notes, les états de tri, ce qui est écrit dans `artists.txt`.
Un correctif de bug qui change l'un de ces points est un changement de
comportement : il se propose, il ne se glisse pas dans un autre commit.

Ne demandent pas d'accord : tests, documentation, refactorisation et
optimisation qui ne changent ni ce qui est envoyé ni les requêtes faites.

Dans le doute, demander. Pas de `git push` sans demande.

## Commandes

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows
pytest -q                                   # aucun accès réseau
pytest -q --cov=musicradar --cov-report=term-missing
ruff check .
```

La CI (`.github/workflows/tests.yml`) lance `ruff check .` puis `pytest` à chaque push.

Diagnostic sans rien envoyer sur Telegram (fait de vrais appels Apple, ~5 min) :

```bash
python -m musicradar daily        # ou weekly, catchup 30, resolve
```

## Architecture

Paquet `musicradar/`, chaque module ne dépend que de ceux au-dessus de lui
(liste complète dans `musicradar/__init__.py`) :

```
config, clock      réglages (.env) et date du jour (Europe/Paris)
ratelimit          délai minimal entre deux appels à un même service
text               fonctions pures : normalisation, crédits, éditions, variantes
models             Release, Batch, Prepared, états de tri
storage            SQLite (data/radar.db) : TOUT le SQL du projet est ici
sources/           clients HTTP : itunes (Apple), musicbrainz
artists            artists.txt, résolution nom -> ID Apple, homonymes
features           featurings sur le projet d'un autre artiste
analysis           note de nouveauté (inédits, rééditions, versions alternatives)
scan               parcours des artistes, dédoublonnage, lots quotidien/vendredi/rattrapage
pipeline           prepare() avant l'envoi, finalize() après l'envoi réussi
formatting         messages Telegram (HTML)
bot/               interface Telegram (python-telegram-bot) : app, recaps, sorting, artists, ui
```

Le cœur ne dépend pas de Telegram ; `bot/` ne fait que l'appeler.

### Trajet d'un récap

`bot/recaps.run_recap` (sous `ui.scan_lock`, un seul scan à la fois) :

1. `scan.daily_batch()` : catalogue des artistes par paquets de 5
   (`itunes.fetch_many_artist_releases`, repli sur `fetch_artist_releases`),
   puis `features.find`, puis `scan.dedupe`, puis filtre des sorties déjà notifiées.
   MusicBrainz est interrogé en parallèle dès le début du scan, dans un seul
   thread (`features.MusicBrainzPrefetch`) ; `features.find` en lit les
   réponses dans l'ordre des artistes, donc le résultat ne dépend pas du thread.
2. `pipeline.prepare()` : reprend le tri existant, absorbe les fiches jumelles,
   lance `analysis.analyze` sur ce qui n'est pas trié, enregistre.
3. `recaps.deliver()` : envoi Telegram.
4. `pipeline.finalize()` : marque « notifié » **seulement après** l'envoi réussi.

### À savoir avant de toucher au code

- **Débit** : Apple tolère ~20 requêtes/min (3,2 s entre deux appels,
  `config.REQUEST_DELAY_S`), MusicBrainz 1 requête/s. Tout appel Apple doit
  passer par `itunes.get_json`, tout appel MusicBrainz par `musicbrainz.get` :
  ce sont eux qui espacent les appels (`ratelimit.RateLimiter`, commun à tous
  les threads). Ne pas ajouter de `time.sleep` ailleurs.
- **Tables** : `artists`, `seen` (notifié), `releases` (tri + analyse), `tracks`
  (cache des titres), `feature_names`, `mb_artists`, `verified`, `meta`.
  `seen` et `releases` sont distinctes : une sortie peut être dans l'une sans l'autre.
- **`album_id`** est l'ID Apple de l'album, ou l'ID du *titre* pour un featuring
  (`kind == "Featuring"`).
- **`Release.is_same_release`** : deux fiches Apple sont la même sortie
  (explicite / clean) si elles ont la même `dup_key` (artiste, type, titre
  normalisé) et des dates à 30 jours d'écart au plus (`config.TWIN_MAX_DAYS`).
- **Catalogue plafonné à 200 fiches par artiste** par l'API, et `sort=recent`
  n'est pas fiable : on lit tout.
- **`artists.txt`** est monté en fichier unique dans le conteneur : il se
  réécrit sur place, jamais par fichier temporaire + rename.
- Code, commentaires et messages en **français**. Lignes de 110 caractères.

## Tests

`tests/conftest.py` isole chaque test : base et `artists.txt` temporaires,
`time.sleep` neutralisé, `itunes.get_json` et `musicbrainz.get` remplacés
(`FakeItunes`, `FakeMusicBrainz`). Un appel réseau non simulé lève une erreur.
Fabriques : `collection()`, `song()`, `release()`, `recording()`.

Un correctif arrive avec son test, dans le même commit.

## Déploiement

Le bot tourne en Docker sur le serveur de Galaan.

```bash
git push                              # depuis le poste (sur demande uniquement)
bash ~/music-radar/deploy.sh          # sur le serveur : git pull --ff-only + docker compose up -d --build
```

- `.env`, `artists.txt` et `data/` ne sont pas suivis par Git ; `artists.txt`
  et `data/` sont montés en volume. Ne jamais lire ni commiter `.env`.
- Un déploiement pendant un scan l'interrompt sans rien perdre : rien n'est
  marqué notifié, le récap suivant couvre 14 jours.
- Au démarrage, `storage.migrate()` met la base à niveau (sans appel réseau).
  Toute évolution du schéma passe par là et doit rester rejouable.
- Les logs survivent aux reconstructions : `data/bot.log`.
