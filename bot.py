"""
Point d'entrée du Music Radar : `python bot.py`.

Le code est dans le paquet `musicradar` (voir musicradar/__init__.py pour
l'organisation des modules) ; l'interface Telegram dans `musicradar/bot/`.
"""

from musicradar.bot.app import main

if __name__ == "__main__":
    main()
