"""
Music Radar : veille des nouvelles sorties musicales d'une liste d'artistes,
avec récap et tri sur Telegram.

Organisation du paquet (chaque module ne dépend que de ceux du dessus) :

    config        réglages (variables d'environnement) et constantes
    clock         date du jour (fuseau Europe/Paris)
    ratelimit     délai minimal entre deux appels à un même service
    text          normalisation des noms et des titres, crédits, éditions, variantes
    models        Release, Batch, Prepared, états de tri
    storage       base SQLite : sorties, tri, caches, migrations, sauvegardes
    sources/      clients des API externes : Apple (itunes), MusicBrainz
    artists       artists.txt, résolution des ID, ajout / retrait, homonymes
    features      featurings sur le projet d'un autre artiste
    analysis      ce qu'une sortie apporte de neuf (singles, rééditions, variantes)
    scan          parcours des artistes et lots quotidien / vendredi / rattrapage
    pipeline      préparation d'un envoi (tri existant, analyse) et finalisation
    formatting    messages Telegram (HTML)
    bot/          interface Telegram (commandes, boutons, planification)
"""
